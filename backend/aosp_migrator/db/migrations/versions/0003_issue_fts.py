"""issue full-text index

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-25 21:10:00.000000

FTS5 table over key, summary, raw description and the file paths of diff
attachments, one row per `jira_issue` (rowid = issue id). Triggers keep it in
sync, so no code path that writes issues or attachments can forget it. The
trigram tokenizer gives substring matches, which suits file paths and
identifiers like `CameraService`.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _diff_paths(issue_id: str) -> str:
    """All paths (and rename sources) from the issue's parsed diff attachments, one per line."""
    return f"""(
        SELECT coalesce(group_concat(f.value ->> '$.path' || coalesce(char(10) || (f.value ->> '$.old_path'), ''), char(10)), '')
        FROM attachment a, json_each(a.parsed, '$.files') f
        WHERE a.issue_id = {issue_id} AND a.kind = 'diff'
    )"""


def upgrade() -> None:
    op.execute("CREATE VIRTUAL TABLE issue_fts USING fts5(key, summary, description, diff_paths, tokenize='trigram')")
    op.execute(
        f"""
        INSERT INTO issue_fts (rowid, key, summary, description, diff_paths)
        SELECT j.id, j.key, j.summary, j.description_raw, {_diff_paths("j.id")} FROM jira_issue j
        """
    )
    op.execute(
        """
        CREATE TRIGGER issue_fts_insert AFTER INSERT ON jira_issue BEGIN
            INSERT INTO issue_fts (rowid, key, summary, description, diff_paths)
            VALUES (NEW.id, NEW.key, NEW.summary, NEW.description_raw, '');
        END
        """
    )
    op.execute(
        """
        CREATE TRIGGER issue_fts_update AFTER UPDATE OF key, summary, description_raw ON jira_issue BEGIN
            UPDATE issue_fts SET key = NEW.key, summary = NEW.summary, description = NEW.description_raw WHERE rowid = NEW.id;
        END
        """
    )
    op.execute("CREATE TRIGGER issue_fts_delete AFTER DELETE ON jira_issue BEGIN DELETE FROM issue_fts WHERE rowid = OLD.id; END")
    for event, ref in (("INSERT", "NEW"), ("UPDATE", "NEW"), ("DELETE", "OLD")):
        op.execute(
            f"""
            CREATE TRIGGER attachment_fts_{event.lower()} AFTER {event} ON attachment BEGIN
                UPDATE issue_fts SET diff_paths = {_diff_paths(f"{ref}.issue_id")} WHERE rowid = {ref}.issue_id;
            END
            """
        )


def downgrade() -> None:
    for trigger in ("issue_fts_insert", "issue_fts_update", "issue_fts_delete", "attachment_fts_insert", "attachment_fts_update", "attachment_fts_delete"):
        op.execute(f"DROP TRIGGER {trigger}")
    op.execute("DROP TABLE issue_fts")
