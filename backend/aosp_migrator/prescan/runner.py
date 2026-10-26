"""Pre-scan jobs: run in a background thread, one at a time per workspace.

A job scans every non-dropped backlog item (`all`) or a list of them (`keys`).
Stage `scan` maps each current diff to a target project, checks whether it is
already present and, when not, whether it applies, then sets the item's triage
label. Each item is written in its own transaction; an item that fails records an
error (and the label `error`) while the job goes on. Only errors that would hit
every item (no project list, missing tree) fail the whole job. The local state is
never touched.
"""

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from aosp_migrator.db.models import (
    Attachment,
    DiffScan,
    HistoryEvent,
    ImportJob,
    Item,
    JiraIssue,
    PrescanJob,
    Workspace,
    utcnow,
)
from aosp_migrator.importer.diffparse import parse_diff
from aosp_migrator.prescan import git
from aosp_migrator.prescan.apply import check_apply
from aosp_migrator.prescan.mapping import (
    ProjectListMissing,
    discover_projects,
    map_diff,
    path_hints,
    read_project_list,
)
from aosp_migrator.prescan.presence import check_presence
from aosp_migrator.prescan.triage import Triage, diff_triage, item_triage

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


def relabel_items(session: Session) -> None:
    """Label every scanned item again from its stored scans, so a change of the triage rules needs no re-scan.

    Items labeled `error` keep it: they have no complete scan to label from.
    """
    rows = session.execute(
        select(Item, JiraIssue).join(JiraIssue, Item.issue_id == JiraIssue.id).where(Item.triage_label.is_not(None), Item.triage_label != "error")
    )
    for item, issue in rows:
        triage_item(session, item, issue, keep_time=True)
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
            self._check_free(session, workspace_id)
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

    def pick_project(self, workspace_id: int, item_id: int, attachment_id: int, project: str | None) -> None:
        """Map one diff to `project` by hand (None: back to automatic mapping) and re-scan its item right away.

        Runs under the start lock, so no job starts meanwhile. A failing scan keeps the pick and labels the item `error`.
        """
        with self._lock, self._sessions() as session:
            self._check_free(session, workspace_id)
            workspace = session.get(Workspace, workspace_id)
            item = session.get(Item, item_id)
            assert workspace is not None and item is not None
            issue = session.get(JiraIssue, item.issue_id)
            assert issue is not None
            tree, projects = target_projects(workspace)
            if project is not None and project not in projects:
                raise PrescanError(f"Not a project of the target tree: {project}")
            attachment = session.get(Attachment, attachment_id)
            assert attachment is not None
            scan = _scan_row(session, attachment_id)
            old = scan.project
            # Without a kept source, the scan maps the diff again.
            scan.project, scan.mapping_source = project, "manual" if project is not None else None
            session.add(
                HistoryEvent(item_id=item.id, type="project_picked", payload={"filename": attachment.filename, "old": old, "new": project})
            )
            session.flush()
            try:
                with session.begin_nested():
                    scan_item(session, issue, tree, projects, self._upstream_domains())
                    triage_item(session, item, issue)
            except Exception as exc:
                log.exception("Pre-scan of %s failed", issue.key)
                _label_error(item, exc)
            session.commit()

    @staticmethod
    def _check_free(session: Session, workspace_id: int) -> None:
        if running_job(session, workspace_id) is not None:
            raise JobConflictError("A pre-scan is already running for this workspace")
        if session.scalar(select(ImportJob.id).where(ImportJob.workspace_id == workspace_id, ImportJob.status == "running")):
            raise JobConflictError("An import is running for this workspace; pre-scan when it is done")

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
                triage_item(self.session, item, issue)
            except Exception as exc:
                log.exception("Pre-scan of %s failed", issue.key)
                self.session.rollback()
                # Reassign: in-place changes to a JSON column are not tracked.
                self.job.errors = [*self.job.errors, {"key": issue.key, "message": str(exc)}]
                _label_error(item, exc)
            self.job.done += 1
            self.session.commit()

    def _check_cancel(self) -> None:
        self.session.refresh(self.job, ["cancel_requested"])
        if self.job.cancel_requested:
            raise _Canceled


def read_diff_text(attachment: Attachment) -> str | None:
    if attachment.local_path is None:
        return None
    try:
        with open(attachment.local_path, "rb") as f:
            return f.read().decode("utf-8", errors="replace")
    except OSError:
        return None


