"""Rework jobs: run in a background thread, one at a time per workspace, user-started only.

Plan (`plan`): the selected items (with `include_dependents` also every item that needs
one of them, transitively) and the items they need (`chain.chain`), in units,
prerequisites first. A prerequisite outside the selection is added unless the user left
it out (`exclude`); a closed or not relevant one only when ticked (`include`). An added
one that already has a fresh patch for every current diff is not reworked but used as
base as it is. A left-out one is still used as base when every current patch of it is
accepted; else not, and the items that need it are reworked without it (noted in the
attempt). `together` merges the units of the selected items into one.

Per unit, one attempt (`rework.attempt`), over the projects of its items' diffs. Per
project one scratch checkout (`rework.scratch`) with the base committed: the current
patches in that project of the unit's prerequisites, in chain order. The unit's items go
up the ladder (`rework.ladder`) one after another in it, each committed before the next.
Copilot runs only for the rungs in the options, while the item's cap and the job's cap
allow; once the job's cap is spent the rest get rungs 1-2 only.

Then the build-fix loop: the patches, on their base, are built in the user's tree as one
unit (`BuildRunner.build_patches`: the same prepare, apply and restore as a build). A
failed build goes back to Copilot (`ladder.fix_build`, the last item of a project the
errors name) for up to `build_rounds` rounds; it stops early when the errors repeat or a
round changes nothing, and instead of a round reports a likely missing dependency when
an error names a symbol that another item's diff adds. A unit whose build still fails
fails. A unit that cannot be built (no lunch target, tree refused) keeps its patches
with a warning. Patched units save one `proposed` patch per item and project.

Each attempt's scratch checkouts are deleted after it (kept on failure with
`rework.keep_scratch`, always deleted on cancel). Cancel stops the Copilot process or the
build and ends the job. A failing unit does not stop the job. At startup, running jobs
and attempts are marked failed and their scratch checkouts deleted.
"""

import logging
import re
import shutil
import threading
from collections.abc import Callable, Iterable, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from aosp_migrator.build import tree as target_tree
from aosp_migrator.build.runner import BuildError, BuildRunner, check_environment
from aosp_migrator.config import Config, CopilotSettings
from aosp_migrator.copilot.client import CopilotClient
from aosp_migrator.db.models import Attachment, BuildJob, HistoryEvent, Item, JiraIssue, MigrationPatch, PrescanJob, ReworkAttempt, ReworkJob, Workspace, utcnow
from aosp_migrator.prescan import git
from aosp_migrator.prescan.chain import chain, current_patches, item_needs, patch_plan, stale_items
from aosp_migrator.prescan.patch import Patch
from aosp_migrator.prescan.runner import current_diffs, diff_scan, read_diff_text
from aosp_migrator.rework import attempt as attempts
from aosp_migrator.rework import ladder
from aosp_migrator.rework.checks import Finding
from aosp_migrator.rework.ladder import Budget, Copilot, LadderResult
from aosp_migrator.rework.scratch import Scratch, ScratchError, touched_files

log = logging.getLogger(__name__)

RUNGS = ("resolve", "port", "reimpl")
# Rungs preselected for a new rework; re-implement is opt-in.
DEFAULT_RUNGS = ("resolve", "port")
LEFT_OUT_STATES = ("closed", "not_relevant")
NEEDS_REIMPL = "(needs re-implement)"
_QUOTED = re.compile(r"[‘'`\"]([A-Za-z_][A-Za-z0-9_]*(?:::[A-Za-z_][A-Za-z0-9_]*)*)(?:\(\))?[’'\"]")
_SYMBOL = re.compile(r"symbol:\s+(?:(?:class|method|variable|function)\s+)?([A-Za-z_][A-Za-z0-9_]*)")
# Shortest symbol name looked for in other items' diffs.
SYMBOL_MIN = 3


class ReworkJobConflict(Exception):
    """A rework, build or pre-scan is already running for the workspace."""


class ReworkError(Exception):
    """The job cannot start: unknown keys, nothing to rework."""


class _Canceled(Exception):
    pass


class _UnitFailed(Exception):
    """The unit ends `failed` with this reason."""


def _start_thread(target: Callable[[], None]) -> None:
    threading.Thread(target=target, name="rework-job", daemon=True).start()


