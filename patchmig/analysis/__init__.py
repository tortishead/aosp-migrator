"""Analysis, deterministic part: which patches are still needed, clusters, folded net diffs.

Everything here costs no LLM credits. The Copilot part of the analysis step (understanding
each backlog item, whether it is still needed, related items, a solution) is
patchmig.agents.analyst; it runs after this and moves patches through `refresh_clusters`.

In order:

1. Knowledge: the OEM footprint map and the upstream change map of every touched file.
2. Per patch not decided yet: revert folding, then the upstream rungs (identity, provenance,
   textual) and the obsolete rule; path rules can park a patch for a human decision.
3. Clustering, ordered by dependency.
4. Per cluster: the net diff of the patches still needed, a textual upstream check of that
   net diff (a chain can be upstream as a whole), and a difficulty forecast.

It can run again at any time, after an import brought new patches: patches already decided
keep their verdict, clusters already migrated (or approved) stay as they are, and the new
patches join the clusters not migrated yet or form new ones after them.

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
from patchmig.analysis.patches import PatchView, load_diff, view
from patchmig.analysis.reverts import cancelled
from patchmig.config import ProjectConfig
from patchmig.gitutil import has_binary
from patchmig.knowledge import history
from patchmig.knowledge.footprint import footprint_map
from patchmig.knowledge.trees import Trees
from patchmig.knowledge.upstream import FileFate, build_change_map
from patchmig.ledger import (
    BacklogItem,
    Cluster,
    KnowledgeItem,
    LedgerStore,
    Patch,
    Run,
    State,
    Ticket,
    can_transition,
)

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
    decided: int = 0  # patches decided by this analysis (the new ones)
    clusters_new: int = 0
    clusters_changed: int = 0


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
        diffs = [
            load_diff(config, m.patch, binary=True) if has_binary(m.diff) else m.diff
            for m in active
            if m.project == project
        ]
        diff, error = fold(trees, project, diffs)
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


def is_locked(cluster: Cluster) -> bool:
    """A cluster the analysis must leave alone: migration started, or a human approved it."""
    return bool(cluster.result.get("migration")) or (
        (cluster.result.get("review") or {}).get("decision") == "approved"
    )


def _fates(
    store: LedgerStore, run: Run, trees: Trees, files: set[tuple[str, str]]
) -> dict[tuple[str, str], FileFate]:
    """The upstream fate of every file: those known from earlier analyses, the rest built."""
    with store.session() as s:
        rows = s.scalars(select(KnowledgeItem).where(
            KnowledgeItem.run_id == run.id, KnowledgeItem.kind == "file_fate"))  # fmt: skip
        known = {(f.project, f.path): f for f in (FileFate.from_json(r.data) for r in rows)}
    missing = files - set(known)
    built = build_change_map(trees, missing) if missing else {}
    with store.session() as s, s.begin():
        for fate in built.values():
            s.add(KnowledgeItem(run_id=run.id, kind="file_fate", key=fate.key,
                                data=fate.to_json()))  # fmt: skip
    return {**known, **built}


def analyse(config: ProjectConfig, store: LedgerStore, run: Run) -> AnalysisReport:
    with store.session() as s:
        patches = s.scalars(select(Patch).where(Patch.run_id == run.id).order_by(Patch.seq)).all()
        clusters = s.scalars(
            select(Cluster).where(Cluster.run_id == run.id).order_by(Cluster.order_index)
        ).all()
        tickets = {t.key: t.issue_links for t in s.scalars(select(Ticket))}
        items = s.scalar(
            select(func.count()).select_from(BacklogItem).where(BacklogItem.run_id == run.id)
        )
    if not patches and not items:
        raise AnalysisError(f"run {run.id} has an empty backlog; run migrate ingest first")
    trees = Trees(config)
    views = [view(config, p) for p in patches]
    for v in views:
        if v.patch.state != State.INGESTED:
            v.state, v.verdict, v.confidence = v.patch.state, v.patch.verdict, v.patch.confidence
    new = [v for v in views if v.patch.state == State.INGESTED]
    fates = _fates(store, run, trees, {k for v in views for k in v.file_keys()})

    paths: dict[str, set[str]] = {}
    for fate in fates.values():
        paths.setdefault(fate.project, set()).add(fate.path)
        if fate.new_path and not fate.new_project:
            paths[fate.project].add(fate.new_path)
    index = rungs.IdentityIndex(trees, paths)
    cancelled_with = cancelled(views)
    for v in new:
        _decide(v, cancelled_with, index, fates, trees, config)

    locked = {c.id: c for c in clusters if is_locked(c)}
    free = [v for v in views if v.patch.cluster_id not in locked]
    results: list[_ClusterResult] = []
    edges: list = []
    if new:
        ticket_links = {k: links for k, links in tickets.items() if links}
        edges = find_edges(views, trees, ticket_links, config.analysis.min_symbol_length)
        free_ids = {v.id for v in free}
        plans = plan_clusters(
            free, [e for e in edges if e.a in free_ids and e.b in free_ids],
            config.analysis.max_cluster_patches, config.analysis.max_cluster_projects,
        )  # fmt: skip
        results = [_fold_cluster(plan, fates, trees, config) for plan in plans]

    saved = _save(store, run, views, results, clusters, locked, edges,
                  footprint_map(views, config.analysis.hot_spot_min_patches))  # fmt: skip
    with store.session() as s:
        n_clusters = s.scalar(
            select(func.count()).select_from(Cluster).where(Cluster.run_id == run.id)
        )
        all_clusters = s.scalars(select(Cluster).where(Cluster.run_id == run.id)).all()
    states = Counter(v.state.value for v in views)
    return AnalysisReport(
        run_id=run.id,
        states=dict(states),
        clusters=n_clusters,
        forecasts=dict(
            Counter(c.result["forecast"]["level"] for c in all_clusters if c.result.get("forecast"))
        ),  # fmt: skip
        rungs=dict(Counter(v.verdict.get("rung") or "none" for v in views)),
        warnings=history.gaps(config, sorted({v.project for v in views}))
        + trees.warnings
        + [e for r in results for e in r.fold_errors]
        + saved["warnings"],
        decided=len(new),
        clusters_new=saved["new"],
        clusters_changed=saved["changed"],
    )


def _cluster_result(r: _ClusterResult) -> dict:
    return {
        "members": [m.id for m in r.plan.members],
        "edges": [e.to_json() for e in r.plan.edges],
        "cut_edges": [e.to_json() for e in r.plan.cut],
        "forecast": r.forecast,
        "net_diff_by_project": r.net_by_project,
        "fold_errors": r.fold_errors,
        "upstream_check": r.upstream_check,
    }


def _net_diff(r: _ClusterResult) -> str | None:
    return "".join(tree_relative(d, p) for p, d in r.net_by_project.items()) or None


def move(store: LedgerStore, entity, target: State, reason: str, actor: str = ACTOR) -> None:
    """Transition, through `needed` when the table has no direct way (e.g. obsolete to
    needs_decision): the history shows both steps."""
    if entity.state == target:
        return
    if not can_transition(entity.state, target) and entity.state != State.NEEDED:
        entity = store.transition(entity, State.NEEDED, actor=actor, reason=reason)
    store.transition(entity, target, actor=actor, reason=reason)


def _save(
    store: LedgerStore,
    run: Run,
    views: list[PatchView],
    results: list[_ClusterResult],
    clusters: list[Cluster],
    locked: dict[int, Cluster],
    edges: list,
    footprints: dict[str, dict],
) -> dict:
    """Write verdicts, clusters and knowledge. Clusters not locked that a plan covers keep
    their id (and their notes, decisions and order); a plan covering none is a new cluster
    after all existing ones; a free cluster merged into another is removed."""
    out = {"new": 0, "changed": 0, "warnings": []}
    by_id = {c.id: c for c in clusters}
    orders = {c.order_index for c in clusters}
    next_order = max((o for o in orders if o is not None), default=-1) + 1
    with store.session() as s, s.begin():
        for row in s.scalars(select(KnowledgeItem).where(
                KnowledgeItem.run_id == run.id, KnowledgeItem.kind == "footprint")):  # fmt: skip
            s.delete(row)
        for project, data in footprints.items():
            s.add(KnowledgeItem(run_id=run.id, kind="footprint", key=project, data=data))
        plan_rows: list[Cluster] = []
        used: set[int] = set()
        for r in results:
            old_ids = sorted({m.patch.cluster_id for m in r.plan.members
                              if m.patch.cluster_id in by_id and m.patch.cluster_id not in used},
                             key=lambda i: by_id[i].order_index)  # fmt: skip
            if old_ids:
                row = s.get(Cluster, old_ids[0])
                used.add(row.id)
                if set(row.result.get("members", [])) != {m.id for m in r.plan.members}:
                    out["changed"] += 1
                row.result = {**row.result, **_cluster_result(r)}
                row.net_diff = _net_diff(r)
            else:
                row = Cluster(run_id=run.id, name=r.plan.members[0].patch.subject[:200],
                              order_index=next_order, net_diff=_net_diff(r), confidence=None,
                              result=_cluster_result(r))  # fmt: skip
                next_order += 1
                s.add(row)
                out["new"] += 1
            plan_rows.append(row)
        s.flush()
        where = {
            m.id: row for r, row in zip(results, plan_rows, strict=True) for m in r.plan.members
        }
        for r, row in zip(results, plan_rows, strict=True):
            deps = {plan_rows[d].order_index for d in r.plan.depends_on}
            members = {m.id for m in r.plan.members}
            for e in edges:  # a new patch built on a cluster already migrated
                if e.b in members and e.a not in where:
                    other = next(
                        (c for c in locked.values() if e.a in c.result.get("members", [])), None
                    )
                    if other is not None:
                        deps.add(other.order_index)
                        out["warnings"].append(
                            f"cluster {row.id} depends on cluster {other.id}, already migrated: "
                            f"{e.kind} {e.detail}".rstrip(": ")
                            + "; rerun it to take both together"
                        )
            row.result = {**row.result, "depends_on_order": sorted(deps)}
            for m in r.plan.members:
                p = s.get(Patch, m.id)
                p.cluster_id, p.verdict, p.confidence = row.id, m.verdict, m.confidence
        for c in clusters:  # free clusters every plan passed by: merged into another
            if c.id not in used and c.id not in locked and results:
                s.delete(s.get(Cluster, c.id))
                out["warnings"].append(f"cluster {c.id} was merged into another by new patches")

    for v in views:
        if v.state != v.patch.state:
            reason = v.verdict.get("question") or _reason(v.verdict)
            move(store, v.patch, v.state, reason)
    for r, row in zip(results, plan_rows, strict=True):
        with store.session() as s:
            row = s.get(Cluster, row.id)
        if row.state != r.state:
            level = (r.forecast or {}).get("level")
            move(store, row, r.state, f"forecast: {level}" if level else "all dropped")
    return out


def refresh_clusters(
    config: ProjectConfig, store: LedgerStore, run: Run, cluster_ids: set[int], actor: str
) -> list[str]:
    """Fold again the clusters (not locked) whose patches changed state, and move them.

    Called after the analyst decided patches; returns warnings.
    """
    trees = Trees(config)
    warnings: list[str] = []
    for cid in sorted(cluster_ids):
        with store.session() as s:
            c = s.get(Cluster, cid)
            members = s.scalars(
                select(Patch).where(Patch.cluster_id == cid).order_by(Patch.seq)
            ).all()
        if c is None or is_locked(c):
            continue
        views = [view(config, m) for m in members]
        for v in views:
            v.state, v.verdict, v.confidence = v.patch.state, v.patch.verdict, v.patch.confidence
        fates = _fates(store, run, trees, {k for v in views for k in v.file_keys()})
        r = _fold_cluster(ClusterPlan(views), fates, trees, config)
        warnings += r.fold_errors
        with store.session() as s, s.begin():
            row = s.get(Cluster, cid)
            keep = {k: v for k, v in _cluster_result(r).items() if k not in ("edges", "cut_edges")}
            row.result = {**row.result, **keep}
            row.net_diff = _net_diff(r)
            for v in views:
                s.get(Patch, v.id).verdict = v.verdict
        for v in views:
            if v.state != v.patch.state:
                move(store, v.patch, v.state, _reason(v.verdict), actor)
        level = (r.forecast or {}).get("level")
        move(store, c, r.state, f"forecast: {level}" if level else "all dropped", actor)
    return warnings


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
