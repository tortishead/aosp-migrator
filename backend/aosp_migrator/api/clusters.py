from collections import defaultdict
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from aosp_migrator.api.deps import get_session
from aosp_migrator.api.items import HistoryOut, NoteIn, NoteOut, _backlog, _workspace, natural_key
from aosp_migrator.db.models import (
    Attachment,
    Cluster,
    ClusterEvent,
    ClusterMember,
    ClusterNote,
    HistoryEvent,
    Item,
    JiraIssue,
    utcnow,
)

router = APIRouter(prefix="/workspaces/{workspace_id}/clusters")

SessionDep = Annotated[Session, Depends(get_session)]
# A cluster needs this many members; fewer dissolves it.
MIN_MEMBERS = 2


class MemberOut(BaseModel):
    item_id: int
    key: str
    summary: str
    status: str
    local_state: str
    dropped: bool
    diff_count: int


class ClusterOut(BaseModel):
    id: int
    name: str
    description: str
    created_at: datetime
    updated_at: datetime
    # Migration order.
    members: list[MemberOut]


class FileOut(BaseModel):
    path: str
    # Member keys whose current diffs touch the path (also as a rename source), in member order.
    touched_by: list[str]


class ClusterDetail(ClusterOut):
    files: list[FileOut]
    notes: list[NoteOut]
    # Oldest first.
    history: list[HistoryOut]


class ClusterIn(BaseModel):
    keys: list[str]
    # Default: first member's key and summary.
    name: str | None = None
    description: str = ""
    # Moving items out of a cluster that would keep fewer than two members dissolves it.
    confirm_dissolve: bool = False


class ClusterUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1)
    description: str | None = None


class MembersIn(BaseModel):
    keys: list[str]
    confirm_dissolve: bool = False


class OrderIn(BaseModel):
    keys: list[str]


class RemoveOut(BaseModel):
    dissolved: bool


Row = tuple[Item, JiraIssue]


def _items(session: Session, workspace_id: int, keys: list[str]) -> list[Row]:
    """Backlog items for `keys`, deduplicated, in the given order; 404 if any key is not one."""
    keys = list(dict.fromkeys(keys))
    found = {issue.key: (item, issue) for item, issue in session.execute(_backlog(workspace_id).where(JiraIssue.key.in_(keys)))}
    if unknown := [key for key in keys if key not in found]:
        raise HTTPException(status_code=404, detail=f"Not in this workspace's backlog: {', '.join(unknown)}")
    return [found[key] for key in keys]


def _default_order(session: Session, rows: list[Row]) -> list[Row]:
    """Oldest current diff first, then key; items without a diff last."""
    first_diff = dict(
        session.execute(
            select(Attachment.issue_id, func.min(Attachment.created))
            .where(Attachment.issue_id.in_([issue.id for _, issue in rows]), Attachment.kind == "diff", Attachment.is_current.is_(True))
            .group_by(Attachment.issue_id)
        ).all()
    )
    return sorted(rows, key=lambda row: (row[1].id not in first_diff, first_diff.get(row[1].id) or datetime.min, natural_key(row[1].key)))


def _cluster(session: Session, workspace_id: int, cluster_id: int) -> Cluster:
    _workspace(session, workspace_id)
    cluster = session.get(Cluster, cluster_id)
    if cluster is None or cluster.workspace_id != workspace_id:
        raise HTTPException(status_code=404, detail="Cluster not found")
    return cluster


def _members(session: Session, cluster_id: int) -> list[tuple[ClusterMember, Item, JiraIssue]]:
    rows = session.execute(
        select(ClusterMember, Item, JiraIssue)
        .join(Item, ClusterMember.item_id == Item.id)
        .join(JiraIssue, Item.issue_id == JiraIssue.id)
        .where(ClusterMember.cluster_id == cluster_id)
        .order_by(ClusterMember.position, ClusterMember.id)
    )
    return [(member, item, issue) for member, item, issue in rows]


def _event(session: Session, cluster: Cluster, type_: str, **payload: Any) -> None:
    session.add(ClusterEvent(cluster_id=cluster.id, type=type_, payload=payload))
    cluster.updated_at = utcnow()


def _dissolve(session: Session, cluster: Cluster) -> None:
    """Delete the cluster; its remaining items stay in the backlog and get a history entry."""
    for _, item, _ in _members(session, cluster.id):
        session.add(HistoryEvent(item_id=item.id, type="cluster_left", payload={"cluster_id": cluster.id, "name": cluster.name, "dissolved": True}))
    session.execute(delete(ClusterMember).where(ClusterMember.cluster_id == cluster.id))
    session.delete(cluster)
    session.flush()


