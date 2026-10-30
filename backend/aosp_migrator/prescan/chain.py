"""Chains of items: which items an item builds on, in which order to migrate them, and the patches a build applies.

An item needs another when one of its current diffs has a `pending` dependency on a
current diff of the other (`prescan.deps`: written on a version only that diff makes),
and when both are in a cluster and the other comes first in cluster order. Needs are
followed transitively. Items that need each other (a cycle) form one unit, migrated
together. Units come prerequisites first; among units ready at the same time, and inside
a unit, cluster members go by cluster order, the rest after them, each by key.

A rework patch (level in `REWORK_LEVELS`) is made for an item's project on top of HEAD
plus its prerequisites' patches (`base_patch_ids`). While not rejected, the newest one
per project replaces the `original` patches of the item's diffs in that project. It is
stale when the project HEAD moved, a base patch is no longer its item's current patch, or
the item got a newer diff version; a stale patch stays what a build applies until the
item is reworked.
"""

import heapq
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session, aliased

from aosp_migrator.db.models import Attachment, ClusterMember, DiffDependency, Item, JiraIssue, MigrationPatch
from aosp_migrator.prescan import git
from aosp_migrator.prescan.runner import current_diffs, current_patch, diff_scan
from aosp_migrator.prescan.runner import stale_items as scan_stale  # Extended below

REWORK_LEVELS = ("stacked", "source3way", "resolve", "port", "reimpl")

# Sort key of an item for ties: cluster members first, by position, then the rest; then key.
Tie = tuple[bool, int, str]


@dataclass
class Chain:
    # Item ids, prerequisites first; several in one unit when they need each other.
    units: list[list[int]]
    # Direct prerequisites of every item in the chain.
    needs: dict[int, set[int]]
    keys: dict[int, str]


def order(needs: Mapping[int, Iterable[int]], tie: Mapping[int, Tie]) -> list[list[int]]:
    """Every item of `tie` in units, prerequisites first; a cycle (strongly connected items) is one unit."""
    units = _cycles(needs, list(tie))
    unit_of = {item: n for n, unit in enumerate(units) for item in unit}
    blockers = [{unit_of[p] for item in unit for p in needs.get(item, ()) if p in unit_of} - {n} for n, unit in enumerate(units)]
    unblocks: dict[int, list[int]] = {}
    for n, before in enumerate(blockers):
        for b in before:
            unblocks.setdefault(b, []).append(n)
    ordered = [sorted(unit, key=tie.__getitem__) for unit in units]
    ready = [(tie[ordered[n][0]], n) for n, before in enumerate(blockers) if not before]
    heapq.heapify(ready)
    out: list[list[int]] = []
    while ready:
        _, n = heapq.heappop(ready)
        out.append(ordered[n])
        for m in unblocks.get(n, []):
            blockers[m].discard(n)
            if not blockers[m]:
                heapq.heappush(ready, (tie[ordered[m][0]], m))
    return out


def _cycles(needs: Mapping[int, Iterable[int]], nodes: list[int]) -> list[list[int]]:
    """Strongly connected components (Tarjan, iterative), edges outside `nodes` ignored."""
    known = set(nodes)
    index: dict[int, int] = {}
    low: dict[int, int] = {}
    on_stack: set[int] = set()
    stack: list[int] = []
    out: list[list[int]] = []
    for root in nodes:
        if root in index:
            continue
        work = [(root, iter(sorted(p for p in needs.get(root, ()) if p in known)))]
        index[root] = low[root] = len(index)
        stack.append(root)
        on_stack.add(root)
        while work:
            node, edges = work[-1]
            for nxt in edges:
                if nxt not in index:
                    index[nxt] = low[nxt] = len(index)
                    stack.append(nxt)
                    on_stack.add(nxt)
                    work.append((nxt, iter(sorted(p for p in needs.get(nxt, ()) if p in known))))
                    break
                if nxt in on_stack:
                    low[node] = min(low[node], index[nxt])
            else:
                work.pop()
                if work:
                    low[work[-1][0]] = min(low[work[-1][0]], low[node])
                if low[node] == index[node]:
                    unit = []
                    while True:
                        member = stack.pop()
                        on_stack.discard(member)
                        unit.append(member)
                        if member == node:
                            break
                    out.append(unit)
    return out


def item_needs(session: Session, workspace_id: int) -> dict[int, set[int]]:
    """Direct prerequisites of every item of the workspace that has some: `pending` diff dependencies and earlier cluster members."""
    mine, theirs = aliased(Attachment), aliased(Attachment)
    my_item, their_item = aliased(Item), aliased(Item)
    needs: dict[int, set[int]] = {}
    for my_id, their_id in session.execute(
        select(my_item.id, their_item.id)
        .select_from(DiffDependency)
        .join(mine, mine.id == DiffDependency.attachment_id)
        .join(my_item, my_item.issue_id == mine.issue_id)
        .join(theirs, theirs.id == DiffDependency.depends_on_id)
        .join(their_item, their_item.issue_id == theirs.issue_id)
        .where(DiffDependency.workspace_id == workspace_id, DiffDependency.state == "pending", mine.is_current.is_(True), theirs.is_current.is_(True))
    ):
        if my_id != their_id:
            needs.setdefault(my_id, set()).add(their_id)
    clusters: dict[int, list[int]] = {}
    for cluster_id, item_id in session.execute(
        select(ClusterMember.cluster_id, ClusterMember.item_id)
        .join(Item, Item.id == ClusterMember.item_id)
        .where(Item.workspace_id == workspace_id)
        .order_by(ClusterMember.cluster_id, ClusterMember.position, ClusterMember.id)
    ):
        members = clusters.setdefault(cluster_id, [])
        if members:
            needs.setdefault(item_id, set()).add(members[-1])
        members.append(item_id)
    return needs


