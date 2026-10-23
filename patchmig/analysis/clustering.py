"""Group related patches into clusters, the unit of migration.

Edges between patches (strongest first): explicit links (revert, fixes), a shared Jira key,
a symbol one patch introduces and a later one uses, linked Jira tickets, and overlapping
changed lines in the same file. Connected components become clusters; components that are
too large are split at their weakest edges, and the cut edges become dependencies between
the resulting clusters. Clusters are ordered so that dependencies come first.
"""

from __future__ import annotations

import heapq
import re
from collections import defaultdict
from dataclasses import dataclass, field

from patchmig.analysis.diffs import overlaps
from patchmig.analysis.patches import PatchView
from patchmig.knowledge.trees import Trees

WEIGHTS = {"revert": 3, "fixes": 3, "jira": 2, "symbol": 2, "jira_link": 1, "overlap": 1}
_WORD = re.compile(r"\b[A-Za-z_]\w*\b")


@dataclass(frozen=True)
class Edge:
    a: int  # patch id, the earlier patch
    b: int  # patch id, the later patch
    kind: str
    detail: str = ""

    @property
    def weight(self) -> int:
        return WEIGHTS[self.kind]

    def to_json(self) -> dict:
        return {"from": self.a, "to": self.b, "kind": self.kind, "detail": self.detail}


@dataclass
class ClusterPlan:
    members: list[PatchView]  # in patch-set order
    edges: list[Edge] = field(default_factory=list)  # inside the cluster
    cut: list[Edge] = field(default_factory=list)  # removed when splitting
    depends_on: set[int] = field(default_factory=set)  # indexes into the ordered plan list

    @property
    def projects(self) -> set[str]:
        return {m.project for m in self.members}


def _ordered(a: PatchView, b: PatchView) -> tuple[PatchView, PatchView]:
    return (a, b) if a.patch.seq <= b.patch.seq else (b, a)


def _edge(a: PatchView, b: PatchView, kind: str, detail: str = "") -> Edge:
    first, second = _ordered(a, b)
    return Edge(first.id, second.id, kind, detail)


def _introduced(view: PatchView, trees: Trees, min_len: int) -> set[str]:
    """Symbols the patch declares that its files did not contain at the source base."""
    names = {s for s in view.patch.footprint.get("defined", []) if len(s) >= min_len}
    if not names:
        return set()
    base = "\n".join(
        trees.source_base_file(view.project, f.old_path) or "" for f in view.files if f.old_path
    )
    return {n for n in names if not re.search(rf"\b{re.escape(n)}\b", base)}


def find_edges(
    views: list[PatchView], trees: Trees, ticket_links: dict[str, list[str]], min_symbol_len: int
) -> list[Edge]:
    by_key = {v.key: v for v in views}
    edges: set[Edge] = set()

    for v in views:
        links = v.patch.links
        if (target := links.get("reverts")) in by_key:
            edges.add(_edge(by_key[target], v, "revert"))
        for target in links.get("fixes", []):
            if target in by_key:
                edges.add(_edge(by_key[target], v, "fixes"))

    by_jira: dict[str, list[PatchView]] = defaultdict(list)
    for v in sorted(views, key=lambda v: v.patch.seq):
        for key in v.patch.jira_keys:
            by_jira[key].append(v)
    for key, group in by_jira.items():
        for a, b in zip(group, group[1:], strict=False):  # a chain is enough to connect them
            edges.add(_edge(a, b, "jira", key))
    for key, linked in ticket_links.items():
        for other in linked:
            if by_jira.get(key) and by_jira.get(other) and key < other:
                edges.add(_edge(by_jira[key][0], by_jira[other][0], "jira_link", f"{key}~{other}"))

    definers: dict[str, PatchView] = {}
    for v in sorted(views, key=lambda v: v.patch.seq):
        used = {w for f in v.files for line in f.added for w in _WORD.findall(line)}
        for name in sorted(used & definers.keys()):
            if definers[name].id != v.id:
                edges.add(_edge(definers[name], v, "symbol", name))
        for name in _introduced(v, trees, min_symbol_len):
            definers.setdefault(name, v)

    by_file: dict[tuple[str, str], list[tuple[PatchView, list]]] = defaultdict(list)
    for v in views:
        for f in v.files:
            by_file[(v.project, f.path)].append((v, f.changed))
    for (_, path), group in by_file.items():
        for i, (a, ra) in enumerate(group):
            for b, rb in group[i + 1 :]:
                if a.id != b.id and overlaps(ra, rb, pad=1):
                    edges.add(_edge(a, b, "overlap", path))
    return sorted(edges, key=lambda e: (e.a, e.b, e.kind))


def _components(ids: list[int], edges: list[Edge]) -> list[list[int]]:
    parent = {i: i for i in ids}

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for e in edges:
        parent[find(e.a)] = find(e.b)
    groups: dict[int, list[int]] = defaultdict(list)
    for i in ids:
        groups[find(i)].append(i)
    return list(groups.values())


def _split(
    ids: list[int], edges: list[Edge], fits, cut: list[Edge]
) -> list[tuple[list[int], list[Edge]]]:
    """Remove the weakest edges of an oversized component until every part fits."""
    if fits(ids) or not edges:
        return [(ids, edges)]
    weakest = min(e.weight for e in edges)
    cut += [e for e in edges if e.weight == weakest]
    kept = [e for e in edges if e.weight > weakest]
    parts = []
    for comp in _components(ids, kept):
        members = set(comp)
        parts += _split(comp, [e for e in kept if e.a in members], fits, cut)
    return parts


def plan_clusters(
    views: list[PatchView], edges: list[Edge], max_patches: int, max_projects: int
) -> list[ClusterPlan]:
    by_id = {v.id: v for v in views}

    def fits(ids: list[int]) -> bool:
        return len(ids) <= max_patches and len({by_id[i].project for i in ids}) <= max_projects

    plans: list[ClusterPlan] = []
    for comp in _components(list(by_id), edges):
        members = set(comp)
        inner = [e for e in edges if e.a in members]
        cut: list[Edge] = []
        for ids, kept in _split(comp, inner, fits, cut):
            views_sorted = sorted((by_id[i] for i in ids), key=lambda v: v.patch.seq)
            own = set(ids)
            plans.append(
                ClusterPlan(views_sorted, kept, [e for e in cut if e.a in own or e.b in own])
            )
    return _order(plans)


def _order(plans: list[ClusterPlan]) -> list[ClusterPlan]:
    """Dependencies first; otherwise by the position of each cluster's first patch."""
    where = {m.id: i for i, p in enumerate(plans) for m in p.members}
    deps: dict[int, set[int]] = {i: set() for i in range(len(plans))}
    for i, p in enumerate(plans):
        for e in p.cut:
            if where[e.b] == i and where[e.a] != i:
                deps[i].add(where[e.a])
    first_seq = [p.members[0].patch.seq for p in plans]
    ready = [(first_seq[i], i) for i in deps if not deps[i]]
    heapq.heapify(ready)
    order: list[int] = []
    waiting = {i: set(d) for i, d in deps.items() if d}
    while ready:
        _, i = heapq.heappop(ready)
        order.append(i)
        for j in list(waiting):
            waiting[j].discard(i)
            if not waiting[j]:
                del waiting[j]
                heapq.heappush(ready, (first_seq[j], j))
    order += sorted(waiting, key=lambda i: first_seq[i])  # cycles can't happen; be safe
    position = {old: new for new, old in enumerate(order)}
    result = [plans[i] for i in order]
    for new, old in enumerate(order):
        result[new].depends_on = {position[d] for d in deps[old]}
    return result
