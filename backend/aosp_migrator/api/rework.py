from datetime import datetime
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict
from sqlalchemy import exists, func, select
from sqlalchemy.orm import Session

from aosp_migrator.api.deps import get_session
from aosp_migrator.db.models import BuildRun, Item, JiraIssue, MigrationPatch, ReworkAttempt, ReworkJob, Workspace
from aosp_migrator.rework import attempt as attempts
from aosp_migrator.rework.runner import Options, ReworkError, ReworkJobConflict, ReworkRunner

router = APIRouter()

SessionDep = Annotated[Session, Depends(get_session)]

REWORK_STATUSES = ("rework_failed", "proposed", "rejected", "accepted")


class ReworkRequest(BaseModel):
    keys: list[str]
    # Copilot rungs allowed (`resolve`, `port`, `reimpl`); None: the preselected ones.
    rungs: list[str] | None = None
    include_dependents: bool = False
    # Closed / not relevant prerequisites to add; other prerequisites to leave out.
    include: list[str] = []
    exclude: list[str] = []
    # The selected items as one unit.
    together: bool = False
    # None: the settings.
    item_cap: int | None = None
    job_cap: int | None = None
    build_rounds: int | None = None


class PlanItemOut(BaseModel):
    key: str
    summary: str
    local_state: str
    triage_label: str | None
    selected: bool
    dependent: bool
    included: bool
    rework: bool
    base: bool
    has_patch: bool
    accepted: bool
    stale: list[str]
    warning: str | None
    reason: str | None


class PlanOut(BaseModel):
    # Keys per unit to rework, in order.
    units: list[list[str]]
    # Every item of the chain, in chain order.
    items: list[PlanItemOut]
    # The items outside the selection that the selection needs, in chain order.
    prerequisites: list[PlanItemOut]
    # Rungs to preselect: re-implement too after a failure that needs it.
    rungs: list[str]
    # Copilot calls: likely and at most; units and items to rework.
    estimate: dict[str, int]
    # Caps in effect (the settings unless given).
    caps: dict[str, int]


class ReworkJobOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    workspace_id: int
    issue_keys: list[str]
    options: dict[str, Any]
    units: list[list[str]]
    skipped: list[dict[str, Any]]
    status: str
    done: int
    total: int
    calls: int
    errors: list[dict[str, Any]]
    error: str | None
    cancel_requested: bool
    started_at: datetime
    finished_at: datetime | None
    # Keys of the unit being reworked now, or last reworked; None before the first.
    unit_keys: list[str] | None = None


class BuildRunRef(BaseModel):
    id: int
    status: str
    summary: str | None
    log_url: str | None


class AttemptOut(BaseModel):
    id: int
    job_id: int | None
    keys: list[str]
    heads: dict[str, Any]
    base_patch_ids: list[int]
    rungs: list[str]
    files: list[dict[str, Any]]
    calls: list[dict[str, Any]]
    build_runs: list[BuildRunRef]
    notes: list[str]
    # Patches the attempt made.
    patch_ids: list[int]
    status: str
    reason: str | None
    started_at: datetime
    finished_at: datetime | None


class ReviewRequest(BaseModel):
    note: str | None = None


class ReviewOut(BaseModel):
    id: int
    review: str
    review_note: str | None
    rework_status: str | None


class ReworkSummaryOut(BaseModel):
    # Items per rework status, every status in REWORK_STATUSES order, zeros included.
    statuses: dict[str, int]
    # Never reworked.
    unreworked: int


def _workspace(session: Session, workspace_id: int) -> Workspace:
    workspace = session.get(Workspace, workspace_id)
    if workspace is None:
        raise HTTPException(status_code=404, detail="Workspace not found")
    return workspace