def running_job(session: Session, workspace_id: int) -> ReworkJob | None:
    return session.scalar(select(ReworkJob).where(ReworkJob.workspace_id == workspace_id, ReworkJob.status == "running"))


def fail_interrupted_jobs(session: Session, config: Config) -> None:
    """Jobs and attempts still `running` at startup died with the previous process; their scratch checkouts go."""
    for job in session.scalars(select(ReworkJob).where(ReworkJob.status == "running")):
        job.status, job.error, job.finished_at = "failed", "Interrupted by a server restart", utcnow()
    for attempt in session.scalars(select(ReworkAttempt).where(ReworkAttempt.status == "running")):
        attempt.status, attempt.reason, attempt.finished_at = "error", "Interrupted by a server restart", utcnow()
        shutil.rmtree(config.rework_dir(attempt.workspace_id, attempt.id), ignore_errors=True)
    session.commit()


@dataclass
class Options:
    rungs: list[str] = field(default_factory=lambda: list(DEFAULT_RUNGS))
    include_dependents: bool = False
    # Keys of closed / not relevant prerequisites to add; of other prerequisites to leave out.
    include: list[str] = field(default_factory=list)
    exclude: list[str] = field(default_factory=list)
    # The selected items as one unit.
    together: bool = False
    # None: the settings.
    item_cap: int | None = None
    job_cap: int | None = None
    build_rounds: int | None = None


@dataclass
class PlanItem:
    item_id: int
    key: str
    summary: str
    local_state: str
    triage_label: str | None
    selected: bool  # Selected, or a dependent of a selected item (`include_dependents`)
    dependent: bool
    included: bool  # In the job: reworked, or used as base as it is
    rework: bool
    # Its patches go below the items that need it: reworked in the job, or a patch for every current diff.
    base: bool
    has_patch: bool  # A current patch for every current diff
    accepted: bool  # And all of them accepted
    stale: list[str]
    warning: str | None  # Closed / not relevant
    reason: str | None  # Why it is not reworked


@dataclass
class Plan:
    items: dict[int, PlanItem]
    # Item ids per unit to rework, in order.
    units: list[list[int]]
    needs: dict[int, set[int]]
    rungs: list[str]
    estimate: dict[str, int]

    @property
    def prerequisites(self) -> list[PlanItem]:
        """The chain's items outside the selection, in chain order."""
        return [p for p in self.items.values() if not p.selected]

    def skipped(self) -> list[dict[str, Any]]:
        return [{"key": p.key, "reason": p.reason, "base": p.base} for p in self.items.values() if not p.rework]


def _rows(session: Session, workspace_id: int, keys: Sequence[str]) -> list[tuple[Item, JiraIssue]]:
    rows = {
        issue.key: (item, issue)
        for item, issue in session.execute(
            select(Item, JiraIssue).join(JiraIssue, Item.issue_id == JiraIssue.id).where(Item.workspace_id == workspace_id, JiraIssue.key.in_(keys))
        )
    }
    if missing := [key for key in keys if key not in rows]:
        raise ReworkError(f"Not in the backlog: {', '.join(missing)}")
    return [rows[key] for key in dict.fromkeys(keys)]


def dependents(needs: dict[int, set[int]], item_ids: Iterable[int]) -> set[int]:
    """Every item that needs one of `item_ids`, transitively."""
    needed_by: dict[int, set[int]] = {}
    for item_id, prerequisites in needs.items():
        for p in prerequisites:
            needed_by.setdefault(p, set()).add(item_id)
    found: set[int] = set()
    todo = list(item_ids)
    while todo:
        for other in needed_by.get(todo.pop(), ()):
            if other not in found:
                found.add(other)
                todo.append(other)
    return found - set(item_ids)


def has_patch(session: Session, item: Item) -> bool:
    """Every current diff has a current patch (accepted or proposed)."""
    plan = patch_plan(session, item)
    return bool(plan) and all(isinstance(p, MigrationPatch) for p in plan)


def prerequisites_of(needs: dict[int, set[int]], unit: Iterable[int]) -> set[int]:
    """What the unit's items need, transitively, the unit itself left out."""
    members = set(unit)
    found: set[int] = set()
    todo = list(members)
    while todo:
        for p in needs.get(todo.pop(), ()):
            if p not in found and p not in members:
                found.add(p)
                todo.append(p)
    return found


