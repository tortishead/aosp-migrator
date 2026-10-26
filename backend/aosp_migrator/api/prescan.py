from datetime import datetime
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from aosp_migrator.api.deps import get_session
from aosp_migrator.db.models import DiffScan, Item, JiraIssue, PrescanJob, Workspace
from aosp_migrator.prescan.presence import combine
from aosp_migrator.prescan.triage import ORDER
from aosp_migrator.prescan.runner import JobConflictError, PrescanError, PrescanRunner, current_diffs, stale_items, target_projects

router = APIRouter()

SessionDep = Annotated[Session, Depends(get_session)]


class PrescanRequest(BaseModel):
    # Omitted: scan every backlog item that is not dropped. Otherwise these items (one for a single-item re-run).
    keys: list[str] | None = None


class PrescanJobOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    workspace_id: int
    scope: str
    issue_keys: list[str]
    status: str
    stage: str | None
    done: int
    total: int | None
    errors: list[dict[str, Any]]
    error: str | None
    cancel_requested: bool
    started_at: datetime
    finished_at: datetime | None


class DiffScanOut(BaseModel):
    attachment_id: int
    jira_id: str
    filename: str
    # None while this diff version has not been scanned.
    scanned_at: datetime | None
    project: str | None
    mapping_source: str | None
    candidates: list[str]
    presence: str | None
    presence_detail: dict[str, Any] | None
    # None when present or unmapped.
    apply: str | None
    apply_detail: dict[str, Any] | None
    target_head: str | None


class ItemPrescanOut(BaseModel):
    scanned: bool
    # Triage label of the item and why; None until scanned, and for items without diffs.
    label: str | None
    label_reason: str | None
    # Combined over the mapped diffs; None until scanned.
    presence: str | None
    stale: list[str]
    diffs: list[DiffScanOut]


class ProjectPick(BaseModel):
    # None: back to the automatic mapping.
    project: str | None


class PrescanSummaryOut(BaseModel):
    # Items per triage label, every label in order (worst first), zeros included.
    labels: dict[str, int]
    # Items without a label: not scanned yet, or without diffs.
    unlabeled: int
    stale: int


def _workspace(session: Session, workspace_id: int) -> Workspace:
    workspace = session.get(Workspace, workspace_id)
    if workspace is None:
        raise HTTPException(status_code=404, detail="Workspace not found")
    return workspace


def _job(session: Session, job_id: int) -> PrescanJob:
    job = session.get(PrescanJob, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Pre-scan job not found")
    return job


def _backlog_keys(session: Session, workspace_id: int, keys: list[str]) -> list[str]:
    known = set(
        session.scalars(
            select(JiraIssue.key).join(Item, Item.issue_id == JiraIssue.id).where(Item.workspace_id == workspace_id, JiraIssue.key.in_(keys))
        )
    )
    if unknown := [key for key in keys if key not in known]:
        raise HTTPException(status_code=404, detail=f"Not in this workspace's backlog: {', '.join(unknown)}")
    return keys


@router.post("/workspaces/{workspace_id}/prescan", status_code=202)
def start_prescan(workspace_id: int, body: PrescanRequest, request: Request, session: SessionDep) -> PrescanJobOut:
    _workspace(session, workspace_id)
    keys = None
    if body.keys is not None:
        keys = list(dict.fromkeys(key.strip() for key in body.keys if key.strip()))
        if not keys:
            raise HTTPException(status_code=422, detail="No ticket keys given")
        _backlog_keys(session, workspace_id, keys)
    runner: PrescanRunner = request.app.state.prescan_runner
    try:
        job = runner.start(workspace_id, keys)
    except JobConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return PrescanJobOut.model_validate(job)


@router.get("/workspaces/{workspace_id}/prescan-jobs")
def list_prescan_jobs(workspace_id: int, session: SessionDep, limit: int = 20) -> list[PrescanJobOut]:
    _workspace(session, workspace_id)
    jobs = session.scalars(select(PrescanJob).where(PrescanJob.workspace_id == workspace_id).order_by(PrescanJob.id.desc()).limit(limit))
    return [PrescanJobOut.model_validate(job) for job in jobs]


@router.get("/prescan-jobs/{job_id}")
def get_prescan_job(job_id: int, session: SessionDep) -> PrescanJobOut:
    return PrescanJobOut.model_validate(_job(session, job_id))


@router.post("/prescan-jobs/{job_id}/cancel", status_code=202)
def cancel_prescan_job(job_id: int, session: SessionDep) -> PrescanJobOut:
    """Ask the job to stop; it does so before the next item."""
    job = _job(session, job_id)
    if job.status != "running":
        raise HTTPException(status_code=409, detail=f"Pre-scan job is {job.status}")
    job.cancel_requested = True
    session.commit()
    return PrescanJobOut.model_validate(job)


@router.get("/workspaces/{workspace_id}/prescan-summary")
def get_prescan_summary(workspace_id: int, session: SessionDep) -> PrescanSummaryOut:
    """Triage counts over the backlog items that are not dropped, for the count bar."""
    workspace = _workspace(session, workspace_id)
    rows = session.execute(
        select(Item.triage_label, func.count()).where(Item.workspace_id == workspace_id, Item.dropped.is_(False)).group_by(Item.triage_label)
    )
    counts: dict[str | None, int] = {label: count for label, count in rows}
    kept = set(session.scalars(select(Item.id).where(Item.workspace_id == workspace_id, Item.dropped.is_(False))))
    stale = stale_items(session, workspace_id, Path(workspace.target_tree))
    return PrescanSummaryOut(
        labels={label: counts.get(label, 0) for label in ORDER},
        unlabeled=counts.get(None, 0),
        stale=len(kept & stale.keys()),
    )


@router.get("/workspaces/{workspace_id}/target-projects")
def list_target_projects(workspace_id: int, session: SessionDep) -> list[str]:
    """Projects of the target tree, for picking a diff's project by hand."""
    try:
        _, projects = target_projects(_workspace(session, workspace_id))
    except PrescanError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return projects


def _item(session: Session, workspace_id: int, key: str) -> tuple[Item, JiraIssue]:
    row = session.execute(
        select(Item, JiraIssue).join(JiraIssue, Item.issue_id == JiraIssue.id).where(Item.workspace_id == workspace_id, JiraIssue.key == key)
    ).one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail=f"Not in this workspace's backlog: {key}")
    item, issue = row
    return item, issue


