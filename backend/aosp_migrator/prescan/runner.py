"""Pre-scan jobs: run in a background thread, one at a time per workspace.

A job scans every non-dropped backlog item (`all`) or a list of them (`keys`).
Stage `scan` maps each current diff to a target project and checks whether it is
already present; each item is written in its own transaction, and an item that
fails records an error while the job goes on. Stage `graph` then rebuilds the
workspace-wide candidate graph. Only errors that would hit every item (no
project list, missing tree) fail the whole job.
"""

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from aosp_migrator.db.models import Attachment, DiffScan, ImportJob, Item, JiraIssue, PrescanJob, Workspace, utcnow
from aosp_migrator.importer.diffparse import parse_diff
from aosp_migrator.prescan import git
from aosp_migrator.prescan.graph import read_diff_text, rebuild_graph
from aosp_migrator.prescan.mapping import ProjectListMissing, discover_projects, map_diff, path_hints, read_project_list
from aosp_migrator.prescan.presence import check_presence

log = logging.getLogger(__name__)

# Mapping sources a re-scan keeps: someone (or Copilot) decided, the files did not.
KEPT_SOURCES = ("copilot", "manual")


class JobConflictError(Exception):
    """A pre-scan or an import is already running for the workspace."""


class PrescanError(Exception):
    """The whole job cannot run."""


class _Canceled(Exception):
    pass


def _start_thread(target: Callable[[], None]) -> None:
    threading.Thread(target=target, name="prescan-job", daemon=True).start()


def running_job(session: Session, workspace_id: int) -> PrescanJob | None:
    return session.scalar(select(PrescanJob).where(PrescanJob.workspace_id == workspace_id, PrescanJob.status == "running"))


def fail_interrupted_jobs(session: Session) -> None:
    """Jobs still `running` at startup died with the previous process."""
    for job in session.scalars(select(PrescanJob).where(PrescanJob.status == "running")):
        job.status = "failed"
        job.error = "Interrupted by a server restart"
        job.finished_at = utcnow()
    session.commit()


def current_diffs(session: Session, issue_id: int) -> list[Attachment]:
    """Diff attachments the pre-scan looks at: newest version per filename, downloaded."""
    return list(
        session.scalars(
            select(Attachment)
            .where(Attachment.issue_id == issue_id, Attachment.kind == "diff", Attachment.is_current.is_(True), Attachment.local_path.is_not(None))
            .order_by(Attachment.created, Attachment.id)
        )
    )


def target_projects(workspace: Workspace) -> tuple[Path, list[str]]:
    """Projects of a `repo` tree from its project list; of a `git` or `plain` tree from its `.git` dirs."""
    tree = Path(workspace.target_tree)
    if not tree.is_dir():
        raise PrescanError(f"Target tree not found: {tree}")
    if workspace.target_tree_type != "repo":
        return tree, discover_projects(tree)
    try:
        return tree, read_project_list(tree)
    except ProjectListMissing as exc:
        raise PrescanError(str(exc)) from exc


class PrescanRunner:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        upstream_domains: Callable[[], list[str]],
        executor: Callable[[Callable[[], None]], None] = _start_thread,
    ) -> None:
        self._sessions = sessions
        self._upstream_domains = upstream_domains
        self._executor = executor
        self._lock = threading.Lock()

    def start(self, workspace_id: int, keys: list[str] | None = None) -> PrescanJob:
        """Create the job and run it in the background. `keys` None scans the whole backlog."""
        with self._lock, self._sessions() as session:
            if running_job(session, workspace_id) is not None:
                raise JobConflictError("A pre-scan is already running for this workspace")
            if session.scalar(select(ImportJob.id).where(ImportJob.workspace_id == workspace_id, ImportJob.status == "running")):
                raise JobConflictError("An import is running for this workspace; pre-scan when it is done")
            job = PrescanJob(
                workspace_id=workspace_id,
                scope="all" if keys is None else "keys",
                issue_keys=keys or [],
                total=None if keys is None else len(keys),
                errors=[],
            )
            session.add(job)
            session.commit()
        self._executor(lambda: self.run(job.id))
        return job

    def run(self, job_id: int) -> None:
        with self._sessions() as session:
            job = session.get(PrescanJob, job_id)
            assert job is not None
            try:
                _Job(session, job, self._upstream_domains()).run()
                self._finish(session, job, "done")
            except _Canceled:
                self._finish(session, job, "canceled")
            except PrescanError as exc:
                self._finish(session, job, "failed", str(exc))
            except Exception as exc:
                log.exception("Pre-scan job %s failed", job_id)
                self._finish(session, job, "failed", f"Unexpected error: {exc}")

    @staticmethod
    def _finish(session: Session, job: PrescanJob, status: str, error: str | None = None) -> None:
        session.rollback()
        job.status = status
        job.stage = None
        job.error = error
        job.finished_at = utcnow()
        session.commit()


