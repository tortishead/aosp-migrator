from datetime import datetime
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.orm import Session

from aosp_migrator.api.deps import get_session
from aosp_migrator.config import JIRA_BASE_URL, JIRA_PAT, Config
from aosp_migrator.db.models import Attachment, ImportJob, Item, JiraIssue, Workspace
from aosp_migrator.importer.diffparse import parse_diff
from aosp_migrator.importer.runner import ImportRunner, JobConflictError

router = APIRouter()

SessionDep = Annotated[Session, Depends(get_session)]


class ImportRequest(BaseModel):
    # Omitted: import everything the workspace JQL matches. Otherwise reimport these backlog items.
    keys: list[str] | None = None


class ImportJobOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    workspace_id: int
    scope: str
    issue_keys: list[str]
    status: str
    done: int
    total: int | None
    errors: list[dict[str, Any]]
    error: str | None
    cancel_requested: bool
    started_at: datetime
    finished_at: datetime | None


def _workspace(session: Session, workspace_id: int) -> Workspace:
    workspace = session.get(Workspace, workspace_id)
    if workspace is None:
        raise HTTPException(status_code=404, detail="Workspace not found")
    return workspace


def _job(session: Session, job_id: int) -> ImportJob:
    job = session.get(ImportJob, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Import job not found")
    return job


@router.post("/workspaces/{workspace_id}/import", status_code=202)
def start_import(workspace_id: int, body: ImportRequest, request: Request, session: SessionDep) -> ImportJobOut:
    _workspace(session, workspace_id)
    config: Config = request.app.state.config
    if not config.get(JIRA_BASE_URL) or not config.get(JIRA_PAT):
        raise HTTPException(status_code=422, detail="Set the Jira base URL and PAT in the settings first")

    keys = None
    if body.keys is not None:
        keys = list(dict.fromkeys(key.strip() for key in body.keys if key.strip()))
        if not keys:
            raise HTTPException(status_code=422, detail="No ticket keys given")
        known = set(
            session.scalars(
                select(JiraIssue.key).join(Item, Item.issue_id == JiraIssue.id).where(Item.workspace_id == workspace_id, JiraIssue.key.in_(keys))
            )
        )
        if unknown := [key for key in keys if key not in known]:
            raise HTTPException(status_code=404, detail=f"Not in this workspace's backlog: {', '.join(unknown)}")

    runner: ImportRunner = request.app.state.import_runner
    try:
        job = runner.start(workspace_id, keys)
    except JobConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return ImportJobOut.model_validate(job)


@router.get("/workspaces/{workspace_id}/import-jobs")
def list_import_jobs(workspace_id: int, session: SessionDep, limit: int = 20) -> list[ImportJobOut]:
    _workspace(session, workspace_id)
    jobs = session.scalars(select(ImportJob).where(ImportJob.workspace_id == workspace_id).order_by(ImportJob.id.desc()).limit(limit))
    return [ImportJobOut.model_validate(job) for job in jobs]


@router.get("/import-jobs/{job_id}")
def get_import_job(job_id: int, session: SessionDep) -> ImportJobOut:
    return ImportJobOut.model_validate(_job(session, job_id))


@router.post("/import-jobs/{job_id}/cancel", status_code=202)
def cancel_import_job(job_id: int, session: SessionDep) -> ImportJobOut:
    """Ask the job to stop; it does so before the next ticket."""
    job = _job(session, job_id)
    if job.status != "running":
        raise HTTPException(status_code=409, detail=f"Import job is {job.status}")
    job.cancel_requested = True
    session.commit()
    return ImportJobOut.model_validate(job)


def _downloaded(session: Session, workspace_id: int, jira_id: str) -> tuple[Attachment, Path]:
    attachment = session.scalar(
        select(Attachment)
        .join(JiraIssue, Attachment.issue_id == JiraIssue.id)
        .where(JiraIssue.workspace_id == workspace_id, Attachment.jira_id == jira_id, Attachment.local_path.is_not(None))
    )
    if attachment is None or attachment.local_path is None or not Path(attachment.local_path).is_file():
        raise HTTPException(status_code=404, detail="Attachment not found")
    return attachment, Path(attachment.local_path)


@router.get("/workspaces/{workspace_id}/diffs/{jira_id}")
def get_diff(workspace_id: int, jira_id: str, session: SessionDep) -> dict[str, Any]:
    """A downloaded diff attachment, parsed, with the raw text of each hunk for the diff viewer."""
    attachment, path = _downloaded(session, workspace_id, jira_id)
    if attachment.kind != "diff":
        raise HTTPException(status_code=404, detail="Attachment is not a diff")
    return parse_diff(path.read_bytes().decode("utf-8", errors="replace")).to_json(hunks=True)


@router.get("/workspaces/{workspace_id}/attachments/{jira_id}/{filename}")
def get_attachment(workspace_id: int, jira_id: str, filename: str, session: SessionDep) -> FileResponse:
    """A downloaded attachment. The filename is cosmetic; the Jira id identifies it."""
    attachment, path = _downloaded(session, workspace_id, jira_id)
    # Attachments are untrusted content (an SVG could carry script): never let them run as a page of this origin.
    headers = {"Content-Security-Policy": "sandbox", "X-Content-Type-Options": "nosniff"}
    return FileResponse(path, media_type=attachment.mime, headers=headers)
