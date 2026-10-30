"""Build jobs: run in a background thread, one at a time per workspace, user-started only.

A job builds units one after another: each selected item on its own (`keys`), or a
cluster's members together in cluster order (`cluster`). Only items labeled `applies` or
`3way`, or with an accepted rework patch, are built; the others are listed as skipped
when the job starts. A stacked build (`stacked`) builds the selected items as one unit,
in the order given, whatever their label: their current patches (else their diffs) are
stacked as in a stacked pre-scan (see `_Job._stacked_patches`).
Per unit:

1. Patches: the item's current patches (`chain.current_patches`), each after its base
   patches. A stale item (new diff version, project HEAD moved) without a rework patch is
   pre-scanned again first; when it no longer applies the unit is `apply_failed`. A stale
   rework patch is applied as it is, 3-way on whatever HEAD. Stacked: each item's patches
   or diffs on a scratch index on top of the items before it (plain, else 3-way); an item
   that does not stack is left out of the unit (`apply_failed`, listed as skipped) and the
   rest build without it.
2. `tree.prepare` (refuses a dirty project before anything is touched), the in-flight
   record goes into the run, then the patches are applied (`--3way` when a project gets
   more than one).
3. Affected modules of the changed files, with the users of modules whose header, export or
   API changed (capped) and the API check target (see `modules`); nothing to build counts
   as successful.
4. `bash -c 'source <envsetup> && lunch <target> && m|mmm <targets>'` in the build root,
   in its own process group, output into `<builds dir>/<run id>.log`. Cancel and
   timeout kill the whole group.
5. `tree.restore`, then the status of every item of the unit. A canceled unit keeps the
   items' previous status.

A failing unit does not stop the job. At startup, running jobs are marked failed and
every unit still in flight is restored (`recover_inflight`).
"""

import logging
import os
import shlex
import signal
import subprocess
import tempfile
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TextIO

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from aosp_migrator.build import tree as target
from aosp_migrator.build.env import build_root, envsetup_path
from aosp_migrator.build.modules import DEFAULT_DEPENDENTS_CAP, AffectedModules, affected_modules, changed_files, widening_files
from aosp_migrator.db.models import BuildJob, BuildRun, ClusterMember, HistoryEvent, Item, JiraIssue, MigrationPatch, Workspace, utcnow
from aosp_migrator.importer.diffparse import parse_diff
from aosp_migrator.prescan.chain import base_patches, current_patches, is_rework, patch_plan, stale_items
from aosp_migrator.prescan.runner import scan_item, stack_diff, target_projects, triage_item
from aosp_migrator.prescan.stack import Stack, StackDiff

log = logging.getLogger(__name__)

ELIGIBLE = ("applies", "3way")
# Seconds between cancel checks while the build runs.
POLL = 0.5
# Error lines kept in a run's summary.
SUMMARY_LINES = 20


class BuildJobConflict(Exception):
    """A build is already running for the workspace."""


class BuildError(Exception):
    """The job cannot start or run at all."""


class _Canceled(Exception):
    pass


class _UnitFailed(Exception):
    """The unit ends with `status` before anything was built."""

    def __init__(self, status: str, summary: str) -> None:
        super().__init__(summary)
        self.status = status
        self.summary = summary


def _start_thread(target: Callable[[], None]) -> None:
    threading.Thread(target=target, name="build-job", daemon=True).start()


def running_job(session: Session, workspace_id: int) -> BuildJob | None:
    return session.scalar(select(BuildJob).where(BuildJob.workspace_id == workspace_id, BuildJob.status == "running"))


def fail_interrupted_jobs(session: Session) -> None:
    """Jobs still `running` at startup died with the previous process."""
    for job in session.scalars(select(BuildJob).where(BuildJob.status == "running")):
        job.status = "failed"
        job.error = "Interrupted by a server restart"
        job.finished_at = utcnow()
    session.commit()


def recover_inflight(session: Session) -> None:
    """Restore every unit the previous process left in the tree; runs still `running` become `interrupted`.

    A restore that fails keeps the record, so the next start tries again.
    """
    for run in session.scalars(select(BuildRun).where(BuildRun.status == "running")):
        run.status = "interrupted"
        run.summary = "Interrupted by a server restart"
        run.finished_at = utcnow()
    session.commit()
    for run, workspace in session.execute(
        select(BuildRun, Workspace).join(BuildJob, BuildRun.job_id == BuildJob.id).join(Workspace).where(BuildRun.inflight.is_not(None))
    ):
        assert run.inflight is not None
        try:
            target.restore(Path(workspace.target_tree), target.InFlight.from_json(run.inflight))
        except Exception:
            log.exception("Restoring the tree of build run %s failed", run.id)
            continue
        log.warning("Restored the target tree after interrupted build run %s", run.id)
        run.inflight = None
        for item_id in run.item_ids:
            session.add(HistoryEvent(item_id=item_id, type="tree_restored", payload={"run_id": run.id}))
        session.commit()