def _add(session: Session, cluster: Cluster, rows: list[Row], confirm_dissolve: bool) -> list[str]:
    """Put items into `cluster`, taking them out of any other cluster. Returns the added keys."""
    current = {m.item_id: m for m in session.scalars(select(ClusterMember).where(ClusterMember.item_id.in_([item.id for item, _ in rows])))}
    moving = [(item, issue) for item, issue in rows if item.id not in current or current[item.id].cluster_id != cluster.id]
    leaving: dict[int, list[Row]] = defaultdict(list)
    for item, issue in moving:
        if item.id in current:
            leaving[current[item.id].cluster_id].append((item, issue))

    old_clusters = {cid: session.get_one(Cluster, cid) for cid in leaving}
    to_dissolve = [old_clusters[cid] for cid, gone in leaving.items() if len(_members(session, cid)) - len(gone) < MIN_MEMBERS]
    if to_dissolve and not confirm_dissolve:
        names = ", ".join(repr(c.name) for c in to_dissolve)
        raise HTTPException(status_code=409, detail=f"This would leave fewer than {MIN_MEMBERS} items in {names} and dissolve it")

    for cid, gone in leaving.items():
        old = old_clusters[cid]
        for item, issue in gone:
            session.delete(current[item.id])
            session.add(HistoryEvent(item_id=item.id, type="cluster_left", payload={"cluster_id": old.id, "name": old.name, "moved_to": cluster.name}))
            _event(session, old, "member_removed", key=issue.key, moved_to=cluster.name)
    session.flush()
    for old in to_dissolve:
        _dissolve(session, old)

    position = session.scalar(select(func.max(ClusterMember.position)).where(ClusterMember.cluster_id == cluster.id))
    position = -1 if position is None else position
    ordered = _default_order(session, moving)
    for item, _ in ordered:
        position += 1
        session.add(ClusterMember(cluster_id=cluster.id, item_id=item.id, position=position))
        session.add(HistoryEvent(item_id=item.id, type="cluster_joined", payload={"cluster_id": cluster.id, "name": cluster.name}))
    return [issue.key for _, issue in ordered]


def _diff_counts(session: Session, issue_ids: list[int]) -> dict[int, int]:
    rows = session.execute(
        select(Attachment.issue_id, func.count(Attachment.id))
        .where(Attachment.issue_id.in_(issue_ids), Attachment.kind == "diff", Attachment.is_current.is_(True))
        .group_by(Attachment.issue_id)
    )
    return dict(rows.all())


def _out(session: Session, cluster: Cluster) -> ClusterOut:
    members = _members(session, cluster.id)
    counts = _diff_counts(session, [issue.id for _, _, issue in members])
    return ClusterOut(
        id=cluster.id,
        name=cluster.name,
        description=cluster.description,
        created_at=cluster.created_at,
        updated_at=cluster.updated_at,
        members=[
            MemberOut(
                item_id=item.id,
                key=issue.key,
                summary=issue.summary,
                status=issue.status,
                local_state=item.local_state,
                dropped=item.dropped,
                diff_count=counts.get(issue.id, 0),
            )
            for _, item, issue in members
        ],
    )


def _files(session: Session, cluster: Cluster) -> list[FileOut]:
    touched: dict[str, list[str]] = defaultdict(list)
    for _, _, issue in _members(session, cluster.id):
        diffs = session.scalars(
            select(Attachment).where(Attachment.issue_id == issue.id, Attachment.kind == "diff", Attachment.is_current.is_(True))
        )
        paths = {
            path
            for diff in diffs
            for change in (diff.parsed or {}).get("files", [])
            for path in (change["path"], change.get("old_path"))
            if path
        }
        for path in paths:
            touched[path].append(issue.key)
    return [FileOut(path=path, touched_by=keys) for path, keys in sorted(touched.items())]


def _detail(session: Session, cluster: Cluster) -> ClusterDetail:
    notes = session.scalars(select(ClusterNote).where(ClusterNote.cluster_id == cluster.id).order_by(ClusterNote.created_at, ClusterNote.id))
    history = session.scalars(select(ClusterEvent).where(ClusterEvent.cluster_id == cluster.id).order_by(ClusterEvent.created_at, ClusterEvent.id))
    return ClusterDetail(
        **_out(session, cluster).model_dump(),
        files=_files(session, cluster),
        notes=[NoteOut(id=n.id, text=n.text, created_at=n.created_at) for n in notes],
        history=[HistoryOut(id=h.id, type=h.type, payload=h.payload, created_at=h.created_at) for h in history],
    )


@router.get("")
def list_clusters(workspace_id: int, session: SessionDep) -> list[ClusterOut]:
    _workspace(session, workspace_id)
    clusters = session.scalars(select(Cluster).where(Cluster.workspace_id == workspace_id).order_by(func.lower(Cluster.name), Cluster.id))
    return [_out(session, c) for c in clusters]


