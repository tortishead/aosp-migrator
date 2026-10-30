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
    # The `repo` checkout whose `.repo/project.list` lists the target tree's projects, when
    # the target tree is a subdirectory of it (`<root>/qssi`). None: the target tree itself.
    repo_root: Mapped[str | None] = mapped_column(String)
    source_tree: Mapped[str | None] = mapped_column(String)
    source_tree_type: Mapped[str | None] = mapped_column(String)
    # Link types whose tickets are followed, matched against the type name ("Relates") or
    # the direction ("relates to", "is blocked by"), case-insensitive. Empty means all.
    link_types: Mapped[list[Any]] = mapped_column(default=list, server_default=text("'[]'"))
    # None: `build/envsetup.sh` of the repo root or target tree (`build.env.envsetup_path`).
    envsetup_path: Mapped[str | None] = mapped_column(String)
    # Required to build.
    lunch_target: Mapped[str | None] = mapped_column(String)
    # Seconds.
    build_timeout: Mapped[int] = mapped_column(default=7200, server_default=text("7200"))
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
    # Result of the item's last finished build run (see build/runner.py); None until built.
    build_status: Mapped[str | None] = mapped_column(String, index=True)  # apply_failed / build_failed / build_successful
    build_reason: Mapped[str | None] = mapped_column(Text)
    built_at: Mapped[datetime | None]
    # Outcome of the item's last finished rework attempt (see rework/attempt.py); None until reworked.
    rework_status: Mapped[str | None] = mapped_column(String, index=True)  # proposed / accepted / rejected / rework_failed
    rework_reason: Mapped[str | None] = mapped_column(Text)
    reworked_at: Mapped[datetime | None]
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


class DiffDependency(Base):
    """A file of a diff written on top of a version (blob) the target project lacks, and the diff that makes it.

    Rebuilt for the whole workspace after each pre-scan, see `prescan.deps`.
    """

    __tablename__ = "diff_dependency"

    id: Mapped[int] = mapped_column(primary_key=True)
    workspace_id: Mapped[int] = mapped_column(ForeignKey("workspace.id", ondelete="CASCADE"), index=True)
    # The diff that needs the blob.
    attachment_id: Mapped[int] = mapped_column(ForeignKey("attachment.id", ondelete="CASCADE"), index=True)
    # Its file, path before the change.
    path: Mapped[str] = mapped_column(String)
    # Preimage blob id as written in the diff's `index` line, maybe abbreviated.
    blob: Mapped[str] = mapped_column(String)
    # The diff of another item whose result is that blob; None when no known diff makes it.
    depends_on_id: Mapped[int | None] = mapped_column(ForeignKey("attachment.id", ondelete="CASCADE"), index=True)
    state: Mapped[str] = mapped_column(String)  # pending / in_target / missing


class PrescanJob(Base):
    __tablename__ = "prescan_job"

    id: Mapped[int] = mapped_column(primary_key=True)
    workspace_id: Mapped[int] = mapped_column(ForeignKey("workspace.id", ondelete="CASCADE"), index=True)
    scope: Mapped[str] = mapped_column(String)  # all / keys
    # Backlog items to scan when scope is `keys`.
    issue_keys: Mapped[list[Any]]
    status: Mapped[str] = mapped_column(String, default="running")
    stage: Mapped[str | None] = mapped_column(String)  # projects / scan / dependencies
    done: Mapped[int] = mapped_column(default=0)
    total: Mapped[int | None]
    # [{key, message}] per failed item; the job itself carries on.
    errors: Mapped[list[Any]]
    # Why the whole job failed (no project list, target tree missing).
    error: Mapped[str | None] = mapped_column(Text)
    cancel_requested: Mapped[bool] = mapped_column(default=False)
    started_at: Mapped[datetime] = mapped_column(default=utcnow)
    finished_at: Mapped[datetime | None]


