"""Engineer notes: extra knowledge for the agents, and what a note change makes stale.

An engineer adds a note when the patches and tickets leave out something the agents need:
"the OEM power HAL moved to vendor/oem/power in this target", "OEM-512 is only for the
China variant", "keep the old API, the infotainment app still calls it". Notes go into the
briefs of the step they are for: `analyse` (the analyst) and `migrate` (the cluster agent,
the reviewer and the build fixer).

Every agent result records a fingerprint of the notes (and, for clusters, the intents) it
was given. When notes change, the analyst's `stale_items` and `stale_clusters` here find
exactly the results that were made with different knowledge, so a rerun pays only for those.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime
from fnmatch import fnmatch

from sqlalchemy import select

from patchmig.ledger import Cluster, LedgerStore, Note, Patch, Run, State
from patchmig.ledger.states import DROPPED, MIGRATED
from patchmig.memory import change_ids

SCOPES = ("all", "path", "patch", "cluster", "ticket")
STAGES = ("analyse", "migrate")
# Notes about one patch, cluster or ticket: specific enough that the cluster agent should
# act on them even where cherry-pick or the net diff would apply cleanly.
SPECIFIC = frozenset({"patch", "cluster", "ticket"})
# Cluster states a rerun can reopen (back to `needed`): every outcome of migrate or verify.
REOPENABLE = frozenset({*MIGRATED, State.VERIFIED, State.FAILED, State.NEEDS_DECISION, *DROPPED})


class NoteError(Exception):
    pass


def _now() -> datetime:
    return datetime.now(UTC)


# ---- editing ----------------------------------------------------------------------------


def add(
    store: LedgerStore,
    text: str,
    by: str,
    scope: str = "all",
    target: str = "",
    label: str = "",
    stages: list[str] | None = None,
) -> Note:
    if not text.strip():
        raise NoteError("a note needs text")
    if scope not in SCOPES:
        raise NoteError(f"scope must be one of {', '.join(SCOPES)}")
    if (scope == "all") != (not target):
        raise NoteError("scope 'all' takes no target; every other scope needs one")
    stages = list(stages or STAGES)
    if bad := [s for s in stages if s not in STAGES]:
        raise NoteError(f"unknown stage {', '.join(bad)}; use {', '.join(STAGES)}")
    with store.session() as s, s.begin():
        note = Note(scope=scope, target=target, label=label, stages=stages, text=text.strip(),
                    author=by, revisions=[])  # fmt: skip
        s.add(note)
    return note


def add_for_run(
    store: LedgerStore,
    run: Run,
    text: str,
    by: str,
    scope: str = "all",
    value: str | int | None = None,
    stages: list[str] | None = None,
) -> Note:
    """Add a note whose target is given as this run sees it: a path glob, a patch or cluster
    id of the run, or a Jira key. A patch is stored by its key, so the note follows it."""
    from patchmig.ledger import Ticket
    from patchmig.memory import patch_key

    target, label = "", ""
    if scope != "all" and (value is None or str(value).strip() == ""):
        raise NoteError(f"a note for one {scope} needs the {scope}")
    with store.session() as s:
        if scope == "path":
            target = str(value).strip()
        elif scope in ("patch", "cluster"):
            try:
                ident = int(value)
            except (TypeError, ValueError) as e:
                raise NoteError(f"{scope} must be an id") from e
            row = s.get(Patch if scope == "patch" else Cluster, ident)
            if row is None or row.run_id != run.id:
                raise NoteError(f"{scope} {ident} not in run {run.id}")
            if scope == "patch":
                if not (target := patch_key(row) or ""):
                    raise NoteError(f"patch {ident} not in run {run.id}")
                label = f"#{row.id} {row.subject}"[:200]
            else:
                target, label = str(row.id), row.name
        elif scope == "ticket":
            target = str(value).strip()
            t = s.get(Ticket, target)
            label = t.summary if t else ""
    return add(store, text, by, scope, target, label, stages)


def get(store: LedgerStore, note_id: int) -> Note:
    with store.session() as s:
        note = s.get(Note, note_id)
    if note is None or note.removed_at is not None:
        raise NoteError(f"note {note_id} not found")
    return note


def edit(
    store: LedgerStore,
    note_id: int,
    by: str,
    text: str | None = None,
    stages: list[str] | None = None,
) -> Note:
    get(store, note_id)
    if text is not None and not text.strip():
        raise NoteError("a note needs text; use remove to delete it")
    if stages is not None and (bad := [s for s in stages if s not in STAGES]):
        raise NoteError(f"unknown stage {', '.join(bad)}; use {', '.join(STAGES)}")
    with store.session() as s, s.begin():
        note = s.get(Note, note_id)
        old = {"text": note.text, "stages": note.stages, "by": note.author,
               "at": note.updated_at.isoformat(timespec="seconds")}  # fmt: skip
        note.revisions = [*note.revisions, old]
        if text is not None:
            note.text = text.strip()
        if stages is not None:
            note.stages = list(stages)
        note.author = by
        note.updated_at = _now()
    return get(store, note_id)


def remove(store: LedgerStore, note_id: int, by: str) -> None:
    get(store, note_id)
    with store.session() as s, s.begin():
        note = s.get(Note, note_id)
        note.removed_at = _now()
        note.revisions = [
            *note.revisions,
            {"removed_by": by, "at": note.removed_at.isoformat(timespec="seconds")},
        ]


def active(store: LedgerStore) -> list[Note]:
    with store.session() as s:
        return list(s.scalars(select(Note).where(Note.removed_at.is_(None)).order_by(Note.id)))


# ---- which notes apply ------------------------------------------------------------------


def _paths(patch: Patch) -> list[str]:
    return [f"{patch.project}/{f['path']}" for f in patch.footprint.get("files", [])]


def applies_to_patch(note: Note, patch: Patch) -> bool:
    if note.scope == "all":
        return True
    if note.scope == "path":
        return any(fnmatch(p, note.target) for p in _paths(patch))
    if note.scope == "patch":
        return note.target in change_ids(patch) or note.target == patch.fingerprint
    if note.scope == "cluster":
        return patch.cluster_id is not None and note.target == str(patch.cluster_id)
    return note.target in (patch.jira_keys or [])


def for_patch(notes: list[Note], patch: Patch, stage: str) -> list[Note]:
    return [n for n in notes if stage in n.stages and applies_to_patch(n, patch)]


def for_cluster(
    notes: list[Note], cluster: Cluster, members: list[Patch], paths: list[str], stage: str
) -> list[Note]:
    """Notes for a cluster: its own, its members', and path notes matching its files."""
    out = []
    for n in notes:
        if stage not in n.stages:
            continue
        if (
            (n.scope == "cluster" and n.target == str(cluster.id))
            or (n.scope == "path" and any(fnmatch(p, n.target) for p in paths))
            or any(applies_to_patch(n, m) for m in members)
        ):
            out.append(n)
    return out


