from datetime import datetime
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, ConfigDict
from sqlalchemy import exists, func, select
from sqlalchemy.orm import Session

from aosp_migrator.api.deps import get_session
from aosp_migrator.build.runner import BuildError, BuildJobConflict, BuildRunner
from aosp_migrator.db.models import BuildJob, BuildRun, Cluster, Item, JiraIssue, Workspace

router = APIRouter()

SessionDep = Annotated[Session, Depends(get_session)]

BUILD_STATUSES = ("apply_failed", "build_failed", "build_successful")
# Last lines of the running unit's log in a job.
TAIL_LINES = 40
# Bytes read from the end of a log for its tail.
TAIL_BYTES = 16 * 1024


class BuildRequest(BaseModel):
    # One of them: these items, one unit each; or the cluster's members as one unit.
    keys: list[str] | None = None
    cluster_id: int | None = None


class BuildJobOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    workspace_id: int
    scope: str
    issue_keys: list[str]
    cluster_id: int | None
    skipped: list[dict[str, Any]]
    status: str
    done: int
    total: int
    errors: list[dict[str, Any]]
    error: str | None
    cancel_requested: bool
    started_at: datetime
    finished_at: datetime | None
    # Keys of the unit building now, or last built; None before the first unit.
    unit_keys: list[str] | None = None
    # Last lines of that unit's log.
    log_tail: str | None = None


class BuildRunOut(BaseModel):
    id: int
    job_id: int
    # Every item of the unit, in build order (several for a cluster).
    keys: list[str]
    cluster_id: int | None
    patch_ids: list[int]
    tool: str | None
    modules: list[str]
    command: str | None
    status: str
    summary: str | None
    # Where this server serves the full log; None when there is none.
    log_url: str | None
    started_at: datetime
    finished_at: datetime | None


class BuildSummaryOut(BaseModel):
    # Items per build status, every status in BUILD_STATUSES order, zeros included.
    statuses: dict[str, int]
    # Never built.
    unbuilt: int


def _workspace(session: Session, workspace_id: int) -> Workspace:
    workspace = session.get(Workspace, workspace_id)
    if workspace is None:
        raise HTTPException(status_code=404, detail="Workspace not found")
    return workspace


