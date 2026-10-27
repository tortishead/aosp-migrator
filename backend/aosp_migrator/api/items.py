from datetime import datetime
from pathlib import Path
from typing import Annotated, Any, Literal
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import ColumnElement, Integer, Select, cast, column, exists, func, literal_column, or_, select, table
from sqlalchemy.orm import Session, aliased

from aosp_migrator.api.deps import get_session
from aosp_migrator.api.build import BUILD_STATUSES
from aosp_migrator.config import JIRA_BASE_URL, Config
from aosp_migrator.db.models import (
    Attachment,
    Cluster,
    ClusterMember,
    Comment,
    DiffScan,
    HistoryEvent,
    Item,
    ItemLink,
    JiraIssue,
    LocalState,
    Note,
    Workspace,
)
from aosp_migrator.prescan.runner import stale_items
from aosp_migrator.prescan.triage import ORDER

router = APIRouter(prefix="/workspaces/{workspace_id}/items")

SessionDep = Annotated[Session, Depends(get_session)]
ListParam = Annotated[list[str] | None, Query()]

# Maintained by triggers, see migration 0003. Not a model: SQLAlchemy only needs to name it.
issue_fts = table("issue_fts", column("rowid"), column("key"), column("summary"), column("description"), column("diff_paths"))
# Trigram FTS cannot match terms shorter than this; those fall back to LIKE.
TRIGRAM = 3

Sort = Literal["key", "-key", "updated", "-updated", "status", "-status", "state", "-state"]
Group = Literal["project"]


def natural_key(key: str) -> tuple[str, int]:
    """PROJ-9 before PROJ-10."""
    prefix, _, number = key.rpartition("-")
    return (prefix, int(number)) if number.isdigit() else (key, 0)


class ClusterRef(BaseModel):
    id: int
    name: str


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
    # Commit subjects from the format-patch headers of the current diffs.
    diff_subjects: list[str]
    # Distinct target projects the current diffs are mapped to, sorted; empty until mapped.
    target_projects: list[str]
    cluster: ClusterRef | None
    # Pre-scan triage label and why; None until scanned, and for items without diffs.
    triage_label: str | None
    triage_reason: str | None
    # Why the pre-scan results are out of date; empty when fresh or never scanned.
    stale: list[str]
    # Result of the last finished build and why; None until built.
    build_status: str | None
    build_reason: str | None


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


class CommentOut(BaseModel):
    id: int
    author: str
    created: datetime
    body_html: str


class AttachmentOut(BaseModel):
    id: int
    jira_id: str
    filename: str
    size: int
    mime: str
    kind: str
    # Jira download URL, and where this server serves the downloaded copy (None if not downloaded).
    jira_url: str
    local_url: str | None
    is_current: bool
    created: datetime
    parsed: dict[str, Any] | None


class IssueOut(BaseModel):
    key: str
    summary: str
    description_html: str
    status: str
    labels: list[str]
    components: list[str]
    fix_versions: list[str]
    jira_updated: datetime
    fetched_at: datetime
    # Browse URL in Jira; None while no Jira base URL is set.
    browse_url: str | None
    comments: list[CommentOut]
    attachments: list[AttachmentOut]


class LinkOut(BaseModel):
    link_type: str
    direction: str
    description: str
    # Set when the linked ticket is a backlog item of this workspace too.
    item_id: int | None
    # The linked backlog item's cluster.
    cluster: ClusterRef | None
    issue: IssueOut


class NoteOut(BaseModel):
    id: int
    text: str
    created_at: datetime


class HistoryOut(BaseModel):
    id: int
    type: str
    payload: dict[str, Any]
    created_at: datetime


class SiblingOut(BaseModel):
    key: str
    summary: str
    local_state: str


class ItemCluster(ClusterRef):
    # All members including this item, in migration order.
    members: list[SiblingOut]


class ItemDetail(BaseModel):
    id: int
    local_state: str
    dropped: bool
    last_import_error: str | None
    created_at: datetime
    # For relative Jira links inside the rendered HTML.
    jira_base_url: str | None
    issue: IssueOut
    cluster: ItemCluster | None
    links: list[LinkOut]
    notes: list[NoteOut]
    # Oldest first.
    history: list[HistoryOut]