def plan(session: Session, workspace: Workspace, keys: Sequence[str], options: Options, settings: CopilotSettings) -> Plan:
    """What a rework of `keys` with `options` does; raises `ReworkError` for keys not in the backlog."""
    selected = {item.id for item, _ in _rows(session, workspace.id, keys)}
    added = dependents(item_needs(session, workspace.id), selected) if options.include_dependents else set()
    found = chain(session, workspace.id, selected | added)
    order = [item_id for unit in found.units for item_id in unit]
    rows = {item.id: (item, issue) for item, issue in session.execute(select(Item, JiraIssue).join(JiraIssue, Item.issue_id == JiraIssue.id).where(Item.id.in_(order)))}
    stale = stale_items(session, workspace.id, Path(workspace.target_tree), order)
    items: dict[int, PlanItem] = {}
    for item_id in order:
        item, issue = rows[item_id]
        is_selected = item_id in selected or item_id in added
        ready = has_patch(session, item)
        accepted = ready and all(p.review == "accepted" for p in current_patches(session, item))
        warning = item.local_state.replace("_", " ") if item.local_state in LEFT_OUT_STATES else None
        entry = PlanItem(
            item_id, issue.key, issue.summary, item.local_state, item.triage_label, is_selected, item_id in added,
            included=True, rework=True, base=True, has_patch=ready, accepted=accepted, stale=stale.get(item_id, []), warning=warning, reason=None,
        )  # fmt: skip
        if not is_selected:
            entry.included = issue.key in options.include if warning else issue.key not in options.exclude
            if not entry.included:
                entry.rework, entry.base = False, accepted
                entry.reason = "left out, used as base as it is" if accepted else "left out: reworked without it"
            elif ready and not entry.stale:
                entry.rework, entry.reason = False, "has a patch: used as base as it is"
        items[item_id] = entry
    units = [[i for i in unit if items[i].rework] for unit in found.units]
    units = [unit for unit in units if unit]
    if options.together:
        chosen = [unit for unit in units if any(items[i].selected for i in unit)]
        if len(chosen) > 1:
            last = units.index(chosen[-1])
            merged = [i for unit in chosen for i in unit]
            units = [merged if n == last else unit for n, unit in enumerate(units) if unit not in chosen[:-1]]
    reworked = [i for unit in units for i in unit]
    # A call per item that conflicts with nothing in the job to stack it on, or that failed to build.
    likely = sum(
        1 for i in reworked
        if (rows[i][0].triage_label == "conflicts" and not prerequisites_of(found.needs, [i])) or rows[i][0].build_status == "build_failed"
    )  # fmt: skip
    item_cap = options.item_cap or settings.item_cap
    job_cap = options.job_cap or settings.job_cap
    estimate = {"units": len(units), "items": len(reworked), "likely_calls": min(likely, job_cap), "max_calls": min(job_cap, item_cap * len(reworked))}
    rungs = list(DEFAULT_RUNGS)
    if any((reason := attempts.last_failure(session, rows[i][0])) and NEEDS_REIMPL in reason for i in reworked):
        rungs.append("reimpl")
    return Plan(items, units, found.needs, rungs, estimate)


