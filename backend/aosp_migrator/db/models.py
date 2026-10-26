from datetime import UTC, datetime
from typing import Any, Literal

from sqlalchemy import JSON, DateTime, ForeignKey, String, Text, TypeDecorator, UniqueConstraint, text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

TreeType = Literal["repo", "git", "plain"]
LocalState = Literal["new", "not_relevant", "closed"]
JobStatus = Literal["running", "done", "failed", "canceled"]


def utcnow() -> datetime:
    return datetime.now(UTC)


class UTCDateTime(TypeDecorator[datetime]):
    """SQLite drops tz info: store naive UTC, hand back aware UTC."""

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect) -> datetime | None:
        if value is not None and value.tzinfo is not None:
            value = value.astimezone(UTC).replace(tzinfo=None)
        return value

    def process_result_value(self, value: datetime | None, dialect) -> datetime | None:
        return value.replace(tzinfo=UTC) if value is not None else None


class Base(DeclarativeBase):
    type_annotation_map = {datetime: UTCDateTime, dict[str, Any]: JSON, list[Any]: JSON}


class Workspace(Base):
    __tablename__ = "workspace"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String, unique=True)
    jql: Mapped[str] = mapped_column(Text)
    target_tree: Mapped[str] = mapped_column(String)
    target_tree_type: Mapped[str] = mapped_column(String)
    source_tree: Mapped[str | None] = mapped_column(String)
    source_tree_type: Mapped[str | None] = mapped_column(String)
    # Link types whose tickets are followed, matched against the type name ("Relates") or
    # the direction ("relates to", "is blocked by"), case-insensitive. Empty means all.
    link_types: Mapped[list[Any]] = mapped_column(default=list, server_default=text("'[]'"))
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)


