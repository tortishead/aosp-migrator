from datetime import datetime
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from aosp_migrator.api.deps import get_session
from aosp_migrator.db.models import Attachment, DiffScan, Item, JiraIssue, MigrationPatch, Workspace
from aosp_migrator.importer.diffparse import parse_diff
from aosp_migrator.prescan.chain import current_patches, is_rework, patch_stale
from aosp_migrator.prescan.runner import current_diffs

router = APIRouter(prefix="/workspaces/{workspace_id}")

SessionDep = Annotated[Session, Depends(get_session)]


class BaseRef(BaseModel):
    id: int
    key: str
    project: str
    review: str


class PatchOut(BaseModel):
    id: int
    attachment_id: int | None
    # The Jira diff it was made from; None for `ai` patches.
    jira_id: str | None
    # That diff's filename; for a rework patch, the item's diff of its project with `_modified` before the extension.
    filename: str | None
    project: str
    source: str
    level: str
    base_head: str | None
    # Patches of other items it was made on top of, in apply order.
    base_patch_ids: list[int]
    # Those patches with their item, in the same order; a base patch since deleted is left out.
    bases: list[BaseRef]
    review: str  # proposed / accepted / rejected
    review_note: str | None
    findings: list[Any]
    # The rework attempt that made it; None for pre-scan patches.
    attempt_id: int | None
    # Why a rework patch is out of date (HEAD moved, base changed, new diff version); empty when fresh.
    stale: list[str]
    author: str | None
    message: str | None
    # What a build applies now: a rework patch of its project, else the current diff version's patch as last scanned.
    current: bool
    created_at: datetime


def modified_name(filename: str) -> str:
    """`0001-fix.patch` -> `0001-fix_modified.patch`; `fix` -> `fix_modified`."""
    stem, dot, ext = filename.rpartition(".")
    return f"{stem}_modified.{ext}" if dot and stem else f"{filename}_modified"


def _diff_names(session: Session, item: Item) -> dict[str, str]:
    """Project to the filename of the item's first current diff mapped to it."""
    names: dict[str, str] = {}
    for attachment in current_diffs(session, item.issue_id):
        project = session.scalar(select(DiffScan.project).where(DiffScan.attachment_id == attachment.id))
        if project is not None:
            names.setdefault(project, attachment.filename)
    return names


def _item(session: Session, workspace_id: int, key: str) -> tuple[Item, JiraIssue]:
    if session.get(Workspace, workspace_id) is None:
        raise HTTPException(status_code=404, detail="Workspace not found")
    row = session.execute(
        select(Item, JiraIssue).join(JiraIssue, Item.issue_id == JiraIssue.id).where(Item.workspace_id == workspace_id, JiraIssue.key == key)
    ).one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail=f"Not in this workspace's backlog: {key}")
    item, issue = row
    return item, issue


@router.get("/items/{key}/patches")
def list_item_patches(workspace_id: int, key: str, session: SessionDep) -> list[PatchOut]:
    """Every migration patch of an item, newest first; without the patch text."""
    item, _ = _item(session, workspace_id, key)
    workspace = session.get(Workspace, workspace_id)
    assert workspace is not None
    current = {patch.id for patch in current_patches(session, item)}
    heads: dict[str, str | None] = {}
    rows = session.execute(
        select(MigrationPatch, Attachment)
        .outerjoin(Attachment, MigrationPatch.attachment_id == Attachment.id)
        .where(MigrationPatch.item_id == item.id)
        .order_by(MigrationPatch.id.desc())
    )
    rows = list(rows)
    diff_names = _diff_names(session, item)
    stale = {patch.id: patch_stale(session, Path(workspace.target_tree), patch, heads) for patch, _ in rows if is_rework(patch)}
    base_ids = {i for patch, _ in rows for i in patch.base_patch_ids}
    bases = {
        base.id: BaseRef(id=base.id, key=key, project=base.project, review=base.review)
        for base, key in session.execute(
            select(MigrationPatch, JiraIssue.key)
            .join(Item, MigrationPatch.item_id == Item.id)
            .join(JiraIssue, Item.issue_id == JiraIssue.id)
            .where(MigrationPatch.id.in_(base_ids))
        )
    }
    return [
        PatchOut(
            id=patch.id,
            attachment_id=patch.attachment_id,
            jira_id=attachment.jira_id if attachment else None,
            filename=attachment.filename if attachment else modified_name(diff_names[patch.project]) if patch.project in diff_names else None,
            project=patch.project,
            source=patch.source,
            level=patch.level,
            base_head=patch.base_head,
            base_patch_ids=patch.base_patch_ids,
            bases=[bases[i] for i in patch.base_patch_ids if i in bases],
            review=patch.review,
            review_note=patch.review_note,
            findings=patch.findings,
            attempt_id=patch.attempt_id,
            stale=stale.get(patch.id, []),
            author=patch.author,
            message=patch.message,
            current=patch.id in current,
            created_at=patch.created_at,
        )
        for patch, attachment in rows
    ]


@router.get("/patches/{patch_id}")
def get_patch(workspace_id: int, patch_id: int, session: SessionDep) -> dict[str, Any]:
    """A migration patch, parsed, with the raw text of each hunk for the diff viewer."""
    patch = session.scalar(
        select(MigrationPatch).join(Item, MigrationPatch.item_id == Item.id).where(MigrationPatch.id == patch_id, Item.workspace_id == workspace_id)
    )
    if patch is None:
        raise HTTPException(status_code=404, detail="Migration patch not found")
    return parse_diff(patch.diff).to_json(hunks=True)
