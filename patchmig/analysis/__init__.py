"""Analysis: decide which patches are still needed, cluster them, and fold each cluster.

Everything here is deterministic and costs no LLM credits. In order:

1. Knowledge: the OEM footprint map and the upstream change map of every touched file.
2. Per patch: revert folding, then the upstream rungs (identity, provenance, textual) and
   the obsolete rule; path rules can park a patch for a human decision.
3. Clustering of all patches, ordered by dependency.
4. Per cluster: the net diff of the patches still needed, a textual upstream check of that
   net diff (a chain can be upstream as a whole), and a difficulty forecast.

Results go to the ledger through LedgerStore.transition, with the evidence for each verdict.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from fnmatch import fnmatch

from sqlalchemy import func, select

from patchmig.analysis import upstream as rungs
from patchmig.analysis.clustering import ClusterPlan, find_edges, plan_clusters
from patchmig.analysis.diffs import split_diff
from patchmig.analysis.fold import fold, tree_relative
from patchmig.analysis.forecast import forecast
from patchmig.analysis.patches import PatchView, view
from patchmig.analysis.reverts import cancelled
from patchmig.config import ProjectConfig
from patchmig.knowledge import history
from patchmig.knowledge.footprint import footprint_map
from patchmig.knowledge.trees import Trees
from patchmig.knowledge.upstream import FileFate, build_change_map
from patchmig.ledger import Cluster, KnowledgeItem, LedgerStore, Patch, Run, State, Ticket

ACTOR = "orchestrator"
ACTIVE = (State.NEEDED, State.NEEDS_DECISION)


class AnalysisError(Exception):
    pass


@dataclass
class AnalysisReport:
    run_id: int
    states: dict[str, int]
    clusters: int
    forecasts: dict[str, int]
    rungs: dict[str, int]  # which rung decided how many patches
    warnings: list[str] = field(default_factory=list)


def _decide(
    v: PatchView,
    cancelled_with: dict[str, str],
    index: rungs.IdentityIndex,
    fates: dict[tuple[str, str], FileFate],
    trees: Trees,
    config: ProjectConfig,
) -> None:
    if v.key in cancelled_with:
        partner = cancelled_with[v.key]
        v.state, v.confidence = State.REVERTED, 1.0
        v.verdict = {"verdict": "reverted", "rung": "revert", "evidence": {"cancels_with": partner}}
        return
    checks: list[rungs.Check] = []
    for check in (
        lambda: rungs.identity(v, index),
        lambda: rungs.provenance(v, trees),
        lambda: rungs.textual(
            v.project, v.files, fates, trees, config.analysis.textual_min_line_chars
        ),
        lambda: rungs.obsolete(v, fates),
    ):
        result = check()
        checks.append(result)
        if result.conclusive:
            v.state, v.confidence = State(result.state), result.confidence
            v.verdict = {
                "verdict": result.state,
                "rung": result.rung,
                "evidence": result.evidence,
                "checks": [c.to_json() for c in checks],
            }
            return
    v.state = State.NEEDED
    v.verdict = {"verdict": "needed", "rung": None, "checks": [c.to_json() for c in checks]}
    for rule in config.path_rules:
        hits = [f.path for f in v.files if fnmatch(f"{v.project}/{f.path}", rule.pattern)]
        if hits and rule.rule in ("needs_decision", "forbidden"):
            v.state = State.NEEDS_DECISION
            v.verdict["question"] = f"{rule.rule}: {rule.reason} ({', '.join(hits[:3])})"
            return
        if hits:
            v.verdict.setdefault("notes", []).append(f"{rule.rule}: {rule.reason}")


@dataclass
class _ClusterResult:
    plan: ClusterPlan
    state: State
    net_by_project: dict[str, str]
    fold_errors: list[str]
    forecast: dict | None
    upstream_check: dict | None


def _cluster_state(members: list[PatchView]) -> State:
    states = [m.state for m in members]
    if State.NEEDS_DECISION in states:
        return State.NEEDS_DECISION
    if State.NEEDED in states:
        return State.NEEDED
    priority = [State.UPSTREAM, State.OBSOLETE, State.REVERTED]
    counts = Counter(states)
    return max(priority, key=lambda s: (counts[s], -priority.index(s)))


def _fold_cluster(
    plan: ClusterPlan, fates: dict[tuple[str, str], FileFate], trees: Trees, config: ProjectConfig
) -> _ClusterResult:
    active = [m for m in plan.members if m.state in ACTIVE]
    net: dict[str, str] = {}
    errors: list[str] = []
    for project in sorted({m.project for m in active}):
        diff, error = fold(trees, project, [m.diff for m in active if m.project == project])
        if error:
            errors.append(error)
        elif diff is not None:
            net[project] = diff

    upstream_check = None
    if active and not errors and any(net.values()):
        checks = [
            rungs.textual(p, split_diff(d), fates, trees, config.analysis.textual_min_line_chars)
            for p, d in net.items()
        ]
        upstream_check = {
            "matched": sum(c.evidence["matched"] for c in checks),
            "total": sum(c.evidence["total"] for c in checks),
            "conclusive": all(c.conclusive for c in checks),
        }
        if upstream_check["conclusive"]:
            for m in active:
                m.state, m.confidence = State.UPSTREAM, 0.85
                m.verdict = {
                    **m.verdict,
                    "verdict": "upstream",
                    "rung": "textual_cluster",
                    "evidence": {"cluster_net_diff": upstream_check},
                }
            active = []

    files = (
        [(p, fd) for p, d in net.items() for fd in split_diff(d)]
        if not errors
        else [(m.project, fd) for m in active for fd in m.files]
    )
    return _ClusterResult(
        plan=plan,
        state=_cluster_state(plan.members),
        net_by_project=net,
        fold_errors=errors,
        forecast=forecast(files, fates) if active else None,
        upstream_check=upstream_check,
    )


def analyse(config: ProjectConfig, store: LedgerStore, run: Run) -> AnalysisReport:
    with store.session() as s:
        patches = s.scalars(select(Patch).where(Patch.run_id == run.id).order_by(Patch.seq)).all()
        n_clusters = s.scalar(
            select(func.count()).select_from(Cluster).where(Cluster.run_id == run.id)
        )
        tickets = {t.key: t.issue_links for t in s.scalars(select(Ticket))}
    if not patches:
        raise AnalysisError(f"run {run.id} has no patches; run migrate ingest first")
    if n_clusters or any(p.state != State.INGESTED for p in patches):
        raise AnalysisError(f"run {run.id} was already analysed; start a new run with ingest")

    trees = Trees(config)
    views = [view(config, p) for p in patches]
    files = {k for v in views for k in v.file_keys()}
    fates = build_change_map(trees, files)

    paths: dict[str, set[str]] = {}
    for fate in fates.values():
        paths.setdefault(fate.project, set()).add(fate.path)
        if fate.new_path and not fate.new_project:
            paths[fate.project].add(fate.new_path)
    index = rungs.IdentityIndex(trees, paths)
    cancelled_with = cancelled(views)
    for v in views:
        _decide(v, cancelled_with, index, fates, trees, config)

    ticket_links = {k: links for k, links in tickets.items() if links}
    edges = find_edges(views, trees, ticket_links, config.analysis.min_symbol_length)
    plans = plan_clusters(
        views, edges, config.analysis.max_cluster_patches, config.analysis.max_cluster_projects
    )
    results = [_fold_cluster(plan, fates, trees, config) for plan in plans]

    _save(
        store,
        run,
        views,
        results,
        fates,
        footprint_map(views, config.analysis.hot_spot_min_patches),
    )
    states = Counter(v.state.value for v in views)
    return AnalysisReport(
        run_id=run.id,
        states=dict(states),
        clusters=len(results),
        forecasts=dict(Counter(r.forecast["level"] for r in results if r.forecast)),
        rungs=dict(Counter(v.verdict.get("rung") or "none" for v in views)),
        warnings=history.gaps(config, sorted({v.project for v in views}))
        + trees.warnings
        + [e for r in results for e in r.fold_errors],
    )


def _save(
    store: LedgerStore,
    run: Run,
    views: list[PatchView],
    results: list[_ClusterResult],
    fates: dict[tuple[str, str], FileFate],
    footprints: dict[str, dict],
) -> None:
    with store.session() as s, s.begin():
        for project, data in footprints.items():
            s.add(KnowledgeItem(run_id=run.id, kind="footprint", key=project, data=data))
        for fate in fates.values():
            s.add(KnowledgeItem(run_id=run.id, kind="file_fate", key=fate.key, data=fate.to_json()))
        clusters = []
        for order, r in enumerate(results):
            first = r.plan.members[0].patch
            cluster = Cluster(
                run_id=run.id,
                name=first.subject[:200],
                order_index=order,
                net_diff="".join(tree_relative(d, p) for p, d in r.net_by_project.items()) or None,
                confidence=None,
                result={
                    "members": [m.id for m in r.plan.members],
                    "edges": [e.to_json() for e in r.plan.edges],
                    "cut_edges": [e.to_json() for e in r.plan.cut],
                    "depends_on_order": sorted(r.plan.depends_on),
                    "forecast": r.forecast,
                    "net_diff_by_project": r.net_by_project,
                    "fold_errors": r.fold_errors,
                    "upstream_check": r.upstream_check,
                },
            )
            s.add(cluster)
            clusters.append(cluster)
        s.flush()
        for cluster, r in zip(clusters, results, strict=True):
            for m in r.plan.members:
                row = s.get(Patch, m.id)
                row.cluster_id = cluster.id
                row.verdict = m.verdict
                row.confidence = m.confidence

    for v in views:
        reason = v.verdict.get("question") or _reason(v.verdict)
        store.transition(v.patch, v.state, actor=ACTOR, reason=reason)
    with store.session() as s:
        rows = s.scalars(
            select(Cluster).where(Cluster.run_id == run.id).order_by(Cluster.order_index)
        ).all()
    for cluster, r in zip(rows, results, strict=True):
        level = (r.forecast or {}).get("level")
        store.transition(
            cluster, r.state, actor=ACTOR, reason=f"forecast: {level}" if level else "all dropped"
        )


def _reason(verdict: dict) -> str:
    rung = verdict.get("rung")
    if not rung:
        return "no deterministic rung concluded"
    ev = verdict.get("evidence", {})
    if rung == "identity":
        return f"same {ev['match']} as target commit {ev['target_commit'][:12]}"
    if rung == "provenance":
        return f"cherry-picked from {ev['origin'][:12]}, which the target contains"
    if rung == "textual":
        return f"all {ev['total']} changed lines already in the target"
    if rung == "textual_cluster":
        return "the cluster's net diff is already in the target"
    if rung == "obsolete":
        return f"files deleted upstream: {', '.join(ev['deleted'])}"
    if rung == "revert":
        return f"cancelled out with {ev['cancels_with'][:12]}"
    return rung