class JiraIssue(Base):
    """A Jira ticket as last imported. Backlog items and linked tickets share this table."""

    __tablename__ = "jira_issue"
    __table_args__ = (UniqueConstraint("workspace_id", "key"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    workspace_id: Mapped[int] = mapped_column(ForeignKey("workspace.id", ondelete="CASCADE"), index=True)
    key: Mapped[str] = mapped_column(String)
    jira_id: Mapped[str] = mapped_column(String)
    summary: Mapped[str] = mapped_column(Text)
    description_raw: Mapped[str] = mapped_column(Text)
    # Rendered HTML with downloaded attachment URLs pointing at the local copies.
    description_html: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String)
    labels: Mapped[list[Any]]
    components: Mapped[list[Any]]
    fix_versions: Mapped[list[Any]]
    # [{link_type, direction, description, key}], as Jira reports them.
    issue_links: Mapped[list[Any]]
    jira_updated: Mapped[datetime]
    fetched_at: Mapped[datetime] = mapped_column(default=utcnow)


class Item(Base):
    """Backlog entry: a ticket matched by the workspace JQL, plus local state."""

    __tablename__ = "item"

    id: Mapped[int] = mapped_column(primary_key=True)
    workspace_id: Mapped[int] = mapped_column(ForeignKey("workspace.id", ondelete="CASCADE"), index=True)
    issue_id: Mapped[int] = mapped_column(ForeignKey("jira_issue.id", ondelete="CASCADE"), unique=True)
    local_state: Mapped[str] = mapped_column(String, default="new")
    dropped: Mapped[bool] = mapped_column(default=False)
    last_import_error: Mapped[str | None] = mapped_column(Text)
    # Pre-scan verdict over the current diffs (see prescan/triage.py); None until scanned or without diffs.
    triage_label: Mapped[str | None] = mapped_column(String, index=True)
    triage_reason: Mapped[str | None] = mapped_column(Text)
    triaged_at: Mapped[datetime | None]
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class ItemLink(Base):
    __tablename__ = "item_link"

    id: Mapped[int] = mapped_column(primary_key=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("item.id", ondelete="CASCADE"), index=True)
    issue_id: Mapped[int] = mapped_column(ForeignKey("jira_issue.id", ondelete="CASCADE"), index=True)
    link_type: Mapped[str] = mapped_column(String)
    direction: Mapped[str] = mapped_column(String)
    # How the item relates to the linked ticket, e.g. "is blocked by".
    description: Mapped[str] = mapped_column(String)


class Comment(Base):
    __tablename__ = "comment"
    __table_args__ = (UniqueConstraint("issue_id", "jira_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    issue_id: Mapped[int] = mapped_column(ForeignKey("jira_issue.id", ondelete="CASCADE"), index=True)
    jira_id: Mapped[str] = mapped_column(String)
    author: Mapped[str] = mapped_column(String)
    created: Mapped[datetime]
    body_html: Mapped[str] = mapped_column(Text)


class Attachment(Base):
    __tablename__ = "attachment"
    __table_args__ = (UniqueConstraint("issue_id", "jira_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    issue_id: Mapped[int] = mapped_column(ForeignKey("jira_issue.id", ondelete="CASCADE"), index=True)
    jira_id: Mapped[str] = mapped_column(String)
    filename: Mapped[str] = mapped_column(String)
    size: Mapped[int]
    mime: Mapped[str] = mapped_column(String)
    kind: Mapped[str] = mapped_column(String)  # diff / image / other
    url: Mapped[str] = mapped_column(String)  # Jira download URL
    local_path: Mapped[str | None] = mapped_column(String)
    sha256: Mapped[str | None] = mapped_column(String)
    # Newest attachment (by Jira `created`) among those with the same filename.
    is_current: Mapped[bool]
    created: Mapped[datetime]
    parsed: Mapped[dict[str, Any] | None]


class Note(Base):
    __tablename__ = "note"

    id: Mapped[int] = mapped_column(primary_key=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("item.id", ondelete="CASCADE"), index=True)
    text: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class HistoryEvent(Base):
    __tablename__ = "history_event"

    id: Mapped[int] = mapped_column(primary_key=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("item.id", ondelete="CASCADE"), index=True)
    type: Mapped[str] = mapped_column(String)
    payload: Mapped[dict[str, Any]]
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class ImportJob(Base):
    __tablename__ = "import_job"

    id: Mapped[int] = mapped_column(primary_key=True)
    workspace_id: Mapped[int] = mapped_column(ForeignKey("workspace.id", ondelete="CASCADE"), index=True)
    scope: Mapped[str] = mapped_column(String)  # all / keys
    # Keys to reimport when scope is `keys`.
    issue_keys: Mapped[list[Any]]
    status: Mapped[str] = mapped_column(String, default="running")
    done: Mapped[int] = mapped_column(default=0)
    total: Mapped[int | None]
    # [{key, message}] per failed ticket; the job itself carries on.
    errors: Mapped[list[Any]]
    # Why the whole job failed (Jira unreachable, auth, search error).
    error: Mapped[str | None] = mapped_column(Text)
    cancel_requested: Mapped[bool] = mapped_column(default=False)
    # Set while the job waits out Jira's rate limit: when the wait ends.
    waiting_until: Mapped[datetime | None]
    started_at: Mapped[datetime] = mapped_column(default=utcnow)
    finished_at: Mapped[datetime | None]


class Cluster(Base):
    """Backlog items the user grouped by hand, to be migrated together."""

    __tablename__ = "cluster"

    id: Mapped[int] = mapped_column(primary_key=True)
    workspace_id: Mapped[int] = mapped_column(ForeignKey("workspace.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String)
    description: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)


class ClusterMember(Base):
    __tablename__ = "cluster_member"

    id: Mapped[int] = mapped_column(primary_key=True)
    cluster_id: Mapped[int] = mapped_column(ForeignKey("cluster.id", ondelete="CASCADE"), index=True)
    # An item is in at most one cluster.
    item_id: Mapped[int] = mapped_column(ForeignKey("item.id", ondelete="CASCADE"), unique=True)
    # Migration order inside the cluster, ascending; gaps are fine.
    position: Mapped[int]


class ClusterNote(Base):
    __tablename__ = "cluster_note"

    id: Mapped[int] = mapped_column(primary_key=True)
    cluster_id: Mapped[int] = mapped_column(ForeignKey("cluster.id", ondelete="CASCADE"), index=True)
    text: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class ClusterEvent(Base):
    __tablename__ = "cluster_event"

    id: Mapped[int] = mapped_column(primary_key=True)
    cluster_id: Mapped[int] = mapped_column(ForeignKey("cluster.id", ondelete="CASCADE"), index=True)
    type: Mapped[str] = mapped_column(String)
    payload: Mapped[dict[str, Any]]
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class DiffScan(Base):
    """Pre-scan results for one diff attachment: repo mapping and already-present check."""

    __tablename__ = "diff_scan"

    id: Mapped[int] = mapped_column(primary_key=True)
    attachment_id: Mapped[int] = mapped_column(ForeignKey("attachment.id", ondelete="CASCADE"), unique=True)
    # Project path in the target tree, relative to its root; None while unmapped.
    project: Mapped[str | None] = mapped_column(String)
    mapping_source: Mapped[str | None] = mapped_column(String)  # hint / files / copilot / manual
    # Projects to choose from when unmapped.
    candidates: Mapped[list[Any]]
    presence: Mapped[str | None] = mapped_column(String)  # present / likely_present / not_present / partial / unknown
    # Per-file reverse-apply results, or the upstream history match.
    presence_detail: Mapped[dict[str, Any] | None]
    # None when present, unmapped or not checked yet.
    apply: Mapped[str | None] = mapped_column(String)  # applies / 3way / conflicts
    # Per-file apply results and why each conflicting file conflicts.
    apply_detail: Mapped[dict[str, Any] | None]
    # HEAD of the mapped project when scanned, to spot stale results.
    target_head: Mapped[str | None] = mapped_column(String)
    scanned_at: Mapped[datetime] = mapped_column(default=utcnow)


class PrescanJob(Base):
    __tablename__ = "prescan_job"

    id: Mapped[int] = mapped_column(primary_key=True)
    workspace_id: Mapped[int] = mapped_column(ForeignKey("workspace.id", ondelete="CASCADE"), index=True)
    scope: Mapped[str] = mapped_column(String)  # all / keys
    # Backlog items to scan when scope is `keys`.
    issue_keys: Mapped[list[Any]]
    status: Mapped[str] = mapped_column(String, default="running")
    stage: Mapped[str | None] = mapped_column(String)  # projects / scan
    done: Mapped[int] = mapped_column(default=0)
    total: Mapped[int | None]
    # [{key, message}] per failed item; the job itself carries on.
    errors: Mapped[list[Any]]
    # Why the whole job failed (no project list, target tree missing).
    error: Mapped[str | None] = mapped_column(Text)
    cancel_requested: Mapped[bool] = mapped_column(default=False)
    started_at: Mapped[datetime] = mapped_column(default=utcnow)
    finished_at: Mapped[datetime | None]