class ReworkRunner:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        config: Config,
        build_runner: BuildRunner,
        client_factory: Callable[[CopilotSettings], CopilotClient] = lambda settings: CopilotClient(settings.binary),
        executor: Callable[[Callable[[], None]], None] = _start_thread,
    ) -> None:
        self._sessions = sessions
        self._config = config
        self._build_runner = build_runner
        self._client_factory = client_factory
        self._executor = executor
        self._lock = threading.Lock()

    def plan(self, session: Session, workspace: Workspace, keys: Sequence[str], options: Options) -> Plan:
        return plan(session, workspace, keys, options, self._config.copilot())

    def start(self, workspace_id: int, keys: Sequence[str], options: Options) -> ReworkJob:
        """Create the job for `keys` and run it in the background."""
        if unknown := [r for r in options.rungs if r not in RUNGS]:
            raise ReworkError(f"Unknown rung: {', '.join(unknown)}")
        with self._lock, self._sessions() as session:
            if running_job(session, workspace_id) is not None:
                raise ReworkJobConflict("A rework is already running for this workspace")
            if session.scalar(select(BuildJob.id).where(BuildJob.workspace_id == workspace_id, BuildJob.status == "running")):
                raise ReworkJobConflict("A build is running for this workspace; rework when it is done")
            if session.scalar(select(PrescanJob.id).where(PrescanJob.workspace_id == workspace_id, PrescanJob.status == "running")):
                raise ReworkJobConflict("A pre-scan is running for this workspace; rework when it is done")
            workspace = session.get(Workspace, workspace_id)
            assert workspace is not None
            found = self.plan(session, workspace, keys, options)
            if not found.units:
                raise ReworkError("Nothing to rework")
            settings = self._config.copilot()
            resolved = {
                **asdict(options),
                "item_cap": options.item_cap or settings.item_cap,
                "job_cap": options.job_cap or settings.job_cap,
                "build_rounds": settings.build_rounds if options.build_rounds is None else options.build_rounds,
            }
            job = ReworkJob(
                workspace_id=workspace_id,
                issue_keys=list(dict.fromkeys(keys)),
                options=resolved,
                units=[[found.items[i].key for i in unit] for unit in found.units],
                skipped=found.skipped(),
                total=len(found.units),
                errors=[],
            )
            session.add(job)
            session.commit()
        self._executor(lambda: self.run(job.id))
        return job

    def run(self, job_id: int) -> None:
        with self._sessions() as session:
            job = session.get(ReworkJob, job_id)
            assert job is not None
            settings = self._config.copilot()
            copilot = Copilot(self._client_factory(settings), settings, Path())
            try:
                _Job(session, job, self._config, self._build_runner, copilot).run()
                self._finish(session, job, "done")
            except _Canceled:
                self._finish(session, job, "canceled")
            except Exception as exc:
                log.exception("Rework job %s failed", job_id)
                self._finish(session, job, "failed", f"Unexpected error: {exc}")

    @staticmethod
    def _finish(session: Session, job: ReworkJob, status: str, error: str | None = None) -> None:
        session.rollback()
        job.status, job.error, job.finished_at = status, error, utcnow()
        session.commit()


@dataclass
class _Member:
    """One item of a unit in one project."""

    item: Item
    issue: JiraIssue
    diffs: list[str]
    present: list[str]
    result: LadderResult | None = None


@dataclass
class _Project:
    path: str
    head: str
    base: list[MigrationPatch]
    members: list[_Member]
    scratch: Scratch | None = None