def _job(session: Session, job_id: int) -> ReworkJob:
    job = session.get(ReworkJob, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Rework job not found")
    return job


def _keys(session: Session, item_ids: list[int]) -> dict[int, str]:
    rows = session.execute(select(Item.id, JiraIssue.key).join(JiraIssue, Item.issue_id == JiraIssue.id).where(Item.id.in_(item_ids)))
    return {item_id: key for item_id, key in rows}


def _job_out(session: Session, job: ReworkJob) -> ReworkJobOut:
    out = ReworkJobOut.model_validate(job)
    attempt = session.scalar(select(ReworkAttempt).where(ReworkAttempt.job_id == job.id).order_by(ReworkAttempt.id.desc()).limit(1))
    if attempt is not None:
        keys = _keys(session, attempt.item_ids)
        out.unit_keys = [keys[i] for i in attempt.item_ids if i in keys]
    return out


def _options(body: ReworkRequest) -> Options:
    options = Options(
        include_dependents=body.include_dependents,
        include=body.include,
        exclude=body.exclude,
        together=body.together,
        item_cap=body.item_cap,
        job_cap=body.job_cap,
        build_rounds=body.build_rounds,
    )
    if body.rungs is not None:
        options.rungs = list(dict.fromkeys(body.rungs))
    return options


def _selection(body: ReworkRequest) -> list[str]:
    keys = list(dict.fromkeys(key.strip() for key in body.keys if key.strip()))
    if not keys:
        raise HTTPException(status_code=422, detail="No ticket keys given")
    for name, value, low, high in (("item_cap", body.item_cap, 1, 50), ("job_cap", body.job_cap, 1, 10000), ("build_rounds", body.build_rounds, 0, 10)):
        if value is not None and not low <= value <= high:
            raise HTTPException(status_code=422, detail=f"{name} must be between {low} and {high}")
    return keys


@router.post("/workspaces/{workspace_id}/rework/plan")
def plan_rework(workspace_id: int, body: ReworkRequest, request: Request, session: SessionDep) -> PlanOut:
    """What a rework of `keys` would do: units in order, prerequisites outside the selection, preselected rungs, estimate."""
    workspace = _workspace(session, workspace_id)
    runner: ReworkRunner = request.app.state.rework_runner
    options = _options(body)
    try:
        found = runner.plan(session, workspace, _selection(body), options)
    except ReworkError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    settings = request.app.state.config.copilot()
    items = [PlanItemOut(**{name: getattr(p, name) for name in PlanItemOut.model_fields}) for p in found.items.values()]
    return PlanOut(
        units=[[found.items[i].key for i in unit] for unit in found.units],
        items=items,
        prerequisites=[i for i in items if not i.selected],
        rungs=found.rungs,
        estimate=found.estimate,
        caps={
            "item_cap": options.item_cap or settings.item_cap,
            "job_cap": options.job_cap or settings.job_cap,
            "build_rounds": settings.build_rounds if options.build_rounds is None else options.build_rounds,
        },
    )


@router.post("/workspaces/{workspace_id}/rework", status_code=202)
def start_rework(workspace_id: int, body: ReworkRequest, request: Request, session: SessionDep) -> ReworkJobOut:
    """Rework `keys` (and what the options add) in the background."""
    workspace = _workspace(session, workspace_id)
    keys = _selection(body)
    runner: ReworkRunner = request.app.state.rework_runner
    options = _options(body)
    if body.rungs is None:
        try:
            options.rungs = runner.plan(session, workspace, keys, options).rungs
        except ReworkError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
    try:
        job = runner.start(workspace_id, keys, options)
    except ReworkJobConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ReworkError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    session.expire_all()
    return _job_out(session, _job(session, job.id))


@router.get("/workspaces/{workspace_id}/rework-jobs")
def list_rework_jobs(workspace_id: int, session: SessionDep, limit: int = 20) -> list[ReworkJobOut]:
    _workspace(session, workspace_id)
    jobs = session.scalars(select(ReworkJob).where(ReworkJob.workspace_id == workspace_id).order_by(ReworkJob.id.desc()).limit(limit))
    return [_job_out(session, job) for job in jobs]


@router.get("/rework-jobs/{job_id}")
def get_rework_job(job_id: int, session: SessionDep) -> ReworkJobOut:
    return _job_out(session, _job(session, job_id))


@router.post("/rework-jobs/{job_id}/cancel", status_code=202)
def cancel_rework_job(job_id: int, session: SessionDep) -> ReworkJobOut:
    """Ask the job to stop: a running Copilot call or build is killed, the scratch checkouts deleted; no further unit starts."""
    job = _job(session, job_id)
    if job.status != "running":
        raise HTTPException(status_code=409, detail=f"Rework job is {job.status}")
    job.cancel_requested = True
    session.commit()
    return _job_out(session, job)


@router.get("/workspaces/{workspace_id}/rework-summary")
def get_rework_summary(workspace_id: int, session: SessionDep) -> ReworkSummaryOut:
    """Rework status counts over the backlog items that are not dropped, for the count bar."""
    _workspace(session, workspace_id)
    rows = session.execute(
        select(Item.rework_status, func.count()).where(Item.workspace_id == workspace_id, Item.dropped.is_(False)).group_by(Item.rework_status)
    )
    counts: dict[str | None, int] = {status: count for status, count in rows}
    return ReworkSummaryOut(statuses={status: counts.get(status, 0) for status in REWORK_STATUSES}, unreworked=counts.get(None, 0))


def _item(session: Session, workspace_id: int, key: str) -> Item:
    _workspace(session, workspace_id)
    item = session.scalar(select(Item).join(JiraIssue, Item.issue_id == JiraIssue.id).where(Item.workspace_id == workspace_id, JiraIssue.key == key))
    if item is None:
        raise HTTPException(status_code=404, detail=f"Not in this workspace's backlog: {key}")
    return item


@router.get("/workspaces/{workspace_id}/items/{key}/rework-attempts")
def list_item_attempts(workspace_id: int, key: str, session: SessionDep) -> list[AttemptOut]:
    """Every rework attempt that included the item, newest first."""
    item = _item(session, workspace_id, key)
    each = func.json_each(ReworkAttempt.item_ids).table_valued("value")
    found = list(
        session.scalars(
            select(ReworkAttempt)
            .where(ReworkAttempt.workspace_id == workspace_id, exists(select(each.c.value).where(each.c.value == item.id)))
            .order_by(ReworkAttempt.id.desc())
        )
    )
    keys = _keys(session, [i for a in found for i in a.item_ids])
    out: list[AttemptOut] = []
    for attempt in found:
        runs = {run.id: run for run in session.scalars(select(BuildRun).where(BuildRun.id.in_(attempt.build_run_ids)))}
        patch_ids = list(session.scalars(select(MigrationPatch.id).where(MigrationPatch.attempt_id == attempt.id).order_by(MigrationPatch.id)))
        out.append(
            AttemptOut(
                id=attempt.id,
                job_id=attempt.job_id,
                keys=[keys[i] for i in attempt.item_ids if i in keys],
                heads=attempt.heads,
                base_patch_ids=attempt.base_patch_ids,
                rungs=attempt.rungs,
                files=attempt.files,
                calls=attempt.calls,
                build_runs=[
                    BuildRunRef(
                        id=run.id,
                        status=run.status,
                        summary=run.summary,
                        log_url=f"/api/workspaces/{workspace_id}/build-runs/{run.id}/log" if run.log_path and Path(run.log_path).is_file() else None,
                    )
                    for run_id in attempt.build_run_ids
                    if (run := runs.get(run_id)) is not None
                ],
                notes=attempt.notes,
                patch_ids=patch_ids,
                status=attempt.status,
                reason=attempt.reason,
                started_at=attempt.started_at,
                finished_at=attempt.finished_at,
            )
        )
    return out


def _patch(session: Session, workspace_id: int, patch_id: int) -> MigrationPatch:
    _workspace(session, workspace_id)
    patch = session.scalar(
        select(MigrationPatch).join(Item, MigrationPatch.item_id == Item.id).where(MigrationPatch.id == patch_id, Item.workspace_id == workspace_id)
    )
    if patch is None:
        raise HTTPException(status_code=404, detail="Migration patch not found")
    return patch


def _review_out(session: Session, patch: MigrationPatch) -> ReviewOut:
    item = session.get(Item, patch.item_id)
    assert item is not None
    return ReviewOut(id=patch.id, review=patch.review, review_note=patch.review_note, rework_status=item.rework_status)


@router.post("/workspaces/{workspace_id}/patches/{patch_id}/accept")
def accept_patch(workspace_id: int, patch_id: int, session: SessionDep) -> ReviewOut:
    """Accept a proposed rework patch: a build applies it. Refused while one of its base patches is not accepted."""
    patch = _patch(session, workspace_id, patch_id)
    try:
        attempts.accept(session, patch)
    except attempts.ReviewRefused as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _review_out(session, patch)


@router.post("/workspaces/{workspace_id}/patches/{patch_id}/reject")
def reject_patch(workspace_id: int, patch_id: int, body: ReviewRequest, session: SessionDep) -> ReviewOut:
    """Reject a rework patch, with an optional note the next rework of the item gets."""
    patch = _patch(session, workspace_id, patch_id)
    try:
        attempts.reject(session, patch, body.note)
    except attempts.ReviewRefused as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _review_out(session, patch)
