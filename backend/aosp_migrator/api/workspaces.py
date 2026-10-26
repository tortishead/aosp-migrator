import shutil
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from aosp_migrator.api.deps import get_session
from aosp_migrator.config import Config
from aosp_migrator.db.models import Workspace
from aosp_migrator.importer.runner import running_job
from aosp_migrator.prescan import runner as prescan_runner
from aosp_migrator.trees import TreePathError, detect_tree_type, validate_tree_path

router = APIRouter(prefix="/workspaces")

SessionDep = Annotated[Session, Depends(get_session)]


class WorkspaceIn(BaseModel):
    name: str
    jql: str
    target_tree: str
    source_tree: str | None = None
    # Empty: follow every link type.
    link_types: list[str] = []


class WorkspaceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    jql: str
    target_tree: str
    target_tree_type: str
    source_tree: str | None
    source_tree_type: str | None
    link_types: list[str]
    created_at: datetime
    updated_at: datetime


def _normalize_link_types(values: list[str]) -> list[str]:
    """Stripped, without empties or case-insensitive duplicates, in the given order."""
    result: list[str] = []
    for value in (v.strip() for v in values):
        if value and value.casefold() not in {r.casefold() for r in result}:
            result.append(value)
    return result


def _apply(workspace: Workspace, body: WorkspaceIn) -> None:
    name, jql = body.name.strip(), body.jql.strip()
    if not name:
        raise HTTPException(status_code=422, detail="Name is required")
    if not jql:
        raise HTTPException(status_code=422, detail="JQL is required")
    if not body.target_tree.strip():
        raise HTTPException(status_code=422, detail="Target tree is required")
    try:
        target = validate_tree_path(body.target_tree)
    except TreePathError as exc:
        raise HTTPException(status_code=422, detail=f"Target tree {exc}") from exc
    source = None
    if body.source_tree and body.source_tree.strip():
        try:
            source = validate_tree_path(body.source_tree)
        except TreePathError as exc:
            raise HTTPException(status_code=422, detail=f"Source tree {exc}") from exc

    workspace.name = name
    workspace.jql = jql
    workspace.target_tree = str(target)
    workspace.target_tree_type = detect_tree_type(target)
    workspace.source_tree = str(source) if source else None
    workspace.source_tree_type = detect_tree_type(source) if source else None
    workspace.link_types = _normalize_link_types(body.link_types)


def _commit(session: Session, workspace: Workspace) -> None:
    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        raise HTTPException(status_code=409, detail=f"A workspace named {workspace.name!r} already exists") from exc


def _get(session: Session, workspace_id: int) -> Workspace:
    workspace = session.get(Workspace, workspace_id)
    if workspace is None:
        raise HTTPException(status_code=404, detail="Workspace not found")
    return workspace


@router.get("")
def list_workspaces(session: SessionDep) -> list[WorkspaceOut]:
    workspaces = session.scalars(select(Workspace).order_by(Workspace.name)).all()
    return [WorkspaceOut.model_validate(w) for w in workspaces]


@router.post("", status_code=201)
def create_workspace(body: WorkspaceIn, session: SessionDep) -> WorkspaceOut:
    workspace = Workspace()
    _apply(workspace, body)
    session.add(workspace)
    _commit(session, workspace)
    return WorkspaceOut.model_validate(workspace)


@router.get("/{workspace_id}")
def get_workspace(workspace_id: int, session: SessionDep) -> WorkspaceOut:
    return WorkspaceOut.model_validate(_get(session, workspace_id))


@router.put("/{workspace_id}")
def update_workspace(workspace_id: int, body: WorkspaceIn, session: SessionDep) -> WorkspaceOut:
    workspace = _get(session, workspace_id)
    _apply(workspace, body)
    _commit(session, workspace)
    return WorkspaceOut.model_validate(workspace)


@router.delete("/{workspace_id}", status_code=204)
def delete_workspace(workspace_id: int, request: Request, session: SessionDep) -> Response:
    workspace = _get(session, workspace_id)
    if running_job(session, workspace_id) is not None:
        raise HTTPException(status_code=409, detail="An import is running for this workspace; cancel it first")
    if prescan_runner.running_job(session, workspace_id) is not None:
        raise HTTPException(status_code=409, detail="A pre-scan is running for this workspace; cancel it first")
    # Items, issues, jobs etc. go with it via ON DELETE CASCADE.
    session.delete(workspace)
    session.commit()
    config: Config = request.app.state.config
    shutil.rmtree(config.workspace_dir(workspace_id), ignore_errors=True)
    return Response(status_code=204)