class MigrationPatch(Base):
    """The patch a build applies: for one diff, as the pre-scan made it (`original`), or for an item's
    project, as a rework made it. Never changed once written, except its review.
    """

    __tablename__ = "migration_patch"

    id: Mapped[int] = mapped_column(primary_key=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("item.id", ondelete="CASCADE"), index=True)
    # The Jira diff it was made from; None for patches not made from one (`ai`, MS5).
    attachment_id: Mapped[int | None] = mapped_column(ForeignKey("attachment.id", ondelete="CASCADE"), index=True)
    # Project path in the target tree, relative to its root.
    project: Mapped[str] = mapped_column(String)
    source: Mapped[str] = mapped_column(String)  # original / ai
    # plain / 3way (`original`); stacked / source3way / resolve / port / reimpl (rework, see `prescan.chain`)
    level: Mapped[str] = mapped_column(String)
    # Project HEAD it was made against; None in a plain tree.
    base_head: Mapped[str | None] = mapped_column(String)
    # Patches of other items it was made on top of, in apply order; empty: on HEAD alone.
    base_patch_ids: Mapped[list[Any]] = mapped_column(default=list, server_default=text("'[]'"))
    # proposed / accepted / rejected; `original` patches are accepted.
    review: Mapped[str] = mapped_column(String, default="accepted", server_default="accepted")
    # Why the user rejected it, for the next rework.
    review_note: Mapped[str | None] = mapped_column(Text)
    # Check and intent findings of a rework, shown in review.
    findings: Mapped[list[Any]] = mapped_column(default=list, server_default=text("'[]'"))
    # The rework attempt that made it; None for `original` patches of the pre-scan.
    attempt_id: Mapped[int | None] = mapped_column(ForeignKey("rework_attempt.id", ondelete="SET NULL"), index=True)
    diff: Mapped[str] = mapped_column(Text)
    # For the format-patch when pushing; None for a plain diff.
    author: Mapped[str | None] = mapped_column(String)
    message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class BuildJob(Base):
    __tablename__ = "build_job"

    id: Mapped[int] = mapped_column(primary_key=True)
    workspace_id: Mapped[int] = mapped_column(ForeignKey("workspace.id", ondelete="CASCADE"), index=True)
    scope: Mapped[str] = mapped_column(String)  # keys / cluster / stacked
    # Eligible items to build, in build order: one unit each (`keys`), or the cluster's members / the stack as one unit.
    issue_keys: Mapped[list[Any]]
    cluster_id: Mapped[int | None] = mapped_column(ForeignKey("cluster.id", ondelete="SET NULL"))
    # [{key, reason}] per selected item that is not built (triage label not `applies` / `3way`; stacked: left out of the stack).
    skipped: Mapped[list[Any]]
    status: Mapped[str] = mapped_column(String, default="running")
    done: Mapped[int] = mapped_column(default=0)
    # Units.
    total: Mapped[int]
    # [{keys, message}] per unit that failed unexpectedly; the job itself carries on.
    errors: Mapped[list[Any]]
    # Why the whole job failed.
    error: Mapped[str | None] = mapped_column(Text)
    cancel_requested: Mapped[bool] = mapped_column(default=False)
    started_at: Mapped[datetime] = mapped_column(default=utcnow)
    finished_at: Mapped[datetime | None]


class BuildRun(Base):
    """One unit of a build job, or one build of a rework attempt: its patches applied, its modules built, the tree restored.
    Kept for every attempt."""

    __tablename__ = "build_run"

    id: Mapped[int] = mapped_column(primary_key=True)
    # One of them: the build job, or the rework attempt whose patch it builds (build-fix loop).
    job_id: Mapped[int | None] = mapped_column(ForeignKey("build_job.id", ondelete="CASCADE"), index=True)
    attempt_id: Mapped[int | None] = mapped_column(ForeignKey("rework_attempt.id", ondelete="CASCADE"), index=True)
    # Several for a cluster, in cluster order; for a stacked build the items that stacked, in stack order.
    item_ids: Mapped[list[Any]]
    cluster_id: Mapped[int | None] = mapped_column(ForeignKey("cluster.id", ondelete="SET NULL"))
    # Migration patches applied; empty for a stacked build (its patches are made on the stack).
    patch_ids: Mapped[list[Any]]
    tool: Mapped[str | None] = mapped_column(String)  # m / mmm; None when nothing was built
    # Module names (`m`) or directories relative to the build root (`mmm`).
    modules: Mapped[list[Any]]
    command: Mapped[str | None] = mapped_column(Text)
    # running / apply_failed / build_failed / build_successful / canceled / interrupted / error
    status: Mapped[str] = mapped_column(String, default="running")
    # First error lines of the build, the apply error, or why it was not built.
    summary: Mapped[str | None] = mapped_column(Text)
    log_path: Mapped[str | None] = mapped_column(String)
    # `build.tree.InFlight` while patches may be in the tree; None (SQL NULL, not JSON null) once restored.
    inflight: Mapped[dict[str, Any] | None] = mapped_column(JSON(none_as_null=True))
    started_at: Mapped[datetime] = mapped_column(default=utcnow)
    finished_at: Mapped[datetime | None]