class NoteIn(BaseModel):
    text: str = Field(min_length=1)


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


def _in_cluster(cluster: str) -> ColumnElement[bool]:
    """`any`: in some cluster, `none`: in no cluster, a number: in that cluster."""
    # Aliased: the list query also joins cluster_member, which would swallow the correlation.
    joined = aliased(ClusterMember)
    member = select(joined.id).where(joined.item_id == Item.id)
    if cluster == "any":
        return exists(member)
    if cluster == "none":
        return ~exists(member)
    if cluster.isdigit():
        return exists(member.where(joined.cluster_id == int(cluster)))
    raise HTTPException(status_code=422, detail="cluster must be any, none or a cluster id")


def _triage(labels: list[str]) -> ColumnElement[bool]:
    """Any of the triage labels; `none` matches items without one."""
    if unknown := [label for label in labels if label != "none" and label not in ORDER]:
        raise HTTPException(status_code=422, detail=f"Unknown triage label: {', '.join(unknown)}")
    known = [label for label in labels if label != "none"]
    condition = Item.triage_label.in_(known)
    return or_(condition, Item.triage_label.is_(None)) if "none" in labels else condition


def _build(statuses: list[str]) -> ColumnElement[bool]:
    """Any of the build statuses; `none` matches items never built."""
    if unknown := [s for s in statuses if s != "none" and s not in BUILD_STATUSES]:
        raise HTTPException(status_code=422, detail=f"Unknown build status: {', '.join(unknown)}")
    condition = Item.build_status.in_([s for s in statuses if s != "none"])
    return or_(condition, Item.build_status.is_(None)) if "none" in statuses else condition


def _project_group() -> Any:
    """The item's target projects, sorted and joined with ", "; NULL while none is mapped."""
    projects = (
        select(DiffScan.project)
        .distinct()
        .join(Attachment, DiffScan.attachment_id == Attachment.id)
        .where(Attachment.issue_id == JiraIssue.id, Attachment.kind == "diff", Attachment.is_current.is_(True), DiffScan.project.is_not(None))
        .order_by(DiffScan.project)
        .correlate(JiraIssue)
        .subquery()
    )
    return select(func.group_concat(projects.c.project, ", ")).scalar_subquery()


def _group_key(group: Group | None) -> list[Any]:
    if group is None:
        return []
    projects = _project_group()
    return [projects.is_(None), projects]


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
    has_error: bool | None = None,
    cluster: str | None = None,
    triage: ListParam = None,
    stale: bool | None = None,
    build: ListParam = None,
    sort: Sort = "key",
    group: Group | None = None,
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
) -> ItemPage:
    """Backlog items. Filters combine with AND; several values of one filter match any of them.

    `group=project` orders by target projects first (items without one last), then by `sort`.
    """
    workspace = _workspace(session, workspace_id)
    tree = Path(workspace.target_tree)
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
    if has_error is not None:
        conditions.append(Item.last_import_error.is_not(None) if has_error else Item.last_import_error.is_(None))
    if cluster:
        conditions.append(_in_cluster(cluster))
    if triage:
        conditions.append(_triage(triage))
    if build:
        conditions.append(_build(build))
    if stale is not None:
        # Needs every scanned item's HEADs, so only when asked for.
        stale_ids = list(stale_items(session, workspace_id, tree))
        conditions.append(Item.id.in_(stale_ids) if stale else Item.id.not_in(stale_ids))
    if q:
        conditions.extend(_text_term(term) for term in q.split())

    query = _backlog(workspace_id).where(*conditions)
    total = session.scalar(select(func.count()).select_from(query.subquery())) or 0
    diff_count = (
        select(func.count(Attachment.id))
        .where(Attachment.issue_id == JiraIssue.id, Attachment.kind == "diff", Attachment.is_current.is_(True))
        .scalar_subquery()
    )
    rows = session.execute(
        query.add_columns(diff_count, Cluster.id, Cluster.name)
        .outerjoin(ClusterMember, ClusterMember.item_id == Item.id)
        .outerjoin(Cluster, ClusterMember.cluster_id == Cluster.id)
        .order_by(*_group_key(group), *_sort_key(sort))
        .offset(offset)
        .limit(limit)
    )
    rows = list(rows)
    subjects = _diff_subjects(session, [issue.id for _, issue, *_ in rows])
    projects = _target_projects(session, [issue.id for _, issue, *_ in rows])
    stale_reasons = stale_items(session, workspace_id, tree, [item.id for item, *_ in rows])
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
            diff_subjects=subjects.get(issue.id, []),
            target_projects=projects.get(issue.id, []),
            cluster=ClusterRef(id=cluster_id, name=cluster_name) if cluster_id is not None else None,
            triage_label=item.triage_label,
            triage_reason=item.triage_reason,
            stale=stale_reasons.get(item.id, []),
            build_status=item.build_status,
            build_reason=item.build_reason,
        )
        for item, issue, count, cluster_id, cluster_name in rows
    ]
    return ItemPage(items=items, total=total, offset=offset, limit=limit)


