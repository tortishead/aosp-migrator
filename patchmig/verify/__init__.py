"""Verify: build and test each migrated cluster on the build host, fixing build breaks.

Per cluster, on a free tree slot (one per parallel build):

1. Put the slot's touched projects on the target base and apply the cluster's commits
   (`git format-patch` locally, `git am -3` on the host), so the slot holds exactly the
   target plus this cluster (and the clusters it was built on).
2. Build the modules of the changed files (Android.bp lookup), or a full build when there
   are too many.
3. On a build error, the cluster agent gets the errors and fixes the change in the cluster's
   worktree (guardrails apply); the fix is committed, re-applied and rebuilt, up to
   budget.max_fix_attempts. An agent claim that the break is not the cluster's is checked
   by building the same modules on the unpatched target.
4. Run the TEST_MAPPING tests; when they fail, run them on the unpatched target too, so a
   failure that already exists there is not blamed on the cluster.
5. Put the slot back on the target base.

Outcome: VERIFIED, FAILED or NEEDS_DECISION, through LedgerStore.transition; details in
cluster.result["verification"].
"""

from __future__ import annotations

import queue
import shlex
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select as sql_select

from patchmig.agents.cluster import FixAnswer, ask_build_fixer
from patchmig.agents.runner import AgentFailed, AgentRunner, BudgetExceeded
from patchmig.config import ProjectConfig
from patchmig.gitutil import git
from patchmig.knowledge.trees import Trees
from patchmig.ledger import Cluster, LedgerStore, Patch, Run, State
from patchmig.ledger.states import MIGRATED
from patchmig.migrate import Migrator, guardrails, ladder
from patchmig.migrate.workspace import IDENTITY, ProjectWorktree
from patchmig.verify.host import BuildHost, CommandResult, host_for
from patchmig.verify.selection import Selection, select

ACTOR = "orchestrator"
MAX_ERROR_CHARS = 8000


class VerifyError(Exception):
    pass


@dataclass
class VerifyOutcome:
    cluster_id: int
    name: str
    state: str  # final state, or "skipped"
    reason: str = ""


@dataclass
class VerifyReport:
    run_id: int
    outcomes: list[VerifyOutcome] = field(default_factory=list)
    stopped_by_budget: str | None = None


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def error_excerpt(output: str, limit: int = MAX_ERROR_CHARS) -> str:
    """The compiler errors of a build log with a little context, else its tail."""
    lines = output.splitlines()
    keep: list[int] = []
    for i, line in enumerate(lines):
        low = line.lower()
        if "error:" in low or low.startswith("failed:") or "error " in low[:12]:
            keep.extend(range(max(0, i - 1), min(len(lines), i + 3)))
    picked = [lines[i] for i in dict.fromkeys(keep)] if keep else lines[-80:]
    text = "\n".join(picked)
    return text if len(text) <= limit else text[-limit:]


class _BudgetStop(Exception):
    def __init__(self, message: str, scope: str = "run"):
        super().__init__(message)
        self.scope = scope