@router.put("/workspaces/{workspace_id}/items/{key}/diffs/{attachment_id}/project")
def pick_project(workspace_id: int, key: str, attachment_id: int, body: ProjectPick, request: Request, session: SessionDep) -> ItemPrescanOut:
    """Map a current diff to a project by hand, kept across re-runs; the item is pre-scanned again right away."""
    _workspace(session, workspace_id)
    item, issue = _item(session, workspace_id, key)
    if attachment_id not in {a.id for a in current_diffs(session, issue.id)}:
        raise HTTPException(status_code=404, detail=f"Not a current diff of {key}")
    runner: PrescanRunner = request.app.state.prescan_runner
    try:
        runner.pick_project(workspace_id, item.id, attachment_id, body.project)
    except JobConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except PrescanError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    session.expire_all()
    return get_item_prescan(workspace_id, key, session)


@router.get("/workspaces/{workspace_id}/items/{key}/prescan")
def get_item_prescan(workspace_id: int, key: str, session: SessionDep) -> ItemPrescanOut:
    """Pre-scan results of one backlog item: per current diff, and whether they are stale."""
    workspace = _workspace(session, workspace_id)
    item, issue = _item(session, workspace_id, key)

    diffs: list[DiffScanOut] = []
    for attachment in current_diffs(session, issue.id):
        scan = session.scalar(select(DiffScan).where(DiffScan.attachment_id == attachment.id))
        diffs.append(
            DiffScanOut(
                attachment_id=attachment.id,
                jira_id=attachment.jira_id,
                filename=attachment.filename,
                scanned_at=scan.scanned_at if scan else None,
                project=scan.project if scan else None,
                mapping_source=scan.mapping_source if scan else None,
                candidates=scan.candidates if scan else [],
                presence=scan.presence if scan else None,
                presence_detail=scan.presence_detail if scan else None,
                apply=scan.apply if scan else None,
                apply_detail=scan.apply_detail if scan else None,
                target_head=scan.target_head if scan else None,
            )
        )
    scanned = [d for d in diffs if d.scanned_at is not None]
    return ItemPrescanOut(
        scanned=bool(scanned),
        label=item.triage_label,
        label_reason=item.triage_reason,
        presence=combine(d.presence or "unknown" for d in scanned) if scanned else None,  # type: ignore[misc]
        stale=stale_items(session, workspace_id, Path(workspace.target_tree), [item.id]).get(item.id, []),
        diffs=diffs,
    )

