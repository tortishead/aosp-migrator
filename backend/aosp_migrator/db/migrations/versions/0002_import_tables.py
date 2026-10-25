"""import tables

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-25 20:30:47.793335
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "import_job",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("workspace_id", sa.Integer(), nullable=False),
        sa.Column("scope", sa.String(), nullable=False),
        sa.Column("issue_keys", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("done", sa.Integer(), nullable=False),
        sa.Column("total", sa.Integer(), nullable=True),
        sa.Column("errors", sa.JSON(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("cancel_requested", sa.Boolean(), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspace.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("import_job", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_import_job_workspace_id"), ["workspace_id"], unique=False)

    op.create_table(
        "jira_issue",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("workspace_id", sa.Integer(), nullable=False),
        sa.Column("key", sa.String(), nullable=False),
        sa.Column("jira_id", sa.String(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("description_raw", sa.Text(), nullable=False),
        sa.Column("description_html", sa.Text(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("labels", sa.JSON(), nullable=False),
        sa.Column("components", sa.JSON(), nullable=False),
        sa.Column("fix_versions", sa.JSON(), nullable=False),
        sa.Column("issue_links", sa.JSON(), nullable=False),
        sa.Column("jira_updated", sa.DateTime(), nullable=False),
        sa.Column("fetched_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspace.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("workspace_id", "key"),
    )
    with op.batch_alter_table("jira_issue", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_jira_issue_workspace_id"), ["workspace_id"], unique=False)

    op.create_table(
        "attachment",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("issue_id", sa.Integer(), nullable=False),
        sa.Column("jira_id", sa.String(), nullable=False),
        sa.Column("filename", sa.String(), nullable=False),
        sa.Column("size", sa.Integer(), nullable=False),
        sa.Column("mime", sa.String(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("url", sa.String(), nullable=False),
        sa.Column("local_path", sa.String(), nullable=True),
        sa.Column("sha256", sa.String(), nullable=True),
        sa.Column("is_current", sa.Boolean(), nullable=False),
        sa.Column("created", sa.DateTime(), nullable=False),
        sa.Column("parsed", sa.JSON(), nullable=True),
        sa.ForeignKeyConstraint(["issue_id"], ["jira_issue.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("issue_id", "jira_id"),
    )
    with op.batch_alter_table("attachment", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_attachment_issue_id"), ["issue_id"], unique=False)

    op.create_table(
        "comment",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("issue_id", sa.Integer(), nullable=False),
        sa.Column("jira_id", sa.String(), nullable=False),
        sa.Column("author", sa.String(), nullable=False),
        sa.Column("created", sa.DateTime(), nullable=False),
        sa.Column("body_html", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(["issue_id"], ["jira_issue.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("issue_id", "jira_id"),
    )
    with op.batch_alter_table("comment", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_comment_issue_id"), ["issue_id"], unique=False)

    op.create_table(
        "item",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("workspace_id", sa.Integer(), nullable=False),
        sa.Column("issue_id", sa.Integer(), nullable=False),
        sa.Column("local_state", sa.String(), nullable=False),
        sa.Column("dropped", sa.Boolean(), nullable=False),
        sa.Column("last_import_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["issue_id"], ["jira_issue.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspace.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("issue_id"),
    )
    with op.batch_alter_table("item", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_item_workspace_id"), ["workspace_id"], unique=False)

    op.create_table(
        "history_event",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("item_id", sa.Integer(), nullable=False),
        sa.Column("type", sa.String(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["item_id"], ["item.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("history_event", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_history_event_item_id"), ["item_id"], unique=False)

    op.create_table(
        "item_link",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("item_id", sa.Integer(), nullable=False),
        sa.Column("issue_id", sa.Integer(), nullable=False),
        sa.Column("link_type", sa.String(), nullable=False),
        sa.Column("direction", sa.String(), nullable=False),
        sa.Column("description", sa.String(), nullable=False),
        sa.ForeignKeyConstraint(["issue_id"], ["jira_issue.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["item_id"], ["item.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("item_link", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_item_link_issue_id"), ["issue_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_item_link_item_id"), ["item_id"], unique=False)

    op.create_table(
        "note",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("item_id", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["item_id"], ["item.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("note", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_note_item_id"), ["item_id"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("note", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_note_item_id"))

    op.drop_table("note")
    with op.batch_alter_table("item_link", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_item_link_item_id"))
        batch_op.drop_index(batch_op.f("ix_item_link_issue_id"))

    op.drop_table("item_link")
    with op.batch_alter_table("history_event", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_history_event_item_id"))

    op.drop_table("history_event")
    with op.batch_alter_table("item", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_item_workspace_id"))

    op.drop_table("item")
    with op.batch_alter_table("comment", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_comment_issue_id"))

    op.drop_table("comment")
    with op.batch_alter_table("attachment", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_attachment_issue_id"))

    op.drop_table("attachment")
    with op.batch_alter_table("jira_issue", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_jira_issue_workspace_id"))

    op.drop_table("jira_issue")
    with op.batch_alter_table("import_job", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_import_job_workspace_id"))

    op.drop_table("import_job")
