"""Rework attempts in the database: the record of one attempt, the patches it makes, and what the next one starts from.

An attempt covers an item, or the items of a unit, over their projects: per project the
ladder's files, rungs and Copilot calls are added to it (`record`). A patched project
gets a `migration_patch` (`save_patch`): source `ai` for the Copilot rungs, `original`
for rungs 1-2 (made by git from the Jira diff), always `proposed`, with the check and
intent findings. When the attempt ends (`finish`) its items take its outcome:
`proposed`, or `rework_failed` with the reason. The next attempt of an item starts from
the last failure's reason and the user's note on the last rejected patch (`context`).

Review (`accept` / `reject`): the user takes or turns down a rework patch as a whole. A
patch on a base is accepted only once every base patch is; a reject takes an optional
note. The item's rework status follows its current rework patches.
"""

from collections.abc import Sequence
from dataclasses import asdict
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from aosp_migrator.db.models import HistoryEvent, Item, JiraIssue, MigrationPatch, ReworkAttempt, utcnow
from aosp_migrator.importer.diffparse import parse_diff
from aosp_migrator.prescan.chain import REWORK_LEVELS, base_patches, current_patches, is_rework
from aosp_migrator.prescan.runner import current_diffs, diff_scan, read_diff_text
from aosp_migrator.rework.bundle import Context, Prerequisite
from aosp_migrator.rework.ladder import LadderResult

AI_LEVELS = ("resolve", "port", "reimpl")


class ReviewRefused(Exception):
    """The patch cannot take this review now."""


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


def same_change(a: str, b: str) -> bool:
    """Whether two diffs make the same change: the same files with the same added and removed lines, in order.
    Line numbers, context and headers may differ (a Jira patch and the rework's diff of it); binary files never match.
    """

    def change(text: str) -> list[tuple[Any, ...]] | None:
        files = parse_diff(text).files
        if any(f.binary for f in files):
            return None
        lines = (tuple(line for hunk in f.hunks for line in hunk.splitlines()[1:] if line.startswith(("+", "-"))) for f in files)
        return sorted((f.path, f.old_path, f.status, changed) for f, changed in zip(files, lines, strict=True))

    first = change(a)
    return first is not None and bool(first) and first == change(b)


def finish(session: Session, attempt: ReworkAttempt, status: str, reason: str | None = None) -> None:
    """End the attempt (`patched` / `unchanged` / `failed` / `canceled` / `error`); its items take the outcome, except on
    cancel, and on `unchanged`: nothing new was proposed, so the item keeps the patch and rework status it had.
    """
    attempt.status, attempt.reason, attempt.finished_at = status, reason, utcnow()
    if status not in ("canceled", "unchanged"):
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


def accept(session: Session, patch: MigrationPatch) -> None:
    """Accept a proposed rework patch; refused while a base patch is not accepted."""
    if not is_rework(patch):
        raise ReviewRefused("Only rework patches are reviewed")
    if patch.review != "proposed":
        raise ReviewRefused(f"Patch is {patch.review}")
    for base in base_patches(session, patch):
        if base.review != "accepted":
            key = session.scalar(select(JiraIssue.key).join(Item, Item.issue_id == JiraIssue.id).where(Item.id == base.item_id))
            raise ReviewRefused(f"Base patch of {key} ({base.project}) is {base.review}: accept it first")
    _review(session, patch, "accepted", None)


def reject(session: Session, patch: MigrationPatch, note: str | None) -> None:
    """Reject a proposed or accepted rework patch, with an optional note for the next rework."""
    if not is_rework(patch):
        raise ReviewRefused("Only rework patches are reviewed")
    if patch.review == "rejected":
        raise ReviewRefused("Patch is rejected")
    _review(session, patch, "rejected", (note or "").strip() or None)


def _review(session: Session, patch: MigrationPatch, review: str, note: str | None) -> None:
    patch.review, patch.review_note = review, note
    item = session.get(Item, patch.item_id)
    assert item is not None
    session.flush()
    current = [p.review for p in current_patches(session, item) if is_rework(p)]
    item.rework_status = "proposed" if "proposed" in current else "accepted" if current else "rejected"
    item.rework_reason = note if review == "rejected" else None
    payload = {"patch_id": patch.id, "project": patch.project, **({"note": note} if note else {})}
    session.add(HistoryEvent(item_id=item.id, type=f"patch_{review}", payload=payload))
    session.commit()


def rework_candidates(session: Session, workspace_id: int, keys: Sequence[str] | None = None) -> list[str]:
    """Keys of the items (of `keys`, else the workspace's backlog) worth a rework: their diffs conflict, or their `ai` patch failed to build."""
    query = select(Item, JiraIssue.key).join(JiraIssue, Item.issue_id == JiraIssue.id).where(Item.workspace_id == workspace_id, Item.dropped.is_(False))
    if keys is not None:
        query = query.where(JiraIssue.key.in_(keys))
    found: dict[str, None] = {}
    for item, key in session.execute(query.order_by(JiraIssue.key)):
        if item.triage_label == "conflicts" or (item.build_status == "build_failed" and any(p.source == "ai" for p in current_patches(session, item))):
            found[key] = None
    if keys is None:
        return sorted(found, key=_natural)
    return [key for key in dict.fromkeys(keys) if key in found]


def _natural(key: str) -> tuple[str, int]:
    prefix, _, number = key.rpartition("-")
    return (prefix, int(number)) if number.isdigit() else (key, 0)