class ReworkJob(Base):
    """Rework of a selection: its units one after another, prerequisites first (see rework/runner.py)."""

    __tablename__ = "rework_job"

    id: Mapped[int] = mapped_column(primary_key=True)
    workspace_id: Mapped[int] = mapped_column(ForeignKey("workspace.id", ondelete="CASCADE"), index=True)
    # The selected items, as given.
    issue_keys: Mapped[list[Any]]
    # {rungs, include_dependents, include, exclude, together, item_cap, job_cap, build_rounds}
    options: Mapped[dict[str, Any]]
    # Keys per unit to rework, in order.
    units: Mapped[list[Any]]
    # [{key, reason}] per item of the chain that is not reworked (left out, used as base as it is).
    skipped: Mapped[list[Any]]
    status: Mapped[str] = mapped_column(String, default="running")
    done: Mapped[int] = mapped_column(default=0)
    # Units.
    total: Mapped[int]
    # Copilot calls made so far.
    calls: Mapped[int] = mapped_column(default=0)
    # [{keys, message}] per unit that failed unexpectedly; the job itself carries on.
    errors: Mapped[list[Any]]
    error: Mapped[str | None] = mapped_column(Text)
    cancel_requested: Mapped[bool] = mapped_column(default=False)
    started_at: Mapped[datetime] = mapped_column(default=utcnow)
    finished_at: Mapped[datetime | None]


class ReworkAttempt(Base):
    """One rework of an item, or of items reworked together (a unit), over its projects: rungs, Copilot calls, outcome."""

    __tablename__ = "rework_attempt"

    id: Mapped[int] = mapped_column(primary_key=True)
    workspace_id: Mapped[int] = mapped_column(ForeignKey("workspace.id", ondelete="CASCADE"), index=True)
    # The rework job that ran it; None when run on its own.
    job_id: Mapped[int | None] = mapped_column(ForeignKey("rework_job.id", ondelete="SET NULL"), index=True)
    # Several for a unit, in chain order.
    item_ids: Mapped[list[Any]]
    # Project path to its HEAD when the attempt started.
    heads: Mapped[dict[str, Any]] = mapped_column(default=dict)
    # Patches of other items the attempt worked on top of, in apply order.
    base_patch_ids: Mapped[list[Any]] = mapped_column(default=list)
    # Rungs that ran, in order: stack / source / resolve / port.
    rungs: Mapped[list[Any]] = mapped_column(default=list)
    # [{project, path, status, rung, error, notes}]: per file the rung that fixed it, or why it failed.
    files: Mapped[list[Any]] = mapped_column(default=list)
    # [{project, rung, round, build_round, model, status, prompt_path, parts, answer, error, usage, duration}] per Copilot call.
    calls: Mapped[list[Any]] = mapped_column(default=list)
    # Build runs of the build-fix loop, in order.
    build_run_ids: Mapped[list[Any]] = mapped_column(default=list)
    # What the attempt left out or could not do, e.g. a prerequisite reworked without.
    notes: Mapped[list[Any]] = mapped_column(default=list, server_default=text("'[]'"))
    status: Mapped[str] = mapped_column(String, default="running")  # running / patched / failed / canceled / error
    reason: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(default=utcnow)
    finished_at: Mapped[datetime | None]