def _diff_subjects(session: Session, issue_ids: list[int]) -> dict[int, list[str]]:
    """Distinct commit subjects of the current diffs per issue, in attachment order."""
    subjects: dict[int, list[str]] = {}
    current = session.execute(
        select(Attachment.issue_id, Attachment.parsed)
        .where(Attachment.issue_id.in_(issue_ids), Attachment.kind == "diff", Attachment.is_current.is_(True))
        .order_by(Attachment.created, Attachment.id)
    )
    for issue_id, parsed in current:
        seen = subjects.setdefault(issue_id, [])
        for commit in (parsed or {}).get("commits", []):
            if commit.get("subject") and commit["subject"] not in seen:
                seen.append(commit["subject"])
    return subjects


def _target_projects(session: Session, issue_ids: list[int]) -> dict[int, list[str]]:
    """Distinct mapped target projects of the current diffs per issue, sorted."""
    projects: dict[int, list[str]] = {}
    mapped = session.execute(
        select(Attachment.issue_id, DiffScan.project)
        .distinct()
        .join(DiffScan, DiffScan.attachment_id == Attachment.id)
        .where(Attachment.issue_id.in_(issue_ids), Attachment.kind == "diff", Attachment.is_current.is_(True), DiffScan.project.is_not(None))
        .order_by(DiffScan.project)
    )
    for issue_id, project in mapped:
        projects.setdefault(issue_id, []).append(project)
    return projects


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



def _local_url(workspace_id: int, attachment: Attachment) -> str | None:
    if attachment.local_path is None:
        return None
    return f"/api/workspaces/{workspace_id}/attachments/{attachment.jira_id}/{quote(Path(attachment.local_path).name)}"


def _issue_out(session: Session, issue: JiraIssue, base_url: str | None) -> IssueOut:
    comments = session.scalars(select(Comment).where(Comment.issue_id == issue.id).order_by(Comment.created, Comment.id))
    attachments = session.scalars(select(Attachment).where(Attachment.issue_id == issue.id).order_by(Attachment.created, Attachment.id))
    return IssueOut(
        key=issue.key,
        summary=issue.summary,
        description_html=issue.description_html,
        status=issue.status,
        labels=issue.labels,
        components=issue.components,
        fix_versions=issue.fix_versions,
        jira_updated=issue.jira_updated,
        fetched_at=issue.fetched_at,
        browse_url=f"{base_url.rstrip('/')}/browse/{quote(issue.key)}" if base_url else None,
        comments=[CommentOut(id=c.id, author=c.author, created=c.created, body_html=c.body_html) for c in comments],
        attachments=[
            AttachmentOut(
                id=a.id,
                jira_id=a.jira_id,
                filename=a.filename,
                size=a.size,
                mime=a.mime,
                kind=a.kind,
                jira_url=a.url,
                local_url=_local_url(issue.workspace_id, a),
                is_current=a.is_current,
                created=a.created,
                parsed=a.parsed,
            )
            for a in attachments
        ],
    )