def _job(session: Session, job_id: int) -> BuildJob:
    job = session.get(BuildJob, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Build job not found")
    return job


def _keys(session: Session, item_ids: list[int]) -> dict[int, str]:
    rows = session.execute(select(Item.id, JiraIssue.key).join(JiraIssue, Item.issue_id == JiraIssue.id).where(Item.id.in_(item_ids)))
    return {item_id: key for item_id, key in rows}


def log_tail(path: Path, lines: int = TAIL_LINES) -> str:
    with path.open("rb") as f:
        f.seek(max(0, f.seek(0, 2) - TAIL_BYTES))
        text = f.read().decode(errors="replace")
    return "\n".join(text.splitlines()[-lines:])


def _job_out(session: Session, job: BuildJob) -> BuildJobOut:
    out = BuildJobOut.model_validate(job)
    run = session.scalar(select(BuildRun).where(BuildRun.job_id == job.id).order_by(BuildRun.id.desc()).limit(1))
    if run is not None:
        keys = _keys(session, run.item_ids)
        out.unit_keys = [keys[i] for i in run.item_ids if i in keys]
        if run.log_path and Path(run.log_path).is_file():
            out.log_tail = log_tail(Path(run.log_path))
    return out


@router.post("/workspaces/{workspace_id}/build", status_code=202)
def start_build(workspace_id: int, body: BuildRequest, request: Request, session: SessionDep) -> BuildJobOut:
    """Build the eligible items among `keys` (one unit each), or a cluster (one unit); the others are listed as skipped."""
    _workspace(session, workspace_id)
    if (body.keys is None) == (body.cluster_id is None):
        raise HTTPException(status_code=422, detail="Give either keys or cluster_id")
    keys = None
    if body.keys is not None:
        keys = list(dict.fromkeys(key.strip() for key in body.keys if key.strip()))
        if not keys:
            raise HTTPException(status_code=422, detail="No ticket keys given")
    if body.cluster_id is not None:
        cluster = session.get(Cluster, body.cluster_id)
        if cluster is None or cluster.workspace_id != workspace_id:
            raise HTTPException(status_code=404, detail="Cluster not found")
    runner: BuildRunner = request.app.state.build_runner
    try:
        job = runner.start(workspace_id, keys=keys, cluster_id=body.cluster_id)
    except BuildJobConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except BuildError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    session.expire_all()
    return _job_out(session, _job(session, job.id))


@router.get("/workspaces/{workspace_id}/build-jobs")
def list_build_jobs(workspace_id: int, session: SessionDep, limit: int = 20) -> list[BuildJobOut]:
    _workspace(session, workspace_id)
    jobs = session.scalars(select(BuildJob).where(BuildJob.workspace_id == workspace_id).order_by(BuildJob.id.desc()).limit(limit))
    return [_job_out(session, job) for job in jobs]


@router.get("/build-jobs/{job_id}")
def get_build_job(job_id: int, session: SessionDep) -> BuildJobOut:
    return _job_out(session, _job(session, job_id))


@router.post("/build-jobs/{job_id}/cancel", status_code=202)
def cancel_build_job(job_id: int, session: SessionDep) -> BuildJobOut:
    """Ask the job to stop: a running build is killed and its tree restored; no further unit starts."""
    job = _job(session, job_id)
    if job.status != "running":
        raise HTTPException(status_code=409, detail=f"Build job is {job.status}")
    job.cancel_requested = True
    session.commit()
    return _job_out(session, job)


@router.get("/workspaces/{workspace_id}/build-summary")
def get_build_summary(workspace_id: int, session: SessionDep) -> BuildSummaryOut:
    """Build status counts over the backlog items that are not dropped, for the count bar."""
    _workspace(session, workspace_id)
    rows = session.execute(
        select(Item.build_status, func.count()).where(Item.workspace_id == workspace_id, Item.dropped.is_(False)).group_by(Item.build_status)
    )
    counts: dict[str | None, int] = {status: count for status, count in rows}
    return BuildSummaryOut(statuses={status: counts.get(status, 0) for status in BUILD_STATUSES}, unbuilt=counts.get(None, 0))


@router.get("/workspaces/{workspace_id}/items/{key}/builds")
def list_item_builds(workspace_id: int, key: str, session: SessionDep) -> list[BuildRunOut]:
    """Every build run that included the item, newest first."""
    _workspace(session, workspace_id)
    item = session.scalar(select(Item).join(JiraIssue, Item.issue_id == JiraIssue.id).where(Item.workspace_id == workspace_id, JiraIssue.key == key))
    if item is None:
        raise HTTPException(status_code=404, detail=f"Not in this workspace's backlog: {key}")
    each = func.json_each(BuildRun.item_ids).table_valued("value")
    runs = list(session.scalars(select(BuildRun).where(exists(select(each.c.value).where(each.c.value == item.id))).order_by(BuildRun.id.desc())))
    keys = _keys(session, [i for run in runs for i in run.item_ids])
    return [
        BuildRunOut(
            id=run.id,
            job_id=run.job_id,
            keys=[keys[i] for i in run.item_ids if i in keys],
            cluster_id=run.cluster_id,
            patch_ids=run.patch_ids,
            tool=run.tool,
            modules=run.modules,
            command=run.command,
            status=run.status,
            summary=run.summary,
            log_url=f"/api/workspaces/{workspace_id}/build-runs/{run.id}/log" if run.log_path and Path(run.log_path).is_file() else None,
            started_at=run.started_at,
            finished_at=run.finished_at,
        )
        for run in runs
    ]


@router.get("/workspaces/{workspace_id}/build-runs/{run_id}/log", response_class=PlainTextResponse)
def get_build_log(workspace_id: int, run_id: int, session: SessionDep) -> PlainTextResponse:
    """The full log of a build run, as plain text."""
    run = session.scalar(
        select(BuildRun).join(BuildJob, BuildRun.job_id == BuildJob.id).where(BuildRun.id == run_id, BuildJob.workspace_id == workspace_id)
    )
    if run is None or not run.log_path or not Path(run.log_path).is_file():
        raise HTTPException(status_code=404, detail="Build log not found")
    return PlainTextResponse(Path(run.log_path).read_text(errors="replace"))