def build_command(envsetup: Path, lunch_target: str, modules: AffectedModules) -> str:
    targets = " ".join(shlex.quote(t) for t in modules.targets)
    return f"source {shlex.quote(str(envsetup))} && lunch {shlex.quote(lunch_target)} && {modules.tool} {targets}"


def error_summary(log_path: Path) -> str:
    """`FAILED:` / `error:` lines of a build log, the first few; without any, its last lines."""
    errors: list[str] = []
    tail: deque[str] = deque(maxlen=SUMMARY_LINES // 2)
    with log_path.open(errors="replace") as f:
        for line in f:
            line = line.rstrip()
            tail.append(line)
            if len(errors) < SUMMARY_LINES and ("FAILED:" in line or "error:" in line):
                errors.append(line)
    return "\n".join(errors or tail)


@dataclass
class _Unit:
    items: list[tuple[Item, JiraIssue]]
    cluster_id: int | None = None
    stacked: bool = False
    patches: list[MigrationPatch] = field(default_factory=list)

    @property
    def keys(self) -> list[str]:
        return [issue.key for _, issue in self.items]


class BuildRunner:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        builds_dir: Callable[[int], Path],
        upstream_domains: Callable[[], list[str]],
        executor: Callable[[Callable[[], None]], None] = _start_thread,
        kill_grace: float = 10.0,
        dependents_cap: Callable[[], int] = lambda: DEFAULT_DEPENDENTS_CAP,
    ) -> None:
        self._sessions = sessions
        self._builds_dir = builds_dir
        self._upstream_domains = upstream_domains
        self._dependents_cap = dependents_cap
        self._executor = executor
        # Seconds between SIGTERM and SIGKILL to the build's process group.
        self._kill_grace = kill_grace
        self._lock = threading.Lock()

    def start(self, workspace_id: int, keys: list[str] | None = None, cluster_id: int | None = None, stacked: bool = False) -> BuildJob:
        """Create the job for `keys` (one unit each, or `stacked` in order as one) or a cluster (one unit) and run it in the background."""
        assert (keys is None) != (cluster_id is None)
        assert not stacked or keys is not None
        with self._lock, self._sessions() as session:
            if running_job(session, workspace_id) is not None:
                raise BuildJobConflict("A build is already running for this workspace")
            workspace = session.get(Workspace, workspace_id)
            assert workspace is not None
            _check_environment(workspace)
            selected = _cluster_items(session, cluster_id) if cluster_id is not None else _items(session, workspace_id, keys or [])
            # Stacked: the stack decides, as in a stacked pre-scan; an item may only apply on the ones before it.
            reasons = {issue.key: None if stacked else _not_buildable(session, item) for item, issue in selected}
            eligible = [key for key, reason in reasons.items() if reason is None]
            skipped = [{"key": key, "reason": reason} for key, reason in reasons.items() if reason is not None]
            job = BuildJob(
                workspace_id=workspace_id,
                scope="cluster" if cluster_id is not None else "stacked" if stacked else "keys",
                issue_keys=eligible,
                cluster_id=cluster_id,
                skipped=skipped,
                total=len(eligible) if cluster_id is None and not stacked else int(bool(eligible)),
                errors=[],
            )
            session.add(job)
            session.commit()
        self._executor(lambda: self.run(job.id))
        return job

    def run(self, job_id: int) -> None:
        with self._sessions() as session:
            job = session.get(BuildJob, job_id)
            assert job is not None
            try:
                _Job(session, job, self._builds_dir(job.workspace_id), self._upstream_domains(), self._kill_grace, self._dependents_cap()).run()
                self._finish(session, job, "done")
            except _Canceled:
                self._finish(session, job, "canceled")
            except BuildError as exc:
                self._finish(session, job, "failed", str(exc))
            except Exception as exc:
                log.exception("Build job %s failed", job_id)
                self._finish(session, job, "failed", f"Unexpected error: {exc}")

    @staticmethod
    def _finish(session: Session, job: BuildJob, status: str, error: str | None = None) -> None:
        session.rollback()
        job.status = status
        job.error = error
        job.finished_at = utcnow()
        session.commit()


