"""Rework attempts in the database: the record of one attempt, the patches it makes, and what the next one starts from.

An attempt covers an item, or the items of a unit, over their projects: per project the
ladder's files, rungs and Copilot calls are added to it (`record`). A patched project
gets a `migration_patch` (`save_patch`): source `ai` for the Copilot rungs, `original`
for rungs 1-2 (made by git from the Jira diff), always `proposed`, with the check and
intent findings. When the attempt ends (`finish`) its items take its outcome:
`proposed`, or `rework_failed` with the reason. The next attempt of an item starts from
the last failure's reason and the user's note on the last rejected patch (`context`).
"""

from collections.abc import Sequence
from dataclasses import asdict

from sqlalchemy import select
from sqlalchemy.orm import Session

from aosp_migrator.db.models import Item, JiraIssue, MigrationPatch, ReworkAttempt, utcnow
from aosp_migrator.prescan.chain import REWORK_LEVELS, current_patches
from aosp_migrator.prescan.runner import current_diffs, diff_scan, read_diff_text
from aosp_migrator.rework.bundle import Context, Prerequisite
from aosp_migrator.rework.ladder import LadderResult

AI_LEVELS = ("resolve", "port", "reimpl")


def start(
    session: Session, workspace_id: int, item_ids: Sequence[int], heads: dict[str, str | None], base_patch_ids: Sequence[int] = (), job_id: int | None = None
) -> ReworkAttempt:
    attempt = ReworkAttempt(workspace_id=workspace_id, job_id=job_id, item_ids=list(item_ids), heads=dict(heads), base_patch_ids=list(base_patch_ids))
    session.add(attempt)
    session.commit()
    return attempt


def record(session: Session, attempt: ReworkAttempt, project: str, result: LadderResult) -> None:
    """Add one project's ladder outcome to the attempt."""
    attempt.rungs = list(dict.fromkeys([*attempt.rungs, *result.rungs]))
    attempt.files = [*attempt.files, *({"project": project, **f} for f in result.files)]
    attempt.calls = [*attempt.calls, *({"project": project, **asdict(c)} for c in result.calls)]
    session.commit()


def save_patch(
    session: Session,
    attempt: ReworkAttempt,
    item_id: int,
    project: str,
    result: LadderResult,
    base_head: str | None,
    base_patch_ids: Sequence[int],
    author: str | None = None,
    message: str | None = None,
) -> MigrationPatch:
    """The patch of a patched ladder result, `proposed` for review."""
    assert result.status == "patched" and result.level in REWORK_LEVELS and result.patch is not None
    patch = MigrationPatch(
        item_id=item_id,
        attachment_id=None,
        project=project,
        source="ai" if result.level in AI_LEVELS else "original",
        level=result.level,
        base_head=base_head,
        base_patch_ids=list(base_patch_ids),
        review="proposed",
        findings=[f.to_json() for f in result.findings],
        attempt_id=attempt.id,
        diff=result.patch,
        author=author,
        message=message,
    )
    session.add(patch)
    session.commit()
    return patch


def finish(session: Session, attempt: ReworkAttempt, status: str, reason: str | None = None) -> None:
    """End the attempt (`patched` / `failed` / `canceled` / `error`); its items take the outcome, except on cancel."""
    attempt.status, attempt.reason, attempt.finished_at = status, reason, utcnow()
    if status != "canceled":
        for item in session.scalars(select(Item).where(Item.id.in_(attempt.item_ids))):
            item.rework_status = "proposed" if status == "patched" else "rework_failed"
            item.rework_reason = reason
            item.reworked_at = attempt.finished_at
    session.commit()


def last_failure(session: Session, item: Item) -> str | None:
    """Why the item's last finished attempt failed; None when it did not."""
    for attempt in session.scalars(
        select(ReworkAttempt).where(ReworkAttempt.workspace_id == item.workspace_id, ReworkAttempt.finished_at.is_not(None)).order_by(ReworkAttempt.id.desc())
    ):
        if item.id in attempt.item_ids:
            return attempt.reason if attempt.status in ("failed", "error") else None
    return None


def reject_note(session: Session, item: Item, project: str) -> str | None:
    """The user's note on the item's newest rework patch for the project, when it was rejected."""
    patch = session.scalar(
        select(MigrationPatch)
        .where(MigrationPatch.item_id == item.id, MigrationPatch.project == project, MigrationPatch.level.in_(REWORK_LEVELS))
        .order_by(MigrationPatch.id.desc())
        .limit(1)
    )
    return patch.review_note if patch is not None and patch.review == "rejected" else None


def project_diffs(session: Session, item: Item, project: str) -> list[str]:
    """The item's current diffs mapped to `project`, as text, in diff order."""
    texts: list[str] = []
    for attachment in current_diffs(session, item.issue_id):
        scan = diff_scan(session, attachment.id)
        if scan is not None and scan.project == project and (text := read_diff_text(attachment)) is not None:
            texts.append(text)
    return texts


def context(session: Session, item: Item, project: str, prerequisites: Sequence[Item] = ()) -> Context:
    """The bundle's context for the item's project: ticket, last failure, reject note, prerequisites with diffs there."""
    issue = session.get(JiraIssue, item.issue_id)
    assert issue is not None
    found: list[Prerequisite] = []
    for other in prerequisites:
        original = "".join(project_diffs(session, other, project))
        if not original:
            continue
        other_issue = session.get(JiraIssue, other.issue_id)
        assert other_issue is not None
        migrated = [p.diff for p in current_patches(session, other) if p.project == project]
        found.append(Prerequisite(other_issue.key, other_issue.summary, original, "".join(migrated) if migrated else None))
    return Context(issue.key, issue.summary, issue.description_raw, reject_note(session, item, project), last_failure(session, item), found)