def where(note: Note) -> str:
    if note.scope == "all":
        return "all patches"
    label = f" ({note.label})" if note.label else ""
    return f"{note.scope} {note.target}{label}"


def render(notes: list[Note]) -> str:
    return "\n".join(f"- (note {n.id}, {n.author}, for {where(n)}) {n.text}" for n in notes)


def fingerprint(notes: list[Note], extra: list[str] = ()) -> str | None:
    """What an agent was told, in 16 hex digits; None when there was nothing to tell."""
    parts = [f"{n.id}:{n.text}" for n in notes] + [e for e in extra if e]
    if not parts:
        return None
    return hashlib.sha256("\n\x00".join(parts).encode()).hexdigest()[:16]


def cluster_knowledge(
    notes: list[Note], cluster: Cluster, members: list[Patch], paths: list[str]
) -> dict:
    """The fingerprints a migration records: all notes plus intents, and specific notes."""
    mine = for_cluster(notes, cluster, members, paths, "migrate")
    intents = [f"{m.id}:{m.intent or ''}" for m in members]
    return {
        "all": fingerprint(mine, intents),
        "specific": fingerprint([n for n in mine if n.scope in SPECIFIC]),
        "notes": [n.id for n in mine],
    }


# ---- what is stale ----------------------------------------------------------------------


@dataclass
class Stale:
    kind: str  # patch or cluster
    id: int
    name: str
    state: str
    why: str


def migration_members(store: LedgerStore, cluster: Cluster) -> list[Patch]:
    """The patches a migration attempt worked on (recorded since notes; else inferred)."""
    record = cluster.result.get("migration") or {}
    with store.session() as s:
        if ids := record.get("members"):
            q = select(Patch).where(Patch.id.in_(ids))
        else:
            q = select(Patch).where(Patch.cluster_id == cluster.id,
                                    Patch.state.in_(list(REOPENABLE - DROPPED)))  # fmt: skip
        return list(s.scalars(q.order_by(Patch.seq)))