def _check_environment(workspace: Workspace) -> None:
    if not workspace.lunch_target:
        raise BuildError("Set a lunch target in the workspace settings to build")
    envsetup = envsetup_path(workspace)
    if not envsetup.is_file():
        raise BuildError(f"envsetup.sh not found: {envsetup}")


def _not_buildable(session: Session, item: Item) -> str | None:
    """Why the item is not built on its own: its rework patch is not accepted, or its label is not `applies` / `3way`; None when it is."""
    rework = [p for p in current_patches(session, item) if is_rework(p)]
    if rework:
        proposed = next((p for p in rework if p.review != "accepted"), None)
        return f"patch {proposed.review}: {proposed.project}" if proposed else None
    if item.triage_label in ELIGIBLE:
        return None
    return f"pre-scan: {item.triage_label}" if item.triage_label else "not pre-scanned"


def _items(session: Session, workspace_id: int, keys: list[str]) -> list[tuple[Item, JiraIssue]]:
    """Backlog items of `keys`, in the given order."""
    rows = {
        issue.key: (item, issue)
        for item, issue in session.execute(
            select(Item, JiraIssue).join(JiraIssue, Item.issue_id == JiraIssue.id).where(Item.workspace_id == workspace_id, JiraIssue.key.in_(keys))
        )
    }
    missing = [key for key in keys if key not in rows]
    if missing:
        raise BuildError(f"Not in the backlog: {', '.join(missing)}")
    return [rows[key] for key in dict.fromkeys(keys)]


def _cluster_items(session: Session, cluster_id: int) -> list[tuple[Item, JiraIssue]]:
    """Members of the cluster in cluster order."""
    return [
        (item, issue)
        for item, issue in session.execute(
            select(Item, JiraIssue)
            .join(JiraIssue, Item.issue_id == JiraIssue.id)
            .join(ClusterMember, ClusterMember.item_id == Item.id)
            .where(ClusterMember.cluster_id == cluster_id)
            .order_by(ClusterMember.position, ClusterMember.id)
        )
    ]