@router.post("", status_code=201)
def create_cluster(workspace_id: int, body: ClusterIn, session: SessionDep) -> ClusterDetail:
    """Group items into a new cluster. Items already in another cluster move here."""
    _workspace(session, workspace_id)
    rows = _items(session, workspace_id, body.keys)
    if len(rows) < MIN_MEMBERS:
        raise HTTPException(status_code=422, detail=f"A cluster needs at least {MIN_MEMBERS} items")
    first = _default_order(session, rows)[0][1]
    name = (body.name or "").strip() or f"{first.key} {first.summary}"
    cluster = Cluster(workspace_id=workspace_id, name=name, description=body.description.strip())
    session.add(cluster)
    session.flush()
    keys = _add(session, cluster, rows, body.confirm_dissolve)
    _event(session, cluster, "created", keys=keys)
    session.commit()
    return _detail(session, cluster)


@router.get("/{cluster_id}")
def get_cluster(workspace_id: int, cluster_id: int, session: SessionDep) -> ClusterDetail:
    return _detail(session, _cluster(session, workspace_id, cluster_id))


@router.patch("/{cluster_id}")
def update_cluster(workspace_id: int, cluster_id: int, body: ClusterUpdate, session: SessionDep) -> ClusterDetail:
    cluster = _cluster(session, workspace_id, cluster_id)
    if body.name is not None:
        name = body.name.strip()
        if not name:
            raise HTTPException(status_code=422, detail="Name is empty")
        if name != cluster.name:
            _event(session, cluster, "renamed", old=cluster.name, new=name)
            cluster.name = name
    if body.description is not None:
        description = body.description.strip()
        if description != cluster.description:
            _event(session, cluster, "description_changed", old=cluster.description, new=description)
            cluster.description = description
    session.commit()
    return _detail(session, cluster)


@router.delete("/{cluster_id}", status_code=204)
def dissolve_cluster(workspace_id: int, cluster_id: int, session: SessionDep) -> Response:
    """Dissolve the cluster. Its items stay in the backlog; notes and cluster history go with it."""
    _dissolve(session, _cluster(session, workspace_id, cluster_id))
    session.commit()
    return Response(status_code=204)


@router.post("/{cluster_id}/members")
def add_members(workspace_id: int, cluster_id: int, body: MembersIn, session: SessionDep) -> ClusterDetail:
    """Append items, in default order. Items in another cluster move here."""
    cluster = _cluster(session, workspace_id, cluster_id)
    rows = _items(session, workspace_id, body.keys)
    if not rows:
        raise HTTPException(status_code=422, detail="No ticket keys given")
    if keys := _add(session, cluster, rows, body.confirm_dissolve):
        _event(session, cluster, "members_added", keys=keys)
    session.commit()
    return _detail(session, cluster)


@router.delete("/{cluster_id}/members/{key}")
def remove_member(workspace_id: int, cluster_id: int, key: str, session: SessionDep, confirm_dissolve: bool = False) -> RemoveOut:
    """Take one item out. Leaving fewer than two members dissolves the cluster (409 unless confirmed)."""
    cluster = _cluster(session, workspace_id, cluster_id)
    members = _members(session, cluster.id)
    found = next(((m, item) for m, item, issue in members if issue.key == key), None)
    if found is None:
        raise HTTPException(status_code=404, detail=f"{key} is not in this cluster")
    member, item = found
    dissolve = len(members) - 1 < MIN_MEMBERS
    if dissolve and not confirm_dissolve:
        raise HTTPException(status_code=409, detail=f"This would leave fewer than {MIN_MEMBERS} items in {cluster.name!r} and dissolve it")
    session.delete(member)
    session.add(HistoryEvent(item_id=item.id, type="cluster_left", payload={"cluster_id": cluster.id, "name": cluster.name}))
    _event(session, cluster, "member_removed", key=key)
    session.flush()
    if dissolve:
        _dissolve(session, cluster)
    session.commit()
    return RemoveOut(dissolved=dissolve)


@router.put("/{cluster_id}/order")
def reorder(workspace_id: int, cluster_id: int, body: OrderIn, session: SessionDep) -> ClusterDetail:
    """Set the migration order. `keys` must list every member exactly once."""
    cluster = _cluster(session, workspace_id, cluster_id)
    members = {issue.key: member for member, _, issue in _members(session, cluster.id)}
    if len(body.keys) != len(set(body.keys)) or set(body.keys) != set(members):
        raise HTTPException(status_code=422, detail="Order must list every member of the cluster exactly once")
    if body.keys != list(members):
        for position, key in enumerate(body.keys):
            members[key].position = position
        _event(session, cluster, "reordered", keys=body.keys)
    session.commit()
    return _detail(session, cluster)


@router.post("/{cluster_id}/notes", status_code=201)
def add_note(workspace_id: int, cluster_id: int, body: NoteIn, session: SessionDep) -> NoteOut:
    """Append a note. Notes are never edited or deleted; each one also goes into the cluster history."""
    text = body.text.strip()
    if not text:
        raise HTTPException(status_code=422, detail="Note is empty")
    cluster = _cluster(session, workspace_id, cluster_id)
    note = ClusterNote(cluster_id=cluster.id, text=text)
    session.add(note)
    session.flush()
    _event(session, cluster, "note_added", note_id=note.id, text=text)
    session.commit()
    return NoteOut(id=note.id, text=note.text, created_at=note.created_at)
