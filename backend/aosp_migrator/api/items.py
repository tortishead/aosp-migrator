from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import ColumnElement, Integer, Select, cast, column, exists, func, literal_column, or_, select, table
from sqlalchemy.orm import Session

from aosp_migrator.api.deps import get_session
from aosp_migrator.db.models import Attachment, HistoryEvent, Item, JiraIssue, LocalState, Workspace

router = APIRouter(prefix="/workspaces/{workspace_id}/items")

SessionDep = Annotated[Session, Depends(get_session)]
ListParam = Annotated[list[str] | None, Query()]

# Maintained by triggers, see migration 0003. Not a model: SQLAlchemy only needs to name it.
issue_fts = table("issue_fts", column("rowid"), column("key"), column("summary"), column("description"), column("diff_paths"))
# Trigram FTS cannot match terms shorter than this; those fall back to LIKE.
TRIGRAM = 3

Sort = Literal["key", "-key", "updated", "-updated", "status", "-status", "state", "-state"]


class ItemOut(BaseModel):
    id: int
    key: str
    summary: str
    status: str
    labels: list[str]
    components: list[str]
    fix_versions: list[str]
    jira_updated: datetime
    local_state: str
    dropped: bool
    last_import_error: str | None
    # Current diff attachments (one per filename).
    diff_count: int


class ItemPage(BaseModel):
    items: list[ItemOut]
    total: int
    offset: int
    limit: int


class Facets(BaseModel):
    statuses: list[str]
    labels: list[str]
    components: list[str]
    fix_versions: list[str]


class StateChange(BaseModel):
    keys: list[str]
    state: LocalState


class StateChangeOut(BaseModel):
    # Keys whose state actually changed; the others already had it.
    changed: list[str]


def _workspace(session: Session, workspace_id: int) -> Workspace:
    workspace = session.get(Workspace, workspace_id)
    if workspace is None:
        raise HTTPException(status_code=404, detail="Workspace not found")
    return workspace


def _backlog(workspace_id: int) -> Select[Any]:
    return select(Item, JiraIssue).join(JiraIssue, Item.issue_id == JiraIssue.id).where(Item.workspace_id == workspace_id)


def _any_of(json_column: Any, values: list[str]) -> ColumnElement[bool]:
    """The JSON list column contains at least one of `values`."""
    each = func.json_each(json_column).table_valued("value")
    return exists(select(each.c.value).where(each.c.value.in_(values)))


def _text_term(term: str) -> ColumnElement[bool]:
    if len(term) >= TRIGRAM:
        query = '"' + term.replace('"', '""') + '"'
        matches = select(issue_fts.c.rowid).where(literal_column("issue_fts").op("MATCH")(query))
    else:
        pattern = "%" + term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        columns = (issue_fts.c.key, issue_fts.c.summary, issue_fts.c.description, issue_fts.c.diff_paths)
        matches = select(issue_fts.c.rowid).where(or_(*(c.like(pattern, escape="\\") for c in columns)))
    return JiraIssue.id.in_(matches)


def _has_diff() -> ColumnElement[bool]:
    return exists(select(Attachment.id).where(Attachment.issue_id == JiraIssue.id, Attachment.kind == "diff"))


def _sort_key(sort: Sort) -> list[Any]:
    name = sort.removeprefix("-")
    if name == "key":
        # Natural order: PROJ-9 before PROJ-10.
        dash = func.instr(JiraIssue.key, "-")
        columns: list[Any] = [func.substr(JiraIssue.key, 1, dash - 1), cast(func.substr(JiraIssue.key, dash + 1), Integer)]
    else:
        columns = [{"updated": JiraIssue.jira_updated, "status": JiraIssue.status, "state": Item.local_state}[name]]
    if sort.startswith("-"):
        columns = [c.desc() for c in columns]
    return [*columns, Item.id]


@router.get("")
def list_items(
    workspace_id: int,
    session: SessionDep,
    q: str | None = None,
    state: ListParam = None,
    status: ListParam = None,
    label: ListParam = None,
    component: ListParam = None,
    fix_version: ListParam = None,
    dropped: bool | None = None,
    has_diff: bool | None = None,
    sort: Sort = "key",
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
) -> ItemPage:
    """Backlog items. Filters combine with AND; several values of one filter match any of them."""
    _workspace(session, workspace_id)
    conditions: list[ColumnElement[bool]] = []
    if state:
        conditions.append(Item.local_state.in_(state))
    if status:
        conditions.append(JiraIssue.status.in_(status))
    if label:
        conditions.append(_any_of(JiraIssue.labels, label))
    if component:
        conditions.append(_any_of(JiraIssue.components, component))
    if fix_version:
        conditions.append(_any_of(JiraIssue.fix_versions, fix_version))
    if dropped is not None:
        conditions.append(Item.dropped.is_(dropped))
    if has_diff is not None:
        conditions.append(_has_diff() if has_diff else ~_has_diff())
    if q:
        conditions.extend(_text_term(term) for term in q.split())

    query = _backlog(workspace_id).where(*conditions)
    total = session.scalar(select(func.count()).select_from(query.subquery())) or 0
    diff_count = (
        select(func.count(Attachment.id))
        .where(Attachment.issue_id == JiraIssue.id, Attachment.kind == "diff", Attachment.is_current.is_(True))
        .scalar_subquery()
    )
    rows = session.execute(query.add_columns(diff_count).order_by(*_sort_key(sort)).offset(offset).limit(limit))
    items = [
        ItemOut(
            id=item.id,
            key=issue.key,
            summary=issue.summary,
            status=issue.status,
            labels=issue.labels,
            components=issue.components,
            fix_versions=issue.fix_versions,
            jira_updated=issue.jira_updated,
            local_state=item.local_state,
            dropped=item.dropped,
            last_import_error=item.last_import_error,
            diff_count=count,
        )
        for item, issue, count in rows
    ]
    return ItemPage(items=items, total=total, offset=offset, limit=limit)


@router.get("/facets")
def item_facets(workspace_id: int, session: SessionDep) -> Facets:
    """Values for the filter dropdowns, over backlog items only (not linked-only tickets)."""
    _workspace(session, workspace_id)
    issues = [issue for _, issue in session.execute(_backlog(workspace_id))]

    def values(name: str) -> list[str]:
        return sorted({value for issue in issues for value in getattr(issue, name)}, key=str.casefold)

    return Facets(
        statuses=sorted({issue.status for issue in issues}, key=str.casefold),
        labels=values("labels"),
        components=values("components"),
        fix_versions=values("fix_versions"),
    )


@router.post("/state")
def change_state(workspace_id: int, body: StateChange, session: SessionDep) -> StateChangeOut:
    """Set the local state of several items; each change goes into the item's history."""
    _workspace(session, workspace_id)
    keys = list(dict.fromkeys(body.keys))
    if not keys:
        raise HTTPException(status_code=422, detail="No ticket keys given")
    found = {issue.key: item for item, issue in session.execute(_backlog(workspace_id).where(JiraIssue.key.in_(keys)))}
    if unknown := [key for key in keys if key not in found]:
        raise HTTPException(status_code=404, detail=f"Not in this workspace's backlog: {', '.join(unknown)}")
    changed = []
    for key in keys:
        item = found[key]
        if item.local_state != body.state:
            session.add(HistoryEvent(item_id=item.id, type="state_changed", payload={"old": item.local_state, "new": body.state}))
            item.local_state = body.state
            changed.append(key)
    session.commit()
    return StateChangeOut(changed=changed)