@dataclass
class _Job:
    session: Session
    job: PrescanJob
    upstream_domains: list[str]

    def run(self) -> None:
        workspace = self.session.get(Workspace, self.job.workspace_id)
        assert workspace is not None
        self.job.stage = "projects"
        self.session.commit()
        tree, projects = target_projects(workspace)

        query = (
            select(Item, JiraIssue)
            .join(JiraIssue, Item.issue_id == JiraIssue.id)
            .where(Item.workspace_id == workspace.id, Item.dropped.is_(False))
            .order_by(Item.id)
        )
        if self.job.scope == "keys":
            query = query.where(JiraIssue.key.in_(self.job.issue_keys))
        items = list(self.session.execute(query))
        self.job.total = len(items)
        self.job.stage = "scan"
        self.session.commit()

        for item, issue in items:
            self._check_cancel()
            try:
                scan_item(self.session, issue, tree, projects, self.upstream_domains)
            except Exception as exc:
                log.exception("Pre-scan of %s failed", issue.key)
                self.session.rollback()
                # Reassign: in-place changes to a JSON column are not tracked.
                self.job.errors = [*self.job.errors, {"key": issue.key, "message": str(exc)}]
            self.job.done += 1
            self.session.commit()

        self._check_cancel()
        self.job.stage = "graph"
        self.session.commit()
        rebuild_graph(self.session, workspace.id)
        self.session.commit()

    def _check_cancel(self) -> None:
        self.session.refresh(self.job, ["cancel_requested"])
        if self.job.cancel_requested:
            raise _Canceled


def scan_item(session: Session, issue: JiraIssue, tree: Path, projects: list[str], upstream_domains: list[str]) -> None:
    """Map and presence-check each current diff of `issue`; results replace its earlier diff scans."""
    hints = path_hints(f"{issue.summary}\n{issue.description_raw}", projects)
    for attachment in current_diffs(session, issue.id):
        text = read_diff_text(attachment)
        if text is None:
            raise PrescanError(f"{attachment.filename}: downloaded file is missing; reimport the ticket")
        scan = session.scalar(select(DiffScan).where(DiffScan.attachment_id == attachment.id))
        if scan is None:
            scan = DiffScan(attachment_id=attachment.id, candidates=[])
            session.add(scan)
        if scan.mapping_source not in KEPT_SOURCES or scan.project is None:
            mapping = map_diff(tree, projects, parse_diff(text).files, hints)
            scan.project, scan.mapping_source, scan.candidates = mapping.project, mapping.source, mapping.candidates
        if scan.project is None:
            scan.presence = scan.presence_detail = scan.target_head = None
        else:
            project_dir = tree / scan.project
            presence = check_presence(project_dir, text, upstream_domains)
            scan.presence, scan.presence_detail = presence.status, presence.detail
            scan.target_head = git.head(project_dir)
        scan.scanned_at = utcnow()
    session.flush()


def stale_reasons(session: Session, issue: JiraIssue, tree: Path) -> list[str]:
    """Why the item's scan results are out of date; empty when fresh or never scanned."""
    attachments = current_diffs(session, issue.id)
    scans = {
        scan.attachment_id: scan
        for scan in session.scalars(select(DiffScan).join(Attachment, DiffScan.attachment_id == Attachment.id).where(Attachment.issue_id == issue.id))
    }
    if not scans:
        return []
    reasons: list[str] = []
    if any(a.id not in scans for a in attachments):
        reasons.append("new diff version")
    heads: dict[str, str | None] = {}
    for attachment in attachments:
        scan = scans.get(attachment.id)
        if scan is None or scan.project is None:
            continue
        if scan.project not in heads:
            heads[scan.project] = git.head(tree / scan.project) if (tree / scan.project).is_dir() else None
        if heads[scan.project] != scan.target_head:
            reasons.append(f"{scan.project} HEAD moved")
    return list(dict.fromkeys(reasons))