@dataclass
class _Job:
    session: Session
    job: ReworkJob
    config: Config
    build_runner: BuildRunner
    copilot: Copilot

    def run(self) -> None:
        workspace = self.session.get(Workspace, self.job.workspace_id)
        assert workspace is not None
        self.workspace = workspace
        self.tree = Path(workspace.target_tree)
        options = self.job.options
        self.rungs: list[str] = options["rungs"]
        self.budget = Budget(options["job_cap"])
        keys = [key for unit in self.job.units for key in unit] + [s["key"] for s in self.job.skipped]
        rows = {issue.key: (item, issue) for item, issue in _rows(self.session, workspace.id, keys)}
        found = chain(self.session, workspace.id, [item.id for item, _ in rows.values()])
        self.needs = found.needs
        self.order = {item_id: n for n, item_id in enumerate(i for unit in found.units for i in unit)}
        self.reworked = {rows[key][0].id for unit in self.job.units for key in unit}
        self.bases = {rows[s["key"]][0].id for s in self.job.skipped if s["base"]}
        for unit in self.job.units:
            self._check_cancel()
            try:
                self._unit([rows[key] for key in unit])
            except _Canceled:
                raise
            except Exception as exc:
                log.exception("Rework of %s failed", ", ".join(unit))
                self.session.rollback()
                # Reassign: in-place changes to a JSON column are not tracked.
                self.job.errors = [*self.job.errors, {"keys": unit, "message": str(exc)}]
            self.job.done += 1
            self.job.calls = self.budget.used
            self.session.commit()

    def _unit(self, unit: list[tuple[Item, JiraIssue]]) -> None:
        unit_ids = [item.id for item, _ in unit]
        notes: list[str] = []
        base_items: list[Item] = []
        for p in sorted(prerequisites_of(self.needs, unit_ids), key=lambda i: self.order.get(i, len(self.order))):
            other = self.session.get(Item, p)
            assert other is not None
            key = self.session.scalar(select(JiraIssue.key).where(JiraIssue.id == other.issue_id))
            if p not in self.reworked and p not in self.bases:
                notes.append(f"{key} left out: reworked without it")
            elif has_patch(self.session, other):
                base_items.append(other)
            else:
                notes.append(f"{key} has no patch: reworked without it")
        try:
            projects = self._projects(unit, base_items)
        except _UnitFailed as failed:
            attempt = attempts.start(self.session, self.workspace.id, unit_ids, {}, job_id=self.job.id)
            attempt.notes = notes
            self._started(attempt)
            self._end(attempt, "failed", str(failed))
            return
        base_ids = list(dict.fromkeys(p.id for project in projects for p in project.base))
        attempt = attempts.start(self.session, self.workspace.id, unit_ids, {p.path: p.head for p in projects}, base_ids, self.job.id)
        attempt.notes = notes
        self._started(attempt)
        self.copilot.prompts_dir = self.config.workspace_dir(self.workspace.id) / "rework-prompts" / str(attempt.id)
        budgets = {item.id: Budget(self.job.options["item_cap"], self.budget) for item, _ in unit}
        status, reason = "error", "unexpected error"
        try:
            try:
                self._ladder(attempt, projects, base_items, budgets)
                self._build_loop(attempt, projects, base_items, budgets)
                self._save(attempt, projects)
                status, reason = "patched", None
            except _UnitFailed as failed:
                status, reason = "failed", str(failed)
            except _Canceled:
                status, reason = "canceled", "canceled"
                raise
            except Exception as exc:
                reason = str(exc)
                raise
        finally:
            self.session.rollback()
            for project in projects:
                for member in project.members:
                    if member.result is not None:
                        attempts.record(self.session, attempt, project.path, member.result)
                if project.scratch is not None and not (status == "failed" and self.copilot.settings.keep_scratch):
                    project.scratch.remove()
            attempt_dir = self.config.rework_dir(self.workspace.id, attempt.id)
            if status != "failed" or not self.copilot.settings.keep_scratch:
                shutil.rmtree(attempt_dir, ignore_errors=True)
            self._end(attempt, status, reason)

    def _projects(self, unit: list[tuple[Item, JiraIssue]], base_items: list[Item]) -> list[_Project]:
        """The unit's projects with their members and base patches; `_UnitFailed` for a diff without a project."""
        projects: dict[str, _Project] = {}
        for item, issue in unit:
            for attachment in current_diffs(self.session, item.issue_id):
                scan = diff_scan(self.session, attachment.id)
                if scan is None or scan.project is None:
                    raise _UnitFailed(f"{issue.key}: {attachment.filename}: {'not pre-scanned' if scan is None else 'no target project'}")
                text = read_diff_text(attachment)
                if text is None:
                    raise _UnitFailed(f"{issue.key}: {attachment.filename}: downloaded file is missing; reimport the ticket")
                if scan.project not in projects:
                    head = git.head(self.tree / scan.project) if (self.tree / scan.project).is_dir() else None
                    if head is None:
                        raise _UnitFailed(f"{scan.project}: not a git project")
                    base = [p for other in base_items for p in current_patches(self.session, other) if p.project == scan.project]
                    projects[scan.project] = _Project(scan.project, head, base, [])
                members = projects[scan.project].members
                if not members or members[-1].item.id != item.id:
                    members.append(_Member(item, issue, [], []))
                members[-1].diffs.append(text)
                present = (scan.presence_detail or {}).get("files", {})
                members[-1].present += [path for path, value in present.items() if value == "present"]
        if not projects:
            raise _UnitFailed("no diffs")
        return sorted(projects.values(), key=lambda p: p.path)

    def _ladder(self, attempt: ReworkAttempt, projects: list[_Project], base_items: list[Item], budgets: dict[int, Budget]) -> None:
        source_tree = Path(self.workspace.source_tree) if self.workspace.source_tree else None
        for project in projects:
            files = touched_files(d for m in project.members for d in m.diffs)
            try:
                project.scratch = Scratch.create(
                    self.config.rework_dir(self.workspace.id, attempt.id) / project.path, self.tree / project.path, files, [p.diff for p in project.base]
                )
            except ScratchError as exc:
                raise _UnitFailed(str(exc)) from exc
            before: list[Item] = list(base_items)
            for n, member in enumerate(project.members):
                if n:
                    project.scratch.commit(project.members[n - 1].issue.key)
                context = attempts.context(self.session, member.item, project.path, before)
                member.result = ladder.run(
                    project.scratch,
                    member.diffs,
                    context,
                    self._copilot(),
                    budgets[member.item.id],
                    source_tree / project.path if source_tree else None,
                    member.present,
                    self.tree / project.path,
                    self._cancel_requested,
                    self.rungs,
                )
                self.job.calls = self.budget.used
                self.session.commit()
                if member.result.status == "canceled":
                    raise _Canceled
                if member.result.status != "patched":
                    raise _UnitFailed(self._reason(member, project, projects, member.result.reason))
                if member.result.patch is None:
                    raise _UnitFailed(self._reason(member, project, projects, "nothing to change: already in the target"))
                before.append(member.item)

    def _build_loop(self, attempt: ReworkAttempt, projects: list[_Project], base_items: list[Item], budgets: dict[int, Budget]) -> None:
        try:
            check_environment(self.workspace)
        except BuildError as exc:
            self._warn(projects, f"not built: {exc}")
            return
        unit_ids = list(dict.fromkeys(m.item.id for p in projects for m in p.members))
        rounds = self.job.options["build_rounds"]
        previous: str | None = None
        for build_round in range(rounds + 1):
            patches = [target_tree.UnitPatch(p.project, p.diff, None) for p in dict.fromkeys(b for project in projects for b in project.base)]
            for project in projects:
                patches += [target_tree.UnitPatch(project.path, m.result.patch, project.head) for m in project.members if m.result and m.result.patch]
            run = self.build_runner.build_patches(self.session, self.workspace, attempt, unit_ids, patches, self._cancel_requested)
            attempt.build_run_ids = [*attempt.build_run_ids, run.id]
            self.session.commit()
            if run.status == "canceled":
                raise _Canceled
            if run.status == "build_successful":
                return
            summary = run.summary or ""
            if run.status != "build_failed":
                self._warn(projects, f"not built: {summary}")
                return
            failure = f"build failed: {next(iter(summary.splitlines()), '')}".rstrip(": ")
            if build_round == rounds:
                raise _UnitFailed(failure if not rounds else f"{failure} (after {rounds} build-fix round{'s' if rounds > 1 else ''})")
            if summary == previous:
                raise _UnitFailed(f"{failure} (the same errors again)")
            excluded = [*unit_ids, *(i.id for i in base_items)]
            if missing := missing_dependency(self.session, self.workspace.id, summary, excluded):
                symbol, key = missing
                raise _UnitFailed(f"likely missing dependency: `{symbol}` is added by {key}; {failure}")
            copilot = self._copilot()
            fixable = [p for p in projects if any(ladder.build_rung(m.result) in self.rungs for m in p.members[-1:] if m.result)]
            if copilot is None or not fixable:
                raise _UnitFailed(failure)
            named = [p for p in fixable if f"{p.path}/" in summary]
            changed = False
            for project in named or fixable:
                member = project.members[-1]
                assert member.result is not None and project.scratch is not None
                before = [*base_items, *(m.item for m in project.members[:-1])]
                context = attempts.context(self.session, member.item, project.path, before)
                fixed = ladder.fix_build(
                    project.scratch,
                    member.diffs,
                    context,
                    copilot,
                    budgets[member.item.id],
                    member.result,
                    summary.splitlines(),
                    build_round + 1,
                    self.tree / project.path,
                    self._cancel_requested,
                )
                changed = changed or fixed.patch != member.result.patch
                member.result = fixed
                self.job.calls = self.budget.used
                self.session.commit()
                if fixed.status == "canceled":
                    raise _Canceled
                if fixed.status != "patched":
                    raise _UnitFailed(self._reason(member, project, projects, f"{fixed.reason}; {failure}"))
            if not changed:
                raise _UnitFailed(f"{failure} (a build-fix round changed nothing)")
            previous = summary

    def _save(self, attempt: ReworkAttempt, projects: list[_Project]) -> None:
        for project in projects:
            base_ids = [p.id for p in project.base]
            for member in project.members:
                assert member.result is not None
                parsed = Patch.parse(member.diffs[0])
                patch = attempts.save_patch(self.session, attempt, member.item.id, project.path, member.result, project.head, base_ids, parsed.author, parsed.message)
                base_ids = [*base_ids, patch.id]

    def _reason(self, member: _Member, project: _Project, projects: list[_Project], reason: str | None) -> str:
        """`reason` with the key when the unit has several items, the project when the item has several."""
        prefix = []
        if len({m.item.id for p in projects for m in p.members}) > 1:
            prefix.append(member.issue.key)
        if sum(any(m.item.id == member.item.id for m in p.members) for p in projects) > 1:
            prefix.append(project.path)
        return ": ".join([*prefix, reason or "failed"])

    @staticmethod
    def _warn(projects: list[_Project], message: str) -> None:
        for project in projects:
            for member in project.members:
                if member.result is not None:
                    member.result.findings = [*member.result.findings, Finding("check", "warning", None, message)]

    def _copilot(self) -> Copilot | None:
        """Copilot while a Copilot rung is selected and the job's cap is not spent; else rungs 1-2 only."""
        return self.copilot if any(r in self.rungs for r in ladder.COPILOT_RUNGS) and self.budget.left() > 0 else None

    def _started(self, attempt: ReworkAttempt) -> None:
        for item_id in attempt.item_ids:
            self.session.add(HistoryEvent(item_id=item_id, type="rework_started", payload={"attempt_id": attempt.id, "job_id": self.job.id}))
        self.session.commit()

    def _end(self, attempt: ReworkAttempt, status: str, reason: str | None) -> None:
        attempts.finish(self.session, attempt, status, reason)
        for item_id in attempt.item_ids:
            self.session.add(HistoryEvent(item_id=item_id, type="rework_finished", payload={"attempt_id": attempt.id, "status": status, "reason": reason}))
        self.session.commit()

    def _cancel_requested(self) -> bool:
        self.session.refresh(self.job, ["cancel_requested"])
        return self.job.cancel_requested

    def _check_cancel(self) -> None:
        if self._cancel_requested():
            raise _Canceled


