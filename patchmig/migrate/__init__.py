"""Migrate: move each needed cluster onto the target tree, cheapest rung first.

Per cluster, in dependency order:

1. A sparse worktree per target project on branch patchmig/<name>/r<run>/c<cluster>
   (from the dependency's branch when the cluster depends on another one).
2. Per project: cherry-pick the members, else a 3-way apply of the net diff (no credits).
3. Only if a project is still not done: one cluster agent call, which edits the worktrees.
   Its edits must pass the guardrails (one retry with feedback); a claim that the intent is
   already upstream must quote target code, which is checked.
4. Agent-made changes go to an independent reviewer agent, which sets the confidence.

The outcome goes to the ledger: states through LedgerStore.transition, details in
cluster.result["migration"].
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import select

from patchmig import notes as engineer_notes
from patchmig.agents.cluster import (
    ClusterAnswer,
    ClusterContext,
    ReviewAnswer,
    ask_cluster_agent,
    ask_reviewer,
)
from patchmig.agents.runner import AgentFailed, AgentRunner, BudgetExceeded
from patchmig.analysis.diffs import split_diff
from patchmig.analysis.fold import fold, tree_relative
from patchmig.analysis.patches import load_diff
from patchmig.config import ProjectConfig
from patchmig.gitutil import git, has_binary
from patchmig.knowledge.trees import Trees
from patchmig.knowledge.upstream import FileFate
from patchmig.ledger import Cluster, KnowledgeItem, LedgerStore, Patch, Run, State
from patchmig.ledger.states import DROPPED, MIGRATED
from patchmig.memory import Memory
from patchmig.migrate import guardrails, ladder
from patchmig.migrate.workspace import ProjectWorktree, WorkspaceError, Workspaces, dirs_for
from patchmig.review import below_quick

ACTOR = "orchestrator"
AGENT_ACTOR = "agent:cluster_agent"
DETERMINISTIC_CONFIDENCE = {"cherry_pick": 0.9, "net_diff": 0.85}


@dataclass
class ClusterOutcome:
    cluster_id: int
    name: str
    state: str  # final state, or "skipped" / "needs_agent"
    strategy: str | None = None
    reason: str = ""
    confidence: float | None = None


@dataclass
class MigrateReport:
    run_id: int
    outcomes: list[ClusterOutcome] = field(default_factory=list)
    stopped_by_budget: str | None = None
    credits: float = 0.0


@dataclass
class _Member:
    patch: Patch

    @property
    def line(self) -> str:
        p = self.patch
        sha = (p.commit_sha or "diff")[:12]
        keys = f" [{', '.join(p.jira_keys)}]" if p.jira_keys else ""
        return f"- #{p.id} {p.project} {sha} {p.subject}{keys}"


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class Migrator:
    def __init__(
        self,
        config: ProjectConfig,
        store: LedgerStore,
        run: Run,
        runner: AgentRunner | None,
        mcp: dict | None = None,
        memory: Memory | None = None,
    ):
        self.memory = memory
        self.config = config
        self.store = store
        self.run = run
        self.runner = runner
        self.mcp = mcp
        self.trees = Trees(config)
        self.workspaces = Workspaces(self.trees, config.workdir / "worktrees", config.name)

    # ---- loading ----------------------------------------------------------------------

    def _clusters(self) -> list[Cluster]:
        with self.store.session() as s:
            return list(
                s.scalars(
                    select(Cluster)
                    .where(Cluster.run_id == self.run.id)
                    .order_by(Cluster.order_index)
                )
            )

    def _members(self, cluster: Cluster) -> list[Patch]:
        with self.store.session() as s:
            return list(
                s.scalars(
                    select(Patch)
                    .where(Patch.cluster_id == cluster.id, Patch.state == State.NEEDED)
                    .order_by(Patch.seq)
                )
            )

    def _fates(self, keys: set[str]) -> dict[str, FileFate]:
        if not keys:
            return {}
        with self.store.session() as s:
            rows = s.scalars(
                select(KnowledgeItem).where(
                    KnowledgeItem.run_id == self.run.id,
                    KnowledgeItem.kind == "file_fate",
                    KnowledgeItem.key.in_(keys),
                )
            )
            return {r.key: FileFate.from_json(r.data) for r in rows}

    # ---- the run ----------------------------------------------------------------------

    def migrate(self, cluster_ids: list[int] | None = None, limit: int | None = None):
        report = MigrateReport(run_id=self.run.id)
        clusters = self._clusters()
        by_order = {c.order_index: c for c in clusters}
        done = 0
        for cluster in clusters:
            if cluster_ids is not None and cluster.id not in cluster_ids:
                continue
            if cluster.state != State.NEEDED:
                continue
            if limit is not None and done >= limit:
                break
            deps = [by_order[i] for i in cluster.result.get("depends_on_order", [])]
            waiting = [d for d in self._refresh(deps) if d.state not in MIGRATED | DROPPED]
            if waiting:
                names = ", ".join(f"#{d.id} ({d.state})" for d in waiting)
                report.outcomes.append(
                    ClusterOutcome(cluster.id, cluster.name, "skipped", reason=f"waits for {names}")
                )
                continue
            done += 1
            try:
                outcome = self._migrate_cluster(cluster, self._refresh(deps))
            except BudgetExceeded as e:
                if e.scope == "cluster":  # only this cluster stops; the others go on
                    report.outcomes.append(self._over_budget(cluster, str(e)))
                    continue
                report.stopped_by_budget = str(e)
                report.outcomes.append(
                    ClusterOutcome(cluster.id, cluster.name, "skipped", reason=str(e))
                )
                break
            report.outcomes.append(outcome)
        if self.runner is not None:
            report.credits = self.runner.spent()
        return report

    def _over_budget(self, cluster: Cluster, why: str) -> ClusterOutcome:
        """Park a cluster that reached budget.cluster_credits for a human decision."""
        reason = f"{why}; raise budget.cluster_credits, then decide 'migrate' to retry"
        with self.store.session() as s:
            row = s.get(Cluster, cluster.id)
        record = {**(row.result.get("migration") or {}), "state": State.NEEDS_DECISION.value,
                  "reason": reason, "question": reason, "finished_at": _now()}  # fmt: skip
        return self._finish(row, self._members(row), State.NEEDS_DECISION, record, reason=reason)

    def _refresh(self, clusters: list[Cluster]) -> list[Cluster]:
        with self.store.session() as s:
            return [s.get(Cluster, c.id) for c in clusters]

    def _migrate_cluster(self, cluster: Cluster, deps: list[Cluster]) -> ClusterOutcome:
        members = self._members(cluster)
        net = cluster.result.get("net_diff_by_project", {})
        fold_errors = cluster.result.get("fold_errors", [])
        # What the cluster changes per project: the net diff, or the members' own diffs where
        # there is none (fold errors, or a dropped cluster a human revived).
        changes = dict(net) if not fold_errors else {}
        for m in members:
            if m.project not in net or fold_errors:
                changes[m.project] = changes.get(m.project, "") + load_diff(self.config, m)
        files = {p: split_diff(d) for p, d in changes.items()}
        keys = {f"{p}:{fd.path}" for p, fds in files.items() for fd in fds}
        keys |= {f"{m.project}:{f['path']}" for m in members for f in m.footprint.get("files", [])}
        fates = self._fates(keys)

        # Target-side paths each project's worktree needs, following upstream renames/moves.
        expected: dict[str, set[str]] = {}
        deletions: dict[str, set[str]] = {}
        checkout: dict[str, set[str]] = {}
        for project, fds in files.items():
            for fd in fds:
                fate = fates.get(f"{project}:{fd.path}")
                loc = fate.target_location if fate else (project, fd.path)
                if loc is None:  # deleted upstream: its neighbours are where to look first
                    expected.setdefault(project, set())
                    checkout.setdefault(project, set()).add(fd.path)
                    continue
                expected.setdefault(loc[0], set()).add(loc[1])
                checkout.setdefault(loc[0], set()).add(loc[1])
                if fd.new_path is None:
                    deletions.setdefault(loc[0], set()).add(loc[1])
        for m in members:
            expected.setdefault(m.project, set())
        projects = sorted(p for p in expected if self.trees.target_repo(p) is not None)

        base_of: dict[str, str] = {}
        for dep in deps:  # later dependencies win: they were built on the earlier ones
            for project, info in (dep.result.get("migration") or {}).get("projects", {}).items():
                if info.get("head"):
                    base_of[project] = info["head"]

        notes = engineer_notes.active(self.store)
        knowledge = engineer_notes.cluster_knowledge(
            notes, cluster, members, engineer_notes.cluster_paths(cluster, members)
        )
        record: dict = {"started_at": _now(), "projects": {}, "agent": None, "review": None,
                        "members": [m.id for m in members], "knowledge": knowledge}  # fmt: skip
        worktrees: dict[str, ProjectWorktree] = {}
        try:
            self.workspaces.remove(self.run.id, cluster.id, projects)
            for project in projects:
                worktrees[project] = self.workspaces.create(
                    self.run.id,
                    cluster.id,
                    project,
                    dirs_for(sorted(checkout.get(project, set()))),
                    base=base_of.get(project),
                )
        except WorkspaceError as e:
            return self._finish(cluster, members, State.FAILED, record, reason=str(e))

        pending: dict[str, ladder.RungResult] = {}
        for project, wt in worktrees.items():
            info = {"branch": wt.branch, "base": wt.base, "path": str(wt.path)}
            record["projects"][project] = info
            own = [m for m in members if m.project == project]
            result = None
            if own and all(m.commit_sha for m in own):
                result = ladder.cherry_pick(wt, [m.commit_sha for m in own])
                info["cherry_pick"] = result.to_json()
            if (result is None or not result.ok) and project in net and not fold_errors:
                result = ladder.apply_net_diff(wt, self._applicable(project, net[project], own))
                info["net_diff"] = result.to_json()
                if result.ok:
                    ladder.commit(wt, self._message(cluster, own or members, "net_diff"))
            if result is None:
                info["strategy"] = None  # only here as a possible home for moved code
                continue
            if result.ok:
                info["strategy"] = result.strategy
                info["head"] = wt.head()
            else:
                pending[project] = result

        # Human feedback on an earlier attempt, or an engineer's note about this cluster: the
        # mechanical result is only a starting point.
        feedback = bool(cluster.result.get("feedback")) or knowledge["specific"] is not None
        if not pending and not feedback:
            strategies = {i["strategy"] for i in record["projects"].values() if i.get("strategy")}
            strategy = "net_diff" if "net_diff" in strategies else "cherry_pick"
            if not strategies:
                return self._finish(
                    cluster, members, State.FAILED, record, reason="nothing could be applied"
                )
            record["strategy"] = strategy
            return self._finish(
                cluster,
                members,
                State.APPLIED,
                record,
                reason=f"applied by {strategy.replace('_', '-')}",
                confidence=DETERMINISTIC_CONFIDENCE[strategy],
                strategy=strategy,
            )

        if self.runner is None or not self.config.migrate.agent:
            record["strategy"] = "needs_agent"
            self._save(cluster, record)
            return ClusterOutcome(
                cluster.id,
                cluster.name,
                "needs_agent",
                reason=(
                    "cherry-pick and net-diff failed in " + ", ".join(sorted(pending))
                    if pending
                    else "reviewer feedback or an engineer note needs the agent"
                ),
            )
        ctx = self._context(cluster, members, changes, fates)
        return self._agent(cluster, members, ctx, worktrees, pending, expected, deletions, record)

    def _applicable(self, project: str, net_diff: str, members: list[Patch]) -> str:
        """The net diff as git can apply it: refolded with the binary files it changes."""
        if not has_binary(net_diff) or not members:
            return net_diff
        diffs = [load_diff(self.config, m, binary=True) for m in members]
        diff, error = fold(self.trees, project, diffs, binary=True)
        return diff if error is None and diff is not None else net_diff

    # ---- the agent rung ---------------------------------------------------------------

    def context_for(self, cluster: Cluster, members: list[Patch]) -> ClusterContext:
        """The agents' view of a cluster that was already migrated (for build fixes)."""
        net = cluster.result.get("net_diff_by_project", {})
        keys = {f"{p}:{fd.path}" for p, d in net.items() for fd in split_diff(d)}
        return self._context(cluster, members, net, self._fates(keys))

    def _context(
        self, cluster: Cluster, members: list[Patch], net: dict[str, str], fates: dict
    ) -> ClusterContext:
        intents = [
            f"#{m.id} {m.subject}:\n{m.intent or m.message.strip() or '(no intent recorded)'}"
            for m in members
        ]
        fate_lines = []
        for fate in sorted(fates.values(), key=lambda f: f.key):
            line = f"- {fate.key}: {fate.status}"
            if (loc := fate.target_location) and loc != (fate.project, fate.path):
                line += f" -> {loc[0]}/{loc[1]}"
            if fate.changed_signatures:
                line += f"; signatures changed: {', '.join(sorted(fate.changed_signatures))}"
            if fate.removed_symbols:
                line += f"; removed: {', '.join(fate.removed_symbols)}"
            for c in fate.commits[:3]:
                line += f"\n    upstream: {c}"
            fate_lines.append(line)
        return ClusterContext(
            cluster_id=cluster.id,
            name=cluster.name,
            members="\n".join(_Member(m).line for m in members),
            intent="\n\n".join(intents),
            net_diff="".join(tree_relative(d, p) for p, d in net.items()),
            fates="\n".join(fate_lines) or "(no upstream information)",
            memory=self._memory_text(members, net),
            notes=self._notes_text(cluster, members),
            analysis=self._analysis_text(members),
            review_feedback="\n".join(
                f"- {f['by']} ({f.get('source', 'review')}): {f['text']}"
                for f in cluster.result.get("feedback", [])
            ),
        )

    def _analysis_text(self, members: list[Patch]) -> str:
        """The analyst's solution for the backlog items the members belong to."""
        from patchmig.agents.analyst import solution_text
        from patchmig.ledger import BacklogItem

        keys = {(m.intent_detail or {}).get("item") or f"patch:{m.id}" for m in members}
        keys |= {k for m in members for k in m.jira_keys or []}
        with self.store.session() as s:
            items = s.scalars(
                select(BacklogItem)
                .where(BacklogItem.run_id == self.run.id, BacklogItem.key.in_(keys))
                .order_by(BacklogItem.id)
            ).all()
        return solution_text(list(items))

    def _notes_text(self, cluster: Cluster, members: list[Patch]) -> str:
        paths = engineer_notes.cluster_paths(cluster, members)
        notes = engineer_notes.active(self.store)
        return engineer_notes.render(
            engineer_notes.for_cluster(notes, cluster, members, paths, "migrate")
        )

    def _memory_text(self, members: list[Patch], changes: dict[str, str]) -> str:
        if self.memory is None:
            return ""
        files = [(p, fd.path) for p, d in changes.items() for fd in split_diff(d)]
        return self.memory.brief_for(members, files, self.config.memory.brief_max_chars)

    def _situation(self, projects: dict[str, dict], pending: dict[str, ladder.RungResult]) -> str:
        lines = []
        for project, info in projects.items():
            rel = f"{project}/"
            if project not in pending:
                if info.get("strategy"):
                    lines.append(
                        f"- `{rel}`: migrated by {info['strategy']} and committed; "
                        "change only what is needed."
                    )
                else:
                    lines.append(f"- `{rel}`: checked out because touched code moved here.")
                continue
            r = pending[project]
            if r.strategy == "net_diff" and r.conflicts:
                files = ", ".join(f"`{rel}{f}`" for f in r.conflicts)
                lines.append(
                    f"- `{rel}`: the 3-way apply of the net diff left conflict markers in "
                    f"{files}. Resolve them."
                )
            else:
                lines.append(
                    f"- `{rel}`: the patches do not apply ({r.error}). Make the change by hand "
                    "in the target's current code."
                )
        return "\n".join(lines)

    def _agent(
        self,
        cluster: Cluster,
        members: list[Patch],
        ctx: ClusterContext,
        worktrees: dict[str, ProjectWorktree],
        pending: dict[str, ladder.RungResult],
        expected: dict[str, set[str]],
        deletions: dict[str, set[str]],
        record: dict,
    ) -> ClusterOutcome:
        workdir = self.workspaces.cluster_dir(self.run.id, cluster.id)
        since = {p: wt.head() for p, wt in worktrees.items()}
        situation = self._situation(record["projects"], pending)
        feedback = ""
        attempts = self.config.migrate.guardrail_retries + 1
        report: guardrails.GuardrailReport | None = None
        answer: ClusterAnswer | None = None
        record["agent"] = {"attempts": []}
        for attempt in range(1, attempts + 1):
            try:
                outcome = ask_cluster_agent(
                    self.runner, self.config, ctx, workdir, situation, self.mcp, feedback, attempt
                )
            except AgentFailed as e:
                self._reset(worktrees, since)
                return self._finish(
                    cluster, members, State.FAILED, record, reason=f"agent failed: {e}"
                )
            answer = outcome.data
            entry = answer.model_dump()
            record["agent"]["attempts"].append(entry)
            if answer.outcome not in ("resolved", "rewritten"):
                break
            report = guardrails.check(
                list(worktrees.values()),
                since,
                expected,
                deletions,
                guardrails.diff_lines(ctx.net_diff),
                self.config.path_rules,
                self.config.migrate,
            )
            entry["guardrails"] = report.to_json()
            if report.ok or report.decisions:
                break
            feedback = "\n".join(f"- {h}" for h in report.hard)
        record["agent"]["final"] = answer.model_dump()

        if answer.outcome == "upstream":
            self._reset(worktrees, since)
            missing = self._unproven(answer)
            if answer.evidence and not missing:
                return self._finish(
                    cluster, members, State.UPSTREAM, record, actor=AGENT_ACTOR,
                    reason=f"agent: already in target ({answer.summary})",
                    confidence=answer.confidence,
                )  # fmt: skip
            question = (
                "The agent says the intent is already met in the target, but its evidence was "
                f"not found there ({'; '.join(missing) or 'no evidence given'}). Is it upstream?"
            )
            record["question"] = question
            return self._finish(cluster, members, State.NEEDS_DECISION, record, reason=question)
        if answer.outcome == "needs_decision":
            self._reset(worktrees, since)
            record["question"] = answer.question or answer.summary
            return self._finish(
                cluster, members, State.NEEDS_DECISION, record, actor=AGENT_ACTOR,
                reason=record["question"],
            )  # fmt: skip
        if answer.outcome == "failed":
            return self._finish(
                cluster, members, State.FAILED, record, actor=AGENT_ACTOR,
                reason=f"agent could not migrate: {answer.summary}",
            )  # fmt: skip
        record["guardrails"] = report.to_json()
        if report.decisions:
            record["question"] = "Agent change touches paths that need a decision: " + "; ".join(
                report.decisions
            )
            return self._finish(
                cluster, members, State.NEEDS_DECISION, record, reason=record["question"]
            )
        if not report.ok:
            return self._finish(
                cluster, members, State.FAILED, record,
                reason="guardrails failed: " + "; ".join(report.hard),
            )  # fmt: skip

        strategy = answer.outcome  # resolved / rewritten
        for project, wt in worktrees.items():
            own = [m for m in members if m.project == project] or members
            if ladder.commit(wt, self._message(cluster, own, strategy, answer.summary)):
                info = record["projects"][project]
                info["strategy"] = strategy
                info["head"] = wt.head()
        record["strategy"] = strategy
        confidence = answer.confidence
        if report.soft:
            confidence = min(confidence, self._below_quick())
        confidence, state, reason = self._review(
            cluster, ctx, worktrees, answer, report, record, confidence
        )
        target = state or (State.RESOLVED if strategy == "resolved" else State.REWRITTEN)
        return self._finish(
            cluster, members, target, record, actor=AGENT_ACTOR,
            reason=reason or f"{strategy} by the cluster agent: {answer.summary}",
            confidence=confidence, strategy=strategy,
        )  # fmt: skip

    def _below_quick(self) -> float:
        return below_quick(self.config.thresholds)

    def _review(self, cluster, ctx, worktrees, answer, report, record, confidence):
        if not self.config.migrate.review:
            return confidence, None, ""
        migrated = "".join(
            tree_relative(wt.git("diff", "-M", "--no-color", wt.base, "HEAD"), p)
            for p, wt in worktrees.items()
        )
        notes = "\n".join(f"- {n}" for n in report.soft)
        try:
            outcome = ask_reviewer(
                self.runner,
                self.config,
                ctx,
                self.workspaces.cluster_dir(self.run.id, cluster.id),
                migrated,
                answer.summary,
                notes,
                self.mcp,
            )
        except (AgentFailed, BudgetExceeded) as e:
            # The agent's change is paid for and passed the guardrails: keep it, unreviewed,
            # out of the quick-approve queue. (Over a cap, the next agent call stops the run.)
            record["review"] = {"error": str(e)}
            return min(confidence, self._below_quick()), None, ""
        review: ReviewAnswer = outcome.data
        record["review"] = review.model_dump()
        if review.verdict == "reject":
            return (
                0.0,
                State.FAILED,
                "rejected by the reviewer: " + "; ".join(review.issues or [review.summary]),
            )
        confidence = min(confidence, review.confidence)
        if review.verdict == "concerns" or not review.intent_met:
            confidence = min(confidence, self._below_quick())
        return confidence, None, ""

    def _unproven(self, answer: ClusterAnswer) -> list[str]:
        missing = []
        for ev in answer.evidence:
            text = self.trees.target_file(ev.project, ev.path)
            if text is None or _squash(ev.snippet) not in _squash(text):
                missing.append(f"{ev.project}/{ev.path}")
        return missing

    def _reset(self, worktrees: dict[str, ProjectWorktree], since: dict[str, str]) -> None:
        for project, wt in worktrees.items():
            ladder.reset(wt, since[project])

    # ---- recording --------------------------------------------------------------------

    def _message(
        self, cluster: Cluster, members: list[Patch], strategy: str, summary: str = ""
    ) -> str:
        src = self.config.migration.source_version
        lines = [cluster.name, ""]
        lines.append(f"Migrated from Android {src} by patchmig (strategy: {strategy}).")
        if summary:
            lines += ["", summary]
        lines += ["", "Original patches:"]
        lines += [f"  {(m.commit_sha or 'diff')[:12]} {m.subject}" for m in members]
        keys = sorted({k for m in members for k in m.jira_keys})
        trailers = [f"Bug: {k}" for k in keys]
        trailers += [f"Original-Change-Id: {m.change_id}" for m in members if m.change_id]
        if trailers:
            lines += [""] + trailers
        return "\n".join(lines) + "\n"

    def migrated_diff(self, record: dict) -> str:
        """The cluster's whole change on its branches (base..head of every project)."""
        parts = []
        for project, info in record["projects"].items():
            repo = self.trees.target_repo(project)
            if repo and info.get("head") and info["head"] != info["base"]:
                diff = git(repo, "diff", "-M", "--no-color", info["base"], info["head"])
                parts.append(tree_relative(diff, project))
        return "".join(parts)

    def _save(self, cluster: Cluster, record: dict, confidence: float | None = None) -> None:
        with self.store.session() as s, s.begin():
            row = s.get(Cluster, cluster.id)
            row.result = {**row.result, "migration": record}
            if confidence is not None:
                row.confidence = confidence

    def _finish(
        self,
        cluster: Cluster,
        members: list[Patch],
        state: State,
        record: dict,
        *,
        reason: str,
        actor: str = ACTOR,
        confidence: float | None = None,
        strategy: str | None = None,
    ) -> ClusterOutcome:
        record["finished_at"] = _now()
        record["state"] = state.value
        record["reason"] = reason
        if state in MIGRATED:
            record["diff"] = self.migrated_diff(record)
        self._save(cluster, record, confidence)
        for m in members:
            self.store.transition(m, state, actor=actor, reason=reason)
            if confidence is not None:
                with self.store.session() as s, s.begin():
                    s.get(Patch, m.id).confidence = confidence
        self.store.transition(cluster, state, actor=actor, reason=reason)
        return ClusterOutcome(cluster.id, cluster.name, state.value, strategy, reason, confidence)


def _squash(text: str) -> str:
    return " ".join(text.split())


def migrate(
    config: ProjectConfig,
    store: LedgerStore,
    run: Run,
    runner: AgentRunner | None,
    *,
    cluster_ids: list[int] | None = None,
    limit: int | None = None,
    mcp: dict | None = None,
    memory: Memory | None = None,
) -> MigrateReport:
    """Migrate the run's needed clusters. With runner=None only the free rungs run."""
    return Migrator(config, store, run, runner, mcp, memory).migrate(cluster_ids, limit)
