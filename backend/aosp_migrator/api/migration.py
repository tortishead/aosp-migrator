from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from aosp_migrator.api.deps import get_session
from aosp_migrator.db.models import Attachment, Item, JiraIssue, MigrationPatch, Workspace
from aosp_migrator.importer.diffparse import parse_diff
from aosp_migrator.prescan.runner import current_diffs, current_patch, diff_scan

router = APIRouter(prefix="/workspaces/{workspace_id}")

SessionDep = Annotated[Session, Depends(get_session)]


class PatchOut(BaseModel):
    id: int
    attachment_id: int | None
    # The Jira diff it was made from; None for `ai` patches.
    jira_id: str | None
    filename: str | None
    project: str
    source: str
    level: str
    base_head: str | None
    author: str | None
    message: str | None
    # What a build applies now for its diff: the current diff version's patch as last scanned.
    current: bool
    created_at: datetime


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
    item, issue = _item(session, workspace_id, key)
    current = {patch.id for a in current_diffs(session, issue.id) if (patch := current_patch(session, diff_scan(session, a.id)))}
    rows = session.execute(
        select(MigrationPatch, Attachment)
        .outerjoin(Attachment, MigrationPatch.attachment_id == Attachment.id)
        .where(MigrationPatch.item_id == item.id)
        .order_by(MigrationPatch.id.desc())
    )
    return [
        PatchOut(
            id=patch.id,
            attachment_id=patch.attachment_id,
            jira_id=attachment.jira_id if attachment else None,
            filename=attachment.filename if attachment else None,
            project=patch.project,
            source=patch.source,
            level=patch.level,
            base_head=patch.base_head,
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