def error_symbols(summary: str) -> list[str]:
    """Names a build's error lines quote (`'foo'`, `‘Foo::bar’`, javac's `symbol: method foo`), last component, in order."""
    names: dict[str, None] = {}
    for line in summary.splitlines():
        if "error" not in line.lower() and "symbol:" not in line:
            continue
        for match in [*_QUOTED.finditer(line), *_SYMBOL.finditer(line)]:
            name = match.group(1).rsplit("::", 1)[-1]
            if len(name) >= SYMBOL_MIN:
                names[name] = None
    return list(names)


def _added_lines(text: str) -> str:
    return "\n".join(line[1:] for line in text.splitlines() if line.startswith("+") and not line.startswith("+++"))


def missing_dependency(session: Session, workspace_id: int, summary: str, exclude: Sequence[int]) -> tuple[str, str] | None:
    """A symbol the build's errors name that the current diffs of an item not in `exclude` (the unit, its base) add: it and that item's key."""
    symbols = error_symbols(summary)
    if not symbols:
        return None
    others: list[tuple[str, str]] = []
    rows = session.execute(
        select(JiraIssue.key, Attachment)
        .join(Item, Item.issue_id == JiraIssue.id)
        .join(Attachment, Attachment.issue_id == JiraIssue.id)
        .where(
            Item.workspace_id == workspace_id,
            Item.dropped.is_(False),
            Item.id.not_in(exclude),
            Attachment.kind == "diff",
            Attachment.is_current.is_(True),
            Attachment.local_path.is_not(None),
        )
        .order_by(JiraIssue.key, Attachment.id)
    )
    for key, attachment in rows:
        if (text := read_diff_text(attachment)) is not None:
            others.append((key, _added_lines(text)))
    for symbol in symbols:
        word = re.compile(rf"\b{re.escape(symbol)}\b")
        for key, added in others:
            if word.search(added):
                return symbol, key
    return None