def _cluster_of(session: Session, item_id: int) -> Cluster | None:
    return session.scalar(select(Cluster).join(ClusterMember, ClusterMember.cluster_id == Cluster.id).where(ClusterMember.item_id == item_id))


def _cluster_ref(session: Session, item_id: int | None) -> ClusterRef | None:
    cluster = _cluster_of(session, item_id) if item_id is not None else None
    return ClusterRef(id=cluster.id, name=cluster.name) if cluster else None


def _item_cluster(session: Session, item_id: int) -> ItemCluster | None:
    cluster = _cluster_of(session, item_id)
    if cluster is None:
        return None
    members = session.execute(
        select(Item, JiraIssue)
        .join(JiraIssue, Item.issue_id == JiraIssue.id)
        .join(ClusterMember, ClusterMember.item_id == Item.id)
        .where(ClusterMember.cluster_id == cluster.id)
        .order_by(ClusterMember.position, ClusterMember.id)
    )
    return ItemCluster(
        id=cluster.id,
        name=cluster.name,
        members=[SiblingOut(key=issue.key, summary=issue.summary, local_state=item.local_state) for item, issue in members],
    )


def _item(session: Session, workspace_id: int, key: str) -> tuple[Item, JiraIssue]:
    _workspace(session, workspace_id)
    row = session.execute(_backlog(workspace_id).where(JiraIssue.key == key)).one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail=f"Not in this workspace's backlog: {key}")
    item, issue = row
    return item, issue


@router.get("/{key}")
def get_item(workspace_id: int, key: str, request: Request, session: SessionDep) -> ItemDetail:
    """One backlog item with its ticket, linked tickets, notes and history."""
    item, issue = _item(session, workspace_id, key)
    config: Config = request.app.state.config
    base_url = config.get(JIRA_BASE_URL)
    links = session.execute(
        select(ItemLink, JiraIssue, Item.id)
        .join(JiraIssue, ItemLink.issue_id == JiraIssue.id)
        .outerjoin(Item, Item.issue_id == JiraIssue.id)
        .where(ItemLink.item_id == item.id)
        .order_by(ItemLink.id)
    )
    notes = session.scalars(select(Note).where(Note.item_id == item.id).order_by(Note.created_at, Note.id))
    history = session.scalars(select(HistoryEvent).where(HistoryEvent.item_id == item.id).order_by(HistoryEvent.created_at, HistoryEvent.id))
    return ItemDetail(
        id=item.id,
        local_state=item.local_state,
        dropped=item.dropped,
        last_import_error=item.last_import_error,
        created_at=item.created_at,
        jira_base_url=base_url,
        issue=_issue_out(session, issue, base_url),
        cluster=_item_cluster(session, item.id),
        links=[
            LinkOut(
                link_type=link.link_type,
                direction=link.direction,
                description=link.description,
                item_id=linked_item_id,
                cluster=_cluster_ref(session, linked_item_id),
                issue=_issue_out(session, linked, base_url),
            )
            for link, linked, linked_item_id in links
        ],
        notes=[NoteOut(id=n.id, text=n.text, created_at=n.created_at) for n in notes],
        history=[HistoryOut(id=h.id, type=h.type, payload=h.payload, created_at=h.created_at) for h in history],
    )


@router.post("/{key}/notes", status_code=201)
def add_note(workspace_id: int, key: str, body: NoteIn, session: SessionDep) -> NoteOut:
    """Append a note. Notes are never edited or deleted; each one also goes into the history."""
    text = body.text.strip()
    if not text:
        raise HTTPException(status_code=422, detail="Note is empty")
    item, _ = _item(session, workspace_id, key)
    note = Note(item_id=item.id, text=text)
    session.add(note)
    session.flush()
    session.add(HistoryEvent(item_id=item.id, type="note_added", payload={"note_id": note.id, "text": text}))
    session.commit()
    return NoteOut(id=note.id, text=note.text, created_at=note.created_at)
