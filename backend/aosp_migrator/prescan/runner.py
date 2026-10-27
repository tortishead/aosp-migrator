"""Pre-scan jobs: run in a background thread, one at a time per workspace.

A job scans every non-dropped backlog item (`all`) or a list of them (`keys`), or
stacks a list of them in the given order (`stacked`, see `_Job.stack`).
Stage `scan` maps each current diff to a target project, checks whether it is
already present and, when not, whether it applies, keeps the migration patch the
apply check made, then sets the item's triage label. Each item is written in its own transaction; an item that fails records an
error (and the label `error`) while the job goes on. Only errors that would hit
every item (no project list, missing tree) fail the whole job. The local state is
never touched.

A stacked job writes nothing but one `stacked_prescan` history event per item: no scan
rows, labels or migration patches.
"""

import logging
import tempfile
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from aosp_migrator.db.models import Attachment, DiffScan, HistoryEvent, ImportJob, Item, JiraIssue, MigrationPatch, PrescanJob, Workspace, utcnow
from aosp_migrator.importer.diffparse import parse_diff
from aosp_migrator.prescan import git
from aosp_migrator.prescan.apply import ApplyCheck, check_apply
from aosp_migrator.prescan.mapping import PROJECT_LIST, ProjectListMissing, discover_projects, map_diff, path_hints, projects_under, read_project_list, repository_hints
from aosp_migrator.prescan.patch import Patch
from aosp_migrator.prescan.presence import check_presence
from aosp_migrator.prescan.stack import Stack, StackDiff
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
    """Projects of a `repo` tree from its project list; of a `git` or `plain` tree from its `.git` dirs.

    With a repo root above the target tree, only the projects under the target tree, relative to it.
    """
    tree = Path(workspace.target_tree)
    if not tree.is_dir():
        raise PrescanError(f"Target tree not found: {tree}")
    if workspace.target_tree_type != "repo":
        return tree, discover_projects(tree)
    root = Path(workspace.repo_root) if workspace.repo_root else tree
    try:
        projects = read_project_list(root)
    except ProjectListMissing as exc:
        raise PrescanError(str(exc)) from exc
    if root == tree:
        return tree, projects
    projects = projects_under(projects, tree.relative_to(root).as_posix())
    if not projects:
        raise PrescanError(f"No project of {root / PROJECT_LIST} is under the target tree {tree}")
    return tree, projects


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

    def start(self, workspace_id: int, keys: list[str] | None = None, stacked: bool = False) -> PrescanJob:
        """Create the job and run it in the background. `keys` None scans the whole backlog; `stacked` stacks `keys` in order."""
        assert not stacked or keys is not None
        with self._lock, self._sessions() as session:
            self._check_free(session, workspace_id)
            job = PrescanJob(
                workspace_id=workspace_id,
                scope="all" if keys is None else "stacked" if stacked else "keys",
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
                    scan_item(session, item, issue, tree, projects, self._upstream_domains())
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

        query = select(Item, JiraIssue).join(JiraIssue, Item.issue_id == JiraIssue.id).where(Item.workspace_id == workspace.id).order_by(Item.id)
        if self.job.scope == "all":
            query = query.where(Item.dropped.is_(False))
        else:
            # Dropped items only when asked for by key: the user was warned.
            query = query.where(JiraIssue.key.in_(self.job.issue_keys))
        items = list(self.session.execute(query))
        self.job.total = len(items)
        self.job.stage = "scan"
        self.session.commit()

        if self.job.scope == "stacked":
            order = {key: n for n, key in enumerate(self.job.issue_keys)}
            self.stack(tree, projects, sorted(items, key=lambda row: order[row[1].key]))
            return
        if self.job.scope == "keys":
            items = [(item, issue) for item, issue in items if not item.dropped]
            self.job.total = len(items)
            self.session.commit()
        for item, issue in items:
            self._check_cancel()
            try:
                scan_item(self.session, item, issue, tree, projects, self.upstream_domains)
                triage_item(self.session, item, issue)
            except Exception as exc:
                log.exception("Pre-scan of %s failed", issue.key)
                self.session.rollback()
                # Reassign: in-place changes to a JSON column are not tracked.
                self.job.errors = [*self.job.errors, {"key": issue.key, "message": str(exc)}]
                _label_error(item, exc)
            self.job.done += 1
            self.session.commit()

    def stack(self, tree: Path, projects: list[str], items: list[tuple[Item, JiraIssue]]) -> None:
        """Stack each item's diffs on the items before it that applied; record the outcome in its history.

        Event `stacked_prescan`: the job's `order`, the item's `position` in it (from 1), `base` (keys stacked below this item),
        `left_out` (earlier keys left out of the stack), `status` (`applied`, `left_out`,
        `no_diffs`, `error`) with `reason`, `heads` of its projects, `attachments` ({key:
        attachment ids} of the base and the item, for staleness), and per diff its
        filename, project, status, error and stacked patch.
        """
        order = [issue.key for _, issue in items]
        base: list[str] = []
        left_out: list[str] = []
        attachments: dict[str, list[int]] = {}
        with tempfile.TemporaryDirectory(prefix="aosp-migrator-stack-") as tmp:
            stack = Stack(tree, Path(tmp))
            for position, (item, issue) in enumerate(items, 1):
                self._check_cancel()
                payload: dict[str, Any] = {
                    "job_id": self.job.id,
                    "order": order,
                    "position": position,
                    "base": list(base),
                    "left_out": list(left_out),
                }
                try:
                    diffs = current_diffs(self.session, issue.id)
                    stacked = stack.add([self._stack_diff(attachment, issue, tree, projects) for attachment in diffs])
                except Exception as exc:
                    log.exception("Stacked pre-scan of %s failed", issue.key)
                    self.session.rollback()
                    self.job.errors = [*self.job.errors, {"key": issue.key, "message": str(exc)}]
                    payload |= {"status": "error", "reason": str(exc), "diffs": []}
                else:
                    own = {key: ids for key, ids in attachments.items() if key in base}
                    own[issue.key] = [a.id for a in diffs]
                    failed = next((d for d in stacked.diffs if d.status in ("conflicts", "unmapped")), None)
                    status = "no_diffs" if not diffs else "applied" if stacked.applied else "left_out"
                    payload |= {
                        "status": status,
                        "reason": f"{failed.filename}: {failed.error}" if failed else None,
                        "heads": {d.project: stack.heads.get(d.project) for d in stacked.diffs if d.project is not None},
                        "attachments": own,
                        "diffs": [
                            {"attachment_id": a.id, "filename": d.filename, "project": d.project, "status": d.status, "error": d.error, "patch": d.patch}
                            for a, d in zip(diffs, stacked.diffs, strict=True)
                        ],
                    }
                    if status == "applied":
                        base.append(issue.key)
                        attachments[issue.key] = own[issue.key]
                    elif status == "left_out":
                        left_out.append(issue.key)
                self.session.add(HistoryEvent(item_id=item.id, type="stacked_prescan", payload=payload))
                self.job.done += 1
                self.session.commit()

    def _stack_diff(self, attachment: Attachment, issue: JiraIssue, tree: Path, projects: list[str]) -> StackDiff:
        """The diff's project as last scanned (else mapped now) and its files already in that project."""
        text = read_diff_text(attachment)
        if text is None:
            raise PrescanError(f"{attachment.filename}: downloaded file is missing; reimport the ticket")
        scan = diff_scan(self.session, attachment.id)
        project = scan.project if scan is not None else None
        if project is None:
            description = f"{issue.summary}\n{issue.description_raw}"
            hints, repository = path_hints(description, projects), repository_hints(description, projects)
            project = map_diff(tree, projects, parse_diff(text).files, hints, repository).project
        if project is None:
            return StackDiff(attachment.filename, None, text)
        presence = check_presence(tree / project, text, self.upstream_domains)
        present = [s.path for s in Patch.parse(text).files] if presence.status == "present" else presence.files("present")
        return StackDiff(attachment.filename, project, text, present)

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


def scan_item(session: Session, item: Item, issue: JiraIssue, tree: Path, projects: list[str], upstream_domains: list[str]) -> None:
    """Map, presence- and apply-check each current diff of `issue`; results replace its earlier diff scans.

    A diff that applies (as is or 3-way) gets a migration patch, unless its newest one is the same.
    """
    description = f"{issue.summary}\n{issue.description_raw}"
    hints = path_hints(description, projects)
    repository = repository_hints(description, projects)
    for attachment in current_diffs(session, issue.id):
        text = read_diff_text(attachment)
        if text is None:
            raise PrescanError(f"{attachment.filename}: downloaded file is missing; reimport the ticket")
        scan = _scan_row(session, attachment.id)
        if scan.mapping_source not in KEPT_SOURCES or scan.project is None:
            mapping = map_diff(tree, projects, parse_diff(text).files, hints, repository)
            scan.project, scan.mapping_source, scan.candidates = mapping.project, mapping.source, mapping.candidates
        if scan.project is None:
            scan.presence = scan.presence_detail = scan.apply = scan.apply_detail = scan.target_head = None
        else:
            project_dir = tree / scan.project
            presence = check_presence(project_dir, text, upstream_domains)
            scan.presence, scan.presence_detail = presence.status, presence.detail
            scan.apply = scan.apply_detail = None
            scan.target_head = git.head(project_dir)
            if presence.status != "present":
                applied = check_apply(project_dir, text, presence.files("present"))
                scan.apply, scan.apply_detail = applied.status, applied.detail
                _keep_patch(session, item, attachment, scan, applied, text)
        scan.scanned_at = utcnow()
    session.flush()


def _keep_patch(session: Session, item: Item, attachment: Attachment, scan: DiffScan, applied: ApplyCheck, text: str) -> None:
    if applied.patch is None:
        return
    assert scan.project is not None
    level = "plain" if applied.status == "applies" else "3way"
    newest = _newest_patch(session, attachment.id)
    if newest is not None and (newest.project, newest.level, newest.base_head, newest.diff) == (scan.project, level, scan.target_head, applied.patch):
        return
    parsed = Patch.parse(text)
    session.add(
        MigrationPatch(
            item_id=item.id,
            attachment_id=attachment.id,
            project=scan.project,
            source="original",
            level=level,
            base_head=scan.target_head,
            diff=applied.patch,
            author=parsed.author,
            message=parsed.message,
        )
    )


def _newest_patch(session: Session, attachment_id: int) -> MigrationPatch | None:
    return session.scalar(
        select(MigrationPatch)
        .where(MigrationPatch.attachment_id == attachment_id, MigrationPatch.source == "original")
        .order_by(MigrationPatch.id.desc())
        .limit(1)
    )


def current_patch(session: Session, scan: DiffScan | None) -> MigrationPatch | None:
    """The migration patch of a diff as last scanned: None unless it applies, as is or 3-way."""
    if scan is None or scan.apply not in ("applies", "3way"):
        return None
    patch = _newest_patch(session, scan.attachment_id)
    if patch is None or (patch.project, patch.base_head) != (scan.project, scan.target_head):
        return None
    return patch


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


def stacked_stale(session: Session, workspace_id: int, tree: Path, payload: dict[str, Any]) -> list[str]:
    """Why a `stacked_prescan` event's result is out of date: a stacked item's diffs changed or a HEAD moved."""
    recorded: dict[str, list[int]] = payload.get("attachments", {})
    reasons: list[str] = []
    if recorded:
        rows = session.execute(
            select(JiraIssue.key, Attachment.id)
            .join(Attachment, Attachment.issue_id == JiraIssue.id)
            .where(JiraIssue.workspace_id == workspace_id, JiraIssue.key.in_(recorded), Attachment.kind == "diff", Attachment.is_current.is_(True), Attachment.local_path.is_not(None))
        )
        current: dict[str, set[int]] = {key: set() for key in recorded}
        for key, attachment_id in rows:
            current[key].add(attachment_id)
        if any(current[key] != set(ids) for key, ids in recorded.items()):
            reasons.append("new diff version")
    for project, head in payload.get("heads", {}).items():
        if (git.head(tree / project) if (tree / project).is_dir() else None) != head:
            reasons.append(f"{project} HEAD moved")
    return reasons