class Verifier:
    def __init__(
        self,
        config: ProjectConfig,
        store: LedgerStore,
        run: Run,
        runner: AgentRunner | None,
        host: BuildHost | None = None,
        mcp: dict | None = None,
    ):
        if config.build is None:
            raise VerifyError("the project has no 'build' section")
        self.config = config
        self.build = config.build
        self.store = store
        self.run = run
        self.runner = runner
        self.mcp = mcp
        self.host = host or host_for(config.build)
        self.migrator = Migrator(config, store, run, None)
        self.trees = self.migrator.trees
        self.slots: queue.Queue[str] = queue.Queue()
        for slot in self.build.tree_slots():
            self.slots.put(slot)

    # ---- host commands ----------------------------------------------------------------

    def _in(self, slot: str, project: str, script: str, input: str | None = None):
        cd = f"cd {shlex.quote(f'{slot}/{project}')}"
        return self.host.run(f"{cd} && {script}", input=input, timeout=600)

    def _setup(self, slot: str) -> str:
        setup = self.build.setup_cmd.format(lunch_target=self.build.lunch_target)
        return f"cd {shlex.quote(slot)} && {setup}"

    def _put_on(self, slot: str, project: str, rev: str) -> CommandResult:
        return self._in(
            slot,
            project,
            "(git am --abort >/dev/null 2>&1 || true) && "
            f"git checkout -q --detach {rev} && git reset -q --hard && git clean -fdq",
        )

    def _apply(self, slot: str, project: str, base: str, head: str) -> CommandResult:
        res = self._put_on(slot, project, base)
        if not res.ok:
            res.output = (
                f"cannot put {project} on the target base {base[:12]} in {slot} (is the slot "
                f"synced to the target tree?):\n{res.output}"
            )
            return res
        if base == head:
            return res
        patches = git(
            self.trees.target_repo(project), "format-patch", "--stdout", f"{base}..{head}"
        )
        env = " ".join(f"{k}={shlex.quote(v)}" for k, v in IDENTITY.items() if "COMMITTER" in k)
        return self._in(slot, project, f"env {env} git am -q --3way", input=patches)

    def _timeout(self) -> float:
        return self.build.timeout_minutes * 60

    def _build(self, slot: str, sel: Selection) -> tuple[str, CommandResult]:
        # Only the affected modules, never a full build: incremental on the prepared slot.
        cmd = self.build.module_build_cmd.format(modules=" ".join(sel.modules))
        return cmd, self.host.run(f"{self._setup(slot)} && {cmd}", timeout=self._timeout())

    def _tests(self, slot: str, tests: list[str]) -> tuple[str, CommandResult]:
        cmd = self.config.tests.test_cmd.format(tests=" ".join(tests))
        return cmd, self.host.run(f"{self._setup(slot)} && {cmd}", timeout=self._timeout())

    def _record(self, cmd: str, res: CommandResult, **extra) -> dict:
        return {
            "command": cmd,
            "ok": res.ok,
            "exit_code": res.exit_code,
            "timed_out": res.timed_out,
            "duration_s": round(res.duration_s, 1),
            "log_tail": res.tail(self.build.log_tail_lines),
            **extra,
        }

    # ---- the run ----------------------------------------------------------------------

    def _clusters(self, cluster_ids: list[int] | None) -> list[Cluster]:
        with self.store.session() as s:
            rows = s.scalars(
                sql_select(Cluster)
                .where(Cluster.run_id == self.run.id)
                .order_by(Cluster.order_index)
            ).all()
        return [
            c for c in rows if c.state in MIGRATED and (cluster_ids is None or c.id in cluster_ids)
        ]

    def verify(self, cluster_ids: list[int] | None = None, limit: int | None = None):
        report = VerifyReport(run_id=self.run.id)
        todo = self._clusters(cluster_ids)[:limit]
        workers = max(1, min(len(todo), self.slots.qsize()))

        def one(cluster: Cluster) -> VerifyOutcome:
            if report.stopped_by_budget:
                return VerifyOutcome(cluster.id, cluster.name, "skipped", report.stopped_by_budget)
            slot = self.slots.get()
            try:
                return self._verify(cluster, slot)
            except _BudgetStop as e:
                report.stopped_by_budget = str(e)
                return VerifyOutcome(cluster.id, cluster.name, "skipped", str(e))
            finally:
                self.slots.put(slot)

        with ThreadPoolExecutor(max_workers=workers) as pool:
            report.outcomes = list(pool.map(one, todo))
        return report

    def _verify(self, cluster: Cluster, slot: str) -> VerifyOutcome:
        migration = cluster.result.get("migration") or {}
        projects = {
            p: info
            for p, info in migration.get("projects", {}).items()
            if info.get("head") and self.trees.target_base(p)
        }
        record: dict = {"started_at": _now(), "slot": slot, "host": self.host.name,
                        "builds": [], "fixes": [], "tests": None}  # fmt: skip
        if not projects:
            return self._finish(cluster, State.FAILED, record, "no migrated commits to verify")
        try:
            return self._verify_on(cluster, slot, projects, record)
        except _BudgetStop as e:
            if e.scope != "cluster":
                raise
            return self._finish(  # only this cluster stops; the others go on
                cluster, State.NEEDS_DECISION, record,
                f"{e}; raise budget.cluster_credits, then decide 'migrate' to retry",
            )  # fmt: skip
        finally:
            for project in projects:
                self._put_on(slot, project, self.trees.target_base(project))

    def _selection(self, projects: dict[str, dict]) -> Selection:
        sel = Selection()
        for project, info in projects.items():
            repo = self.trees.target_repo(project)
            base = self.trees.target_base(project)
            paths = git(repo, "diff", "--name-only", base, info["head"]).split()
            part = select(repo, info["head"], project, paths, self.build, self.config.tests)
            sel.modules += part.modules
            sel.tests += part.tests
            sel.notes += part.notes
        sel.modules = list(dict.fromkeys(sel.modules))
        sel.tests = list(dict.fromkeys(sel.tests))[: self.config.tests.max_tests]
        return sel

    def _apply_all(self, slot: str, projects: dict[str, dict]) -> str | None:
        for project, info in projects.items():
            res = self._apply(slot, project, self.trees.target_base(project), info["head"])
            if not res.ok:
                return f"{project}: applying the cluster's commits on {slot} failed:\n" + res.tail(
                    40
                )
        return None

    def _base_build(self, slot: str, projects: dict[str, dict], sel: Selection) -> CommandResult:
        for project in projects:
            self._put_on(slot, project, self.trees.target_base(project))
        return self._build(slot, sel)[1]

    def _verify_on(self, cluster, slot, projects, record) -> VerifyOutcome:
        if error := self._apply_all(slot, projects):
            return self._finish(cluster, State.FAILED, record, error)
        sel = self._selection(projects)
        record["selection"] = sel.to_json()
        members = self._members(cluster)
        previous: list[str] = []
        fix_confidence: float | None = None
        attempt = 0
        if not sel.modules and not (self.config.tests.enabled and sel.tests):
            return self._finish(
                cluster, State.NEEDS_DECISION, record,
                "no affected module or test found for the changed files; add build.module_map "
                "entries for them, then verify again",
            )  # fmt: skip
        while sel.modules:
            cmd, res = self._build(slot, sel)
            record["builds"].append(self._record(cmd, res, attempt=attempt))
            self._save(cluster, record)
            if res.ok:
                break
            if attempt >= self.config.budget.max_fix_attempts or self.runner is None:
                why = "no agent" if self.runner is None else f"{attempt} fix attempt(s)"
                return self._finish(cluster, State.FAILED, record, f"build fails ({why}): {cmd}")
            attempt += 1
            fix = self._fix(cluster, members, projects, cmd, res, previous, attempt, record)
            if fix is None:
                return self._finish(
                    cluster, State.FAILED, record, record["fixes"][-1].get("error", "fix failed")
                )
            if fix.outcome == "not_caused_by_cluster":
                base = self._base_build(slot, projects, sel)
                record["fixes"][-1]["base_build"] = self._record(cmd, base)
                if not base.ok:
                    return self._finish(
                        cluster, State.NEEDS_DECISION, record,
                        f"the build of {cmd!r} also fails on the unpatched target; fix the "
                        "target or the build setup, then verify again",
                    )  # fmt: skip
                previous.append(f"claimed not caused by the cluster, but the target builds: "
                                f"{fix.summary}")  # fmt: skip
                if error := self._apply_all(slot, projects):
                    return self._finish(cluster, State.FAILED, record, error)
                continue
            if fix.outcome == "cannot_fix":
                return self._finish(
                    cluster, State.FAILED, record, f"build fails; agent cannot fix: {fix.summary}"
                )
            fix_confidence = min(fix_confidence or 1.0, fix.confidence)
            previous.append(fix.summary)
            if error := self._apply_all(slot, projects):
                return self._finish(cluster, State.FAILED, record, error)

        confidence = cluster.confidence
        if fix_confidence is not None and confidence is not None:
            confidence = min(confidence, fix_confidence)
        tests_cfg = self.config.tests
        if not tests_cfg.enabled or not sel.tests:
            note = "built; no tests selected" if tests_cfg.enabled else "built; tests disabled"
            return self._finish(cluster, State.VERIFIED, record, note, confidence)
        cmd, res = self._tests(slot, sel.tests)
        record["tests"] = self._record(cmd, res, tests=sel.tests)
        if res.ok:
            return self._finish(
                cluster, State.VERIFIED, record, f"built and {len(sel.tests)} test(s) passed",
                confidence,
            )  # fmt: skip
        if not tests_cfg.baseline:
            return self._finish(cluster, State.FAILED, record, f"tests fail: {cmd}")
        for project in projects:
            self._put_on(slot, project, self.trees.target_base(project))
        _, base = self._tests(slot, sel.tests)
        record["tests"]["baseline"] = self._record(cmd, base)
        if base.ok:
            return self._finish(
                cluster, State.FAILED, record, "tests fail with the cluster and pass without it"
            )
        cap = round(max(self.config.thresholds.quick_approve - 0.01, 0.0), 2)
        return self._finish(
            cluster, State.VERIFIED, record,
            "built; tests fail on the unpatched target too (not caused by the cluster)",
            min(confidence, cap) if confidence is not None else cap,
        )  # fmt: skip

    # ---- build fixes ------------------------------------------------------------------

    def _fix(self, cluster, members, projects, cmd, res, previous, attempt, record):
        entry: dict = {"attempt": attempt}
        record["fixes"].append(entry)
        worktrees = {
            p: ProjectWorktree(
                p, self.trees.target_repo(p), Path(i["path"]), i["branch"], i["base"]
            )
            for p, i in projects.items()
            if i.get("path")
        }
        missing = [p for p in projects if p not in worktrees or not worktrees[p].path.exists()]
        if missing:
            entry["error"] = f"cluster worktree missing for {', '.join(missing)}; migrate again"
            return None
        ctx = self.migrator.context_for(cluster, members)
        since = {p: wt.head() for p, wt in worktrees.items()}
        try:
            outcome = ask_build_fixer(
                self.runner, self.config, ctx,
                self.migrator.workspaces.cluster_dir(self.run.id, cluster.id),
                cluster.result["migration"].get("diff") or "", cmd,
                error_excerpt(res.output), previous, self.mcp, attempt,
            )  # fmt: skip
        except BudgetExceeded as e:
            raise _BudgetStop(str(e), e.scope) from e
        except AgentFailed as e:
            entry["error"] = f"build fixer failed: {e}"
            return None
        fix: FixAnswer = outcome.data
        entry["answer"] = fix.model_dump()
        if fix.outcome != "fixed":
            for p, wt in worktrees.items():
                ladder.reset(wt, since[p])
            return fix
        migrated = {p: set(git(wt.repo, "diff", "--name-only", self.trees.target_base(p),
                                 since[p]).split()) for p, wt in worktrees.items()}  # fmt: skip
        report = guardrails.check(
            list(worktrees.values()), since, migrated, {},
            guardrails.diff_lines(ctx.net_diff), self.config.path_rules, self.config.migrate,
        )  # fmt: skip
        entry["guardrails"] = report.to_json()
        if not report.ok:
            for p, wt in worktrees.items():
                ladder.reset(wt, since[p])
            entry["error"] = "fix rejected by guardrails: " + "; ".join(
                report.hard + report.decisions
            )
            return None
        msg = f"Fix build of migrated cluster: {cluster.name}\n\n{fix.summary}\n"
        for p, wt in worktrees.items():
            if ladder.commit(wt, msg):
                projects[p]["head"] = wt.head()
        self._update_migration(cluster, projects)
        return fix

    def _update_migration(self, cluster: Cluster, projects: dict[str, dict]) -> None:
        with self.store.session() as s, s.begin():
            row = s.get(Cluster, cluster.id)
            migration = dict(row.result["migration"])
            migration["projects"] = {**migration["projects"], **projects}
            migration["diff"] = self.migrator.migrated_diff(migration)
            row.result = {**row.result, "migration": migration}
            cluster.result = row.result

    # ---- recording --------------------------------------------------------------------

    def _members(self, cluster: Cluster) -> list[Patch]:
        with self.store.session() as s:
            return list(
                s.scalars(
                    sql_select(Patch)
                    .where(Patch.cluster_id == cluster.id, Patch.state == cluster.state)
                    .order_by(Patch.seq)
                )
            )

    def _save(self, cluster: Cluster, record: dict, confidence: float | None = None) -> None:
        with self.store.session() as s, s.begin():
            row = s.get(Cluster, cluster.id)
            row.result = {**row.result, "verification": record}
            if confidence is not None:
                row.confidence = confidence

    def _finish(self, cluster, state: State, record: dict, reason: str, confidence=None):
        record.update(finished_at=_now(), state=state.value, reason=reason)
        self._save(cluster, record, confidence)
        for m in self._members(cluster):
            self.store.transition(m, state, actor=ACTOR, reason=reason)
            if confidence is not None:
                with self.store.session() as s, s.begin():
                    s.get(Patch, m.id).confidence = confidence
        self.store.transition(cluster, state, actor=ACTOR, reason=reason)
        return VerifyOutcome(cluster.id, cluster.name, state.value, reason)