@dataclass
class _Job:
    session: Session
    job: BuildJob
    builds_dir: Path
    upstream_domains: list[str]
    kill_grace: float
    dependents_cap: int = DEFAULT_DEPENDENTS_CAP

    def run(self) -> None:
        workspace = self.session.get(Workspace, self.job.workspace_id)
        assert workspace is not None
        _check_environment(workspace)
        self.workspace = workspace
        self.tree = Path(workspace.target_tree)
        self.builds_dir.mkdir(parents=True, exist_ok=True)

        items = _items(self.session, workspace.id, self.job.issue_keys)
        if self.job.scope == "keys":
            units = [_Unit([row]) for row in items]
        else:
            units = [_Unit(items, self.job.cluster_id, stacked=self.job.scope == "stacked")]
        for unit in units:
            if not unit.items:
                continue
            self._check_cancel()
            try:
                self._build(unit)
            except _Canceled:
                raise
            except Exception as exc:
                log.exception("Build of %s failed", ", ".join(unit.keys))
                self.session.rollback()
                # Reassign: in-place changes to a JSON column are not tracked.
                self.job.errors = [*self.job.errors, {"keys": unit.keys, "message": str(exc)}]
            self.job.done += 1
            self.session.commit()

    def _build(self, unit: _Unit) -> None:
        run = BuildRun(job_id=self.job.id, item_ids=[item.id for item, _ in unit.items], cluster_id=unit.cluster_id, patch_ids=[], modules=[])
        self.session.add(run)
        self.session.flush()
        run.log_path = str(self.builds_dir / f"{run.id}.log")
        for item, _ in unit.items:
            self.session.add(HistoryEvent(item_id=item.id, type="build_started", payload={"run_id": run.id, "job_id": self.job.id}))
        self.session.commit()

        try:
            status, summary = self._apply_build_restore(unit, run)
        except Exception as exc:
            self.session.rollback()
            run.status, run.summary, run.finished_at = "error", str(exc), utcnow()
            self.session.commit()
            raise
        run.status, run.summary, run.finished_at = status, summary, utcnow()
        for item, _ in unit.items:
            if status != "canceled":
                item.build_status, item.build_reason, item.built_at = status, summary, run.finished_at
            self.session.add(HistoryEvent(item_id=item.id, type="build_finished", payload={"run_id": run.id, "status": status, "summary": summary}))
        self.session.commit()
        if status == "canceled":
            raise _Canceled

    def _apply_build_restore(self, unit: _Unit, run: BuildRun) -> tuple[str, str]:
        """Status and summary of the unit; the tree is restored whatever happens, unless restoring fails."""
        assert run.log_path is not None
        with open(run.log_path, "w") as log_file:
            try:
                try:
                    return self._apply_and_build(unit, run, log_file)
                finally:
                    self._restore(run)
            except _UnitFailed as failed:
                log_file.write(f"{failed.summary}\n")
                return failed.status, failed.summary

    def _apply_and_build(self, unit: _Unit, run: BuildRun, log_file: TextIO) -> tuple[str, str]:
        if unit.stacked:
            unit_patches = self._stacked_patches(unit, run, log_file)
        else:
            patches = self._patches(unit)
            run.patch_ids = [patch.id for patch, _ in patches]
            unit_patches = [target.UnitPatch(project=p.project, diff=p.diff, base_head=None if any_head else p.base_head) for p, any_head in patches]
        try:
            inflight = target.prepare(self.tree, unit_patches)
        except target.TreeRefused as exc:
            raise _UnitFailed("apply_failed", str(exc)) from exc
        run.inflight = inflight.to_json()
        self.session.commit()

        # Stacked patches apply as is, in order: each was made on top of the ones before it.
        projects = [p.project for p in unit_patches]
        three_way = not unit.stacked and (len(projects) != len(set(projects)) or any(p.base_head is None for p in unit_patches))
        try:
            target.apply_patches(self.tree, unit_patches, three_way=three_way)
        except target.ApplyFailed as exc:
            raise _UnitFailed("apply_failed", str(exc)) from exc

        changed: list[str] = []
        reaching: list[str] = []
        api: list[str] = []
        for p in unit_patches:
            files = parse_diff(p.diff).files
            changed += changed_files(p.project, files)
            more, api_files = widening_files(p.project, files)
            reaching += more
            api += api_files
        root = build_root(self.workspace)
        modules = affected_modules(changed, self.tree, root, reaching, api, self.dependents_cap)
        if modules.unbuilt:
            log_file.write(f"Not built (no module): {', '.join(modules.unbuilt)}\n")
        if modules.dependents or modules.dependents_left_out:
            left_out = f" ({modules.dependents_left_out} more left out by the cap)" if modules.dependents_left_out else ""
            log_file.write(f"Dependents built too: {', '.join(modules.dependents) or 'none'}{left_out}\n")
        if modules.api_check:
            log_file.write(f"API changed: {', '.join(api)}\n")
        elif modules.tool == "mmm" and (reaching or api):
            log_file.write("Dependents and API check not built: no single module-info.json\n")
        if modules.nothing_to_build:
            return "build_successful", "nothing to build"
        assert self.workspace.lunch_target is not None
        run.tool, run.modules = modules.tool, modules.targets
        run.command = build_command(envsetup_path(self.workspace), self.workspace.lunch_target, modules)
        self.session.commit()
        return self._run_build(run.command, root, log_file)

    def _patches(self, unit: _Unit) -> list[tuple[MigrationPatch, bool]]:
        """The unit's current migration patches, item by item, each after its base patches (once each), and whether
        it may go on any HEAD: an item with a rework patch is applied as it is, stale or not, the others are
        pre-scanned again first when stale.
        """
        stale = stale_items(self.session, self.workspace.id, self.tree, [item.id for item, _ in unit.items])
        patches: dict[int, tuple[MigrationPatch, bool]] = {}
        for item, issue in unit.items:
            current = current_patches(self.session, item)
            if any(is_rework(p) for p in current):
                if reason := _not_buildable(self.session, item):
                    raise _UnitFailed("apply_failed", f"{issue.key}: {reason}")
                for patch in current:
                    stale_patch = item.id in stale
                    for base in [*base_patches(self.session, patch), patch]:
                        patches.setdefault(base.id, (base, stale_patch))
                continue
            if item.id in stale:
                try:
                    tree, projects = target_projects(self.workspace)
                    scan_item(self.session, item, issue, tree, projects, self.upstream_domains)
                    triage_item(self.session, item, issue)
                except Exception as exc:
                    raise _UnitFailed("apply_failed", f"{issue.key}: re-scan failed: {exc}") from exc
                self.session.commit()
            if item.triage_label not in ELIGIBLE:
                raise _UnitFailed("apply_failed", f"{issue.key}: pre-scan: {item.triage_label}: {item.triage_reason}")
            for patch in current_patches(self.session, item):
                patches.setdefault(patch.id, (patch, False))
        return list(patches.values())

    def _stacked_patches(self, unit: _Unit, run: BuildRun, log_file: TextIO) -> list[target.UnitPatch]:
        """Each item's current patches (a diff without one: the diff) stacked on the items before it that stacked, as in
        a stacked pre-scan; their stacked patches in order. Base patches are not added: the items below are the base.

        An item that does not stack (a diff conflicts or is unmapped, no diffs, an error) is
        left out of the unit: `apply_failed` with the reason, listed as skipped, out of the
        run's items. No migration patches are used or written.
        """
        tree, projects = target_projects(self.workspace)
        patches: list[target.UnitPatch] = []
        built: list[tuple[Item, JiraIssue]] = []
        with tempfile.TemporaryDirectory(prefix="aosp-migrator-stack-") as tmp:
            stack = Stack(tree, Path(tmp))
            for item, issue in unit.items:
                try:
                    diffs = patch_plan(self.session, item)
                    stacked = stack.add(
                        [
                            StackDiff(f"patch {d.id} ({d.project})", d.project, d.diff)
                            if isinstance(d, MigrationPatch)
                            else stack_diff(self.session, d, issue, tree, projects, self.upstream_domains)
                            for d in diffs
                        ]
                    )
                except Exception as exc:
                    log.exception("Stacking %s failed", issue.key)
                    reason = str(exc)
                else:
                    failed = next((d for d in stacked.diffs if d.status in ("conflicts", "unmapped")), None)
                    reason = "no diffs" if not diffs else f"{failed.filename}: {failed.error}" if failed else None
                if reason is None:
                    built.append((item, issue))
                    patches += [target.UnitPatch(d.project, d.patch, stack.heads[d.project]) for d in stacked.diffs if d.project and d.patch]
                    log_file.write(f"Stacked {issue.key}: {', '.join(f'{d.filename} {d.status}' for d in stacked.diffs)}\n")
                    continue
                summary = f"left out of the stack: {reason}"
                log_file.write(f"Left out {issue.key}: {reason}\n")
                item.build_status, item.build_reason, item.built_at = "apply_failed", summary, utcnow()
                self.session.add(HistoryEvent(item_id=item.id, type="build_finished", payload={"run_id": run.id, "status": "apply_failed", "summary": summary}))
                # Reassign: in-place changes to a JSON column are not tracked.
                self.job.skipped = [*self.job.skipped, {"key": issue.key, "reason": summary}]
        unit.items = built
        run.item_ids = [item.id for item, _ in built]
        self.session.commit()
        if not built:
            raise _UnitFailed("apply_failed", "no item stacked")
        return patches

    def _run_build(self, command: str, cwd: Path, log_file: TextIO) -> tuple[str, str]:
        log_path = Path(log_file.name)
        log_file.write(f"$ {command}\n")
        log_file.flush()
        process = subprocess.Popen(
            ["bash", "-c", command], cwd=cwd, stdin=subprocess.DEVNULL, stdout=log_file, stderr=subprocess.STDOUT, start_new_session=True
        )
        timeout = self.workspace.build_timeout
        deadline = time.monotonic() + timeout
        while True:
            try:
                code = process.wait(POLL)
                break
            except subprocess.TimeoutExpired:
                pass
            if time.monotonic() > deadline:
                self._kill(process)
                log_file.write(f"\nTimed out after {timeout} s\n")
                log_file.flush()
                return "build_failed", f"timed out after {timeout} s\n{error_summary(log_path)}".rstrip()
            if self._cancel_requested():
                self._kill(process)
                log_file.write("\nCanceled\n")
                return "canceled", "canceled"
        log_file.flush()
        if code == 0:
            return "build_successful", ""
        return "build_failed", error_summary(log_path) or f"exit code {code}"

    def _kill(self, process: subprocess.Popen[bytes]) -> None:
        """SIGTERM to the build's process group, SIGKILL when it does not end in time."""
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(process.pid, sig)
            except ProcessLookupError:
                pass
            try:
                process.wait(self.kill_grace)
                return
            except subprocess.TimeoutExpired:
                continue
        process.wait()

    def _restore(self, run: BuildRun) -> None:
        if run.inflight is None:
            return
        target.restore(self.tree, target.InFlight.from_json(run.inflight))
        run.inflight = None
        self.session.commit()

    def _cancel_requested(self) -> bool:
        self.session.refresh(self.job, ["cancel_requested"])
        return self.job.cancel_requested

    def _check_cancel(self) -> None:
        if self._cancel_requested():
            raise _Canceled