def scan_item(session: Session, issue: JiraIssue, tree: Path, projects: list[str], upstream_domains: list[str]) -> None:
    """Map, presence- and apply-check each current diff of `issue`; results replace its earlier diff scans."""
    hints = path_hints(f"{issue.summary}\n{issue.description_raw}", projects)
    for attachment in current_diffs(session, issue.id):
        text = read_diff_text(attachment)
        if text is None:
            raise PrescanError(f"{attachment.filename}: downloaded file is missing; reimport the ticket")
        scan = _scan_row(session, attachment.id)
        if scan.mapping_source not in KEPT_SOURCES or scan.project is None:
            mapping = map_diff(tree, projects, parse_diff(text).files, hints)
            scan.project, scan.mapping_source, scan.candidates = mapping.project, mapping.source, mapping.candidates
        if scan.project is None:
            scan.presence = scan.presence_detail = scan.apply = scan.apply_detail = scan.target_head = None
        else:
            project_dir = tree / scan.project
            presence = check_presence(project_dir, text, upstream_domains)
            scan.presence, scan.presence_detail = presence.status, presence.detail
            scan.apply = scan.apply_detail = None
            if presence.status != "present":
                applied = check_apply(project_dir, text, presence.files("present"))
                scan.apply, scan.apply_detail = applied.status, applied.detail
            scan.target_head = git.head(project_dir)
        scan.scanned_at = utcnow()
    session.flush()


def diff_scan(session: Session, attachment_id: int) -> DiffScan | None:
    return session.scalar(select(DiffScan).where(DiffScan.attachment_id == attachment_id))


def _scan_row(session: Session, attachment_id: int) -> DiffScan:
    scan = diff_scan(session, attachment_id)
    if scan is None:
        scan = DiffScan(attachment_id=attachment_id, candidates=[])
        session.add(scan)
    return scan


def triage_item(session: Session, item: Item, issue: JiraIssue, keep_time: bool = False) -> None:
    """Label `item` from the scans of its current diffs; None when it has none or one is not scanned yet.

    `keep_time`: leave `triaged_at` alone unless the label or its reason changes.
    """
    triages: list[Triage] = []
    for attachment in current_diffs(session, issue.id):
        scan = diff_scan(session, attachment.id)
        if scan is None:
            triages = []
            break
        triages.append(diff_triage(scan, attachment.filename))
    triage = item_triage(triages)
    label, reason = (triage.label, triage.reason) if triage else (None, None)
    if keep_time and (item.triage_label, item.triage_reason) == (label, reason):
        return
    item.triage_label, item.triage_reason, item.triaged_at = label, reason, utcnow()


def _label_error(item: Item, exc: Exception) -> None:
    item.triage_label, item.triage_reason, item.triaged_at = "error", str(exc), utcnow()


def stale_items(session: Session, workspace_id: int, tree: Path, item_ids: list[int] | None = None) -> dict[int, list[str]]:
    """Why each scanned item's results are out of date (all items, or `item_ids`); fresh or never scanned items are left out."""
    query = (
        select(Item.id, Attachment, DiffScan)
        .join(Attachment, Attachment.issue_id == Item.issue_id)
        .outerjoin(DiffScan, DiffScan.attachment_id == Attachment.id)
        .where(Item.workspace_id == workspace_id, Attachment.kind == "diff")
        .order_by(Item.id, Attachment.created, Attachment.id)
    )
    if item_ids is not None:
        query = query.where(Item.id.in_(item_ids))
    rows: dict[int, list[tuple[Attachment, DiffScan | None]]] = {}
    for item_id, attachment, found in session.execute(query):
        rows.setdefault(item_id, []).append((attachment, found))

    heads: dict[str, str | None] = {}
    stale: dict[int, list[str]] = {}
    for item_id, attachments in rows.items():
        if all(scan is None for _, scan in attachments):
            continue
        reasons: list[str] = []
        current = [scan for a, scan in attachments if a.is_current and a.local_path is not None]
        if None in current:
            reasons.append("new diff version")
        for scan in current:
            if scan is None or scan.project is None:
                continue
            if scan.project not in heads:
                heads[scan.project] = git.head(tree / scan.project) if (tree / scan.project).is_dir() else None
            if heads[scan.project] != scan.target_head:
                reasons.append(f"{scan.project} HEAD moved")
        if reasons:
            stale[item_id] = list(dict.fromkeys(reasons))
    return stale