def verify(
    config: ProjectConfig,
    store: LedgerStore,
    run: Run,
    runner: AgentRunner | None,
    *,
    host: BuildHost | None = None,
    cluster_ids: list[int] | None = None,
    limit: int | None = None,
    mcp: dict | None = None,
) -> VerifyReport:
    """Build and test the run's migrated clusters. With runner=None build breaks fail."""
    return Verifier(config, store, run, runner, host, mcp).verify(cluster_ids, limit)


def check_host(config: ProjectConfig, host: BuildHost | None = None) -> list[tuple[str, bool, str]]:
    """Connection test: the host answers, and each slot looks like a prepared tree."""
    if config.build is None:
        raise VerifyError("the project has no 'build' section")
    host = host or host_for(config.build)
    res = host.run("echo ok", timeout=60)
    checks = [(f"connect to {host.name}", res.ok, res.tail(5))]
    if not res.ok:
        return checks
    trees = Trees(config)
    projects = trees.target_projects()
    for slot in config.build.tree_slots():
        q = shlex.quote(slot)
        res = host.run(f"test -f {q}/build/envsetup.sh && echo ok", timeout=60)
        checks.append((f"{slot}: build/envsetup.sh", res.ok, res.tail(5)))
        for project in projects:
            base = trees.target_base(project)
            if base is None:
                continue
            res = host.run(
                f"cd {shlex.quote(f'{slot}/{project}')} && git cat-file -e {base}^{{commit}}",
                timeout=60,
            )
            checks.append((f"{slot}/{project}: has target base {base[:12]}", res.ok, res.tail(5)))
    return checks