def cluster_paths(cluster: Cluster, members: list[Patch]) -> list[str]:
    from patchmig.analysis.diffs import split_diff

    paths = {p for m in members for p in _paths(m)}
    for project, diff in (cluster.result.get("net_diff_by_project") or {}).items():
        paths |= {f"{project}/{fd.path}" for fd in split_diff(diff)}
    return sorted(paths)


def stale_clusters(store: LedgerStore, run: Run) -> list[Stale]:
    """Migrated (or failed) clusters whose knowledge changed since their migration.

    A cluster the agent worked on is stale when any of its notes or member intents changed.
    One applied mechanically only when a note about it specifically (its patches, the
    cluster or a ticket) changed: other notes would not change a cherry-pick. Approved
    clusters are never stale; reopen them explicitly.
    """
    notes = active(store)
    with store.session() as s:
        clusters = s.scalars(
            select(Cluster).where(Cluster.run_id == run.id).order_by(Cluster.order_index)
        ).all()
    out = []
    for c in clusters:
        record = c.result.get("migration")
        if not record or c.state not in REOPENABLE:
            continue
        if (c.result.get("review") or {}).get("decision") == "approved":
            continue
        members = migration_members(store, c)
        now = cluster_knowledge(notes, c, members, cluster_paths(c, members))
        then = record.get("knowledge") or {}
        used_agent = record.get("agent") is not None or c.state in (
            State.FAILED, State.NEEDS_DECISION)  # fmt: skip
        if now["specific"] != then.get("specific"):
            out.append(Stale("cluster", c.id, c.name, c.state.value,
                             "a note about this cluster changed"))  # fmt: skip
        elif used_agent and now["all"] != then.get("all"):
            out.append(Stale("cluster", c.id, c.name, c.state.value,
                             "its notes or intents changed"))  # fmt: skip
    return out


def reopen(store: LedgerStore, run: Run, cluster_id: int, actor: str, reason: str) -> Cluster:
    """Send a migrated, verified, failed or decided cluster back to `needed` for a rerun."""
    with store.session() as s:
        c = s.get(Cluster, cluster_id)
    if c is None or c.run_id != run.id:
        raise NoteError(f"cluster {cluster_id} is not in run {run.id}")
    if c.state == State.NEEDED:
        return c
    if c.state not in REOPENABLE or not c.result.get("migration"):
        raise NoteError(f"cluster {cluster_id} is {c.state} and was not migrated; nothing to rerun")
    for m in migration_members(store, c):
        if m.state != State.NEEDED and m.state in REOPENABLE:
            store.transition(m, State.NEEDED, actor=actor, reason=reason)
    if (c.result.get("review") or {}).get("decision") == "approved":
        with store.session() as s, s.begin():
            row = s.get(Cluster, c.id)
            review = {**row.result["review"], "decision": "reopened", "reopened": reason}
            row.result = {**row.result, "review": review}
    return store.transition(c, State.NEEDED, actor=actor, reason=reason)


def rerun(
    store: LedgerStore, run: Run, cluster_ids: list[int], actor: str, reason: str
) -> tuple[list[int], list[str]]:
    """Reopen these clusters and every migrated cluster built on top of them.

    A dependent cluster's branch starts from its dependency's head, so it is migrated again
    on the new result. Approved dependents are left alone and reported. Returns the reopened
    cluster ids (in migration order) and messages for the engineer.
    """
    with store.session() as s:
        clusters = s.scalars(
            select(Cluster).where(Cluster.run_id == run.id).order_by(Cluster.order_index)
        ).all()
    by_id = {c.id: c for c in clusters}
    order_of = {c.id: c.order_index for c in clusters}
    todo = set(cluster_ids)
    messages: list[str] = []
    for c in clusters:  # in order, so dependencies are decided before their dependents
        if c.id in todo:
            continue
        deps = {d.id for d in clusters if d.order_index in c.result.get("depends_on_order", [])}
        if not deps & todo or not c.result.get("migration"):
            continue
        if (c.result.get("review") or {}).get("decision") == "approved":
            messages.append(f"cluster {c.id} is approved but built on a rerun cluster; "
                            "reopen it with --redo --cluster if it must follow")  # fmt: skip
            continue
        todo.add(c.id)
    reopened = []
    for cid in sorted(todo, key=lambda i: order_of.get(i, 0)):
        if cid not in by_id:
            raise NoteError(f"cluster {cid} is not in run {run.id}")
        why = reason if cid in cluster_ids else f"{reason} (built on a rerun cluster)"
        reopen(store, run, cid, actor, why)
        reopened.append(cid)
    return reopened, messages