def chain(session: Session, workspace_id: int, item_ids: Iterable[int]) -> Chain:
    """The items and every item they need, transitively, in units, prerequisites first."""
    needs = item_needs(session, workspace_id)
    seen: set[int] = set()
    todo = list(item_ids)
    while todo:
        item_id = todo.pop()
        if item_id not in seen:
            seen.add(item_id)
            todo += needs.get(item_id, ())
    rows = session.execute(
        select(Item.id, JiraIssue.key, ClusterMember.position)
        .join(JiraIssue, Item.issue_id == JiraIssue.id)
        .outerjoin(ClusterMember, ClusterMember.item_id == Item.id)
        .where(Item.id.in_(seen))
    )
    keys: dict[int, str] = {}
    tie: dict[int, Tie] = {}
    for item_id, key, position in rows:
        keys[item_id] = key
        tie[item_id] = (position is None, position or 0, key)
    chained = {item_id: needs.get(item_id, set()) & seen for item_id in seen}
    return Chain(order(chained, tie), chained, keys)


def patch_plan(session: Session, item: Item) -> list[MigrationPatch | Attachment]:
    """What a build applies for the item, in diff order: per current diff the newest rework patch of its project
    that is not rejected (once per project), else the diff's current patch, else the diff itself (it has none).
    """
    rework: dict[str, MigrationPatch] = {}
    for patch in session.scalars(
        select(MigrationPatch)
        .where(MigrationPatch.item_id == item.id, MigrationPatch.level.in_(REWORK_LEVELS), MigrationPatch.review != "rejected")
        .order_by(MigrationPatch.id)
    ):
        rework[patch.project] = patch
    plan: list[MigrationPatch | Attachment] = []
    for attachment in current_diffs(session, item.issue_id):
        scan = diff_scan(session, attachment.id)
        patch = rework.get(scan.project) if scan is not None and scan.project is not None else None
        if patch is None:
            patch = current_patch(session, scan)
        if patch is None:
            plan.append(attachment)
        elif patch not in plan:
            plan.append(patch)
    return plan


def current_patches(session: Session, item: Item) -> list[MigrationPatch]:
    return [p for p in patch_plan(session, item) if isinstance(p, MigrationPatch)]


def is_rework(patch: MigrationPatch) -> bool:
    return patch.level in REWORK_LEVELS


def base_patches(session: Session, patch: MigrationPatch) -> list[MigrationPatch]:
    """The patch's base patches in apply order; ones deleted since are left out."""
    found = {p.id: p for p in session.scalars(select(MigrationPatch).where(MigrationPatch.id.in_(patch.base_patch_ids)))}
    return [found[i] for i in patch.base_patch_ids if i in found]


def patch_stale(session: Session, tree: Path, patch: MigrationPatch, heads: dict[str, str | None] | None = None) -> list[str]:
    """Why a patch on a base is out of date: its project HEAD moved, a base patch is no longer its item's
    current patch, or its item has a diff version newer than the patch. `heads` caches project HEADs.
    """
    heads = {} if heads is None else heads
    reasons: list[str] = []
    if patch.project not in heads:
        heads[patch.project] = git.head(tree / patch.project) if (tree / patch.project).is_dir() else None
    if heads[patch.project] != patch.base_head:
        reasons.append(f"{patch.project} HEAD moved")
    current: dict[int, set[int]] = {}
    bases = base_patches(session, patch)
    for base in bases:
        if base.item_id not in current:
            owner = session.get(Item, base.item_id)
            current[base.item_id] = {p.id for p in current_patches(session, owner)} if owner is not None else set()
        if base.id not in current[base.item_id]:
            key = session.scalar(select(JiraIssue.key).join(Item, Item.issue_id == JiraIssue.id).where(Item.id == base.item_id))
            reasons.append(f"base {key} changed")
    if len(bases) != len(patch.base_patch_ids):
        reasons.append("base patch gone")
    item = session.get(Item, patch.item_id)
    if item is not None and is_rework(patch) and any(a.created > patch.created_at for a in current_diffs(session, item.issue_id)):
        reasons.append("new diff version")
    return list(dict.fromkeys(reasons))


def stale_items(session: Session, workspace_id: int, tree: Path, item_ids: list[int] | None = None) -> dict[int, list[str]]:
    """`runner.stale_items`, plus why the current rework patches of items that have one are out of date (`patch_stale`)."""
    stale = scan_stale(session, workspace_id, tree, item_ids)
    query = select(Item).join(MigrationPatch, MigrationPatch.item_id == Item.id).where(Item.workspace_id == workspace_id, MigrationPatch.level.in_(REWORK_LEVELS)).distinct()
    if item_ids is not None:
        query = query.where(Item.id.in_(item_ids))
    heads: dict[str, str | None] = {}
    for item in session.scalars(query):
        reasons = [r for patch in current_patches(session, item) if is_rework(patch) for r in patch_stale(session, tree, patch, heads)]
        if reasons:
            stale[item.id] = list(dict.fromkeys([*stale.get(item.id, []), *reasons]))
    return stale
