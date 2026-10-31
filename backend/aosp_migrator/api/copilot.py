from datetime import timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from aosp_migrator.api.deps import get_session
from aosp_migrator.copilot.usage import CallRef, summarize
from aosp_migrator.db.models import Item, JiraIssue, ReworkAttempt, Workspace, utcnow

router = APIRouter()

SessionDep = Annotated[Session, Depends(get_session)]


@router.get("/copilot/usage")
def get_copilot_usage(
    session: SessionDep,
    workspace_id: int | None = None,
    # Calls of attempts started in the last this many days; None for all.
    days: Annotated[int | None, Query(ge=1, le=3650)] = None,
) -> dict[str, Any]:
    """Copilot calls made by rework attempts: totals, premium requests, tokens, by rung / day / workspace / model."""
    names = {w.id: w.name for w in session.scalars(select(Workspace))}
    if workspace_id is not None and workspace_id not in names:
        raise HTTPException(status_code=404, detail="Workspace not found")
    query = select(ReworkAttempt).order_by(ReworkAttempt.id)
    if workspace_id is not None:
        query = query.where(ReworkAttempt.workspace_id == workspace_id)
    if days is not None:
        query = query.where(ReworkAttempt.started_at >= utcnow() - timedelta(days=days))
    found = [a for a in session.scalars(query) if a.calls]
    item_ids = {i for a in found for i in a.item_ids}
    rows = session.execute(select(Item.id, JiraIssue.key).join(JiraIssue, Item.issue_id == JiraIssue.id).where(Item.id.in_(item_ids))) if item_ids else []
    keys = {item_id: key for item_id, key in rows}
    refs = [
        CallRef(a.id, a.workspace_id, [keys[i] for i in a.item_ids if i in keys], a.started_at, call)
        for a in found
        for call in a.calls
        if isinstance(call, dict)
    ]
    return {"workspace_id": workspace_id, "days": days, **summarize(refs, names)}
