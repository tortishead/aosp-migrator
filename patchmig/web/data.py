"""Read-only views of the ledger for the front end, as plain JSON-ready dicts.

The FastAPI app (api.py) serves these over HTTP and the static exporter (export.py) writes
them to files, so both front ends show exactly the same data.
"""

from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime

from sqlalchemy import func, select

from patchmig import notes as engineer_notes
from patchmig.analysis.patches import load_diff
from patchmig.config import ProjectConfig
from patchmig.config.schema import ThresholdsConfig
from patchmig.ledger import Cluster, KnowledgeItem, LedgerStore, Patch, Run, State, Transition
from patchmig.ledger.models import AgentCall
from patchmig.review import queue_of, queues

MAX_DIFF_CHARS = 200_000


class NotFound(LookupError):
    pass


def _iso(dt) -> str | None:
    if dt is None:
        return None
    return (dt if dt.tzinfo else dt.replace(tzinfo=UTC)).isoformat()  # SQLite drops the zone


def _history(store: LedgerStore, entity: Patch | Cluster) -> list[dict]:
    return [
        {"from": t.from_state.value, "to": t.to_state.value, "actor": t.actor,
         "reason": t.reason, "forced": t.forced, "at": _iso(t.at)}
        for t in store.history(entity)
    ]  # fmt: skip


def _cut(text: str | None) -> str | None:
    if text is None or len(text) <= MAX_DIFF_CHARS:
        return text
    return text[:MAX_DIFF_CHARS] + f"\n[... {len(text) - MAX_DIFF_CHARS} more characters]\n"


def runs(store: LedgerStore) -> list[dict]:
    with store.session() as s:
        rows = s.scalars(select(Run).order_by(Run.id.desc())).all()
        counts = dict(s.execute(select(Patch.run_id, func.count()).group_by(Patch.run_id)).all())
        clusters = dict(
            s.execute(select(Cluster.run_id, func.count()).group_by(Cluster.run_id)).all()
        )
    return [
        {"id": r.id, "created_at": _iso(r.created_at), "note": r.note,
         "config_hash": r.config_hash[:12], "patches": counts.get(r.id, 0),
         "clusters": clusters.get(r.id, 0)}
        for r in rows
    ]  # fmt: skip


def _run(store: LedgerStore, run_id: int) -> Run:
    with store.session() as s:
        run = s.get(Run, run_id)
    if run is None:
        raise NotFound(f"run {run_id} not found")
    return run


def summary(store: LedgerStore, run_id: int) -> dict:
    run = _run(store, run_id)
    with store.session() as s:
        patches = s.scalars(select(Patch).where(Patch.run_id == run_id)).all()
        clusters = s.scalars(
            select(Cluster).where(Cluster.run_id == run_id).order_by(Cluster.order_index)
        ).all()
        calls = s.scalars(select(AgentCall).where(AgentCall.run_id == run_id)).all()
    snapshot = run.config_snapshot
    return {
        "run": {"id": run.id, "created_at": _iso(run.created_at), "note": run.note,
                "config_hash": run.config_hash[:12]},
        "project": snapshot.get("name"),
        "migration": snapshot.get("migration"),
        "patches": len(patches),
        "states": {st.value: n for st, n in Counter(p.state for p in patches).items()},
        "state_order": [st.value for st in State],
        "rungs": dict(Counter((p.verdict or {}).get("rung") or "none" for p in patches)),
        "projects": dict(Counter(p.project for p in patches)),
        "clusters": len(clusters),
        "cluster_states": {st.value: n for st, n in Counter(c.state for c in clusters).items()},
        "forecasts": dict(
            Counter((c.result.get("forecast") or {}).get("level") for c in clusters
                    if c.result.get("forecast"))
        ),
        "intents": sum(1 for p in patches if p.intent),
        "agent_calls": len(calls),
        "credits": round(sum(c.credits for c in calls), 2),
        "agent_failures": sum(1 for c in calls if not c.ok),
    }  # fmt: skip


def patches(store: LedgerStore, run_id: int) -> list[dict]:
    _run(store, run_id)
    with store.session() as s:
        rows = s.scalars(select(Patch).where(Patch.run_id == run_id).order_by(Patch.seq)).all()
    return [
        {"id": p.id, "seq": p.seq, "project": p.project, "subject": p.subject,
         "commit": p.commit_sha, "jira_keys": p.jira_keys, "role": p.chain_role,
         "state": p.state.value, "cluster_id": p.cluster_id,
         "rung": (p.verdict or {}).get("rung"), "confidence": p.confidence,
         "has_intent": bool(p.intent), "author": p.author,
         "files": len(p.footprint.get("files", []))}
        for p in rows
    ]  # fmt: skip


def patch(store: LedgerStore, config: ProjectConfig | None, run_id: int, patch_id: int) -> dict:
    with store.session() as s:
        p = s.get(Patch, patch_id)
    if p is None or p.run_id != run_id:
        raise NotFound(f"patch {patch_id} not in run {run_id}")
    diff = load_diff(config, p) if config else None
    return {
        "id": p.id, "seq": p.seq, "project": p.project, "subject": p.subject,
        "message": p.message, "commit": p.commit_sha, "diff_path": p.diff_path,
        "change_id": p.change_id, "fingerprint": p.fingerprint, "author": p.author,
        "authored_at": _iso(p.authored_at), "sources": p.sources, "jira_keys": p.jira_keys,
        "links": p.links, "footprint": p.footprint, "role": p.chain_role,
        "intent": p.intent, "intent_detail": p.intent_detail, "state": p.state.value,
        "verdict": p.verdict, "confidence": p.confidence, "cluster_id": p.cluster_id,
        "history": _history(store, p), "diff": _cut(diff), "notes": _notes_for_patch(store, p),
    }  # fmt: skip


def clusters(store: LedgerStore, run_id: int) -> list[dict]:
    _run(store, run_id)
    with store.session() as s:
        rows = s.scalars(
            select(Cluster).where(Cluster.run_id == run_id).order_by(Cluster.order_index)
        ).all()
        projects = dict(
            s.execute(select(Patch.id, Patch.project).where(Patch.run_id == run_id)).all()
        )
    by_order = {c.order_index: c.id for c in rows}
    return [
        {"id": c.id, "order": c.order_index, "name": c.name, "state": c.state.value,
         "members": c.result.get("members", []),
         "projects": sorted({projects[m] for m in c.result.get("members", [])}),
         "forecast": (c.result.get("forecast") or {}).get("level"),
         "depends_on": [by_order[o] for o in c.result.get("depends_on_order", [])],
         "fold_errors": len(c.result.get("fold_errors", []))}
        for c in rows
    ]  # fmt: skip


def cluster(store: LedgerStore, run_id: int, cluster_id: int) -> dict:
    with store.session() as s:
        c = s.get(Cluster, cluster_id)
        if c is None or c.run_id != run_id:
            raise NotFound(f"cluster {cluster_id} not in run {run_id}")
        members = s.scalars(
            select(Patch).where(Patch.id.in_(c.result.get("members", []))).order_by(Patch.seq)
        ).all()
        orders = dict(
            s.execute(select(Cluster.order_index, Cluster.id).where(Cluster.run_id == run_id)).all()
        )
    r = c.result
    return {
        "id": c.id, "order": c.order_index, "name": c.name, "state": c.state.value,
        "intent": c.intent, "confidence": c.confidence,
        "members": [
            {"id": p.id, "seq": p.seq, "project": p.project, "subject": p.subject,
             "state": p.state.value, "role": p.chain_role, "rung": (p.verdict or {}).get("rung")}
            for p in members
        ],
        "edges": r.get("edges", []), "cut_edges": r.get("cut_edges", []),
        "depends_on": [orders[o] for o in r.get("depends_on_order", [])],
        "forecast": r.get("forecast"), "fold_errors": r.get("fold_errors", []),
        "upstream_check": r.get("upstream_check"), "net_diff": _cut(c.net_diff),
        "migration": _migration(r.get("migration")),
        "verification": r.get("verification"),
        "review": r.get("review"), "feedback": r.get("feedback", []),
        "decisions": r.get("decisions", []), "publish": _publish(r.get("publish")),
        "jira_export": r.get("jira_export"), "queue": _queue(store, c),
        "history": _history(store, c), "notes": _notes_for_cluster(store, c, list(members)),
    }  # fmt: skip


def _note(n, stale: bool = False) -> dict:
    return {"id": n.id, "where": engineer_notes.where(n), "stages": n.stages, "text": n.text,
            "author": n.author, "updated_at": _iso(n.updated_at), "stale": stale}  # fmt: skip


def _notes_for_patch(store: LedgerStore, p: Patch) -> list[dict]:
    return [_note(n) for n in engineer_notes.active(store) if engineer_notes.applies_to_patch(n, p)]


def _notes_for_cluster(store: LedgerStore, c: Cluster, members: list[Patch]) -> list[dict]:
    """The notes the agents get for this cluster; marked when added or edited since migration."""
    paths = engineer_notes.cluster_paths(c, members)
    used = set(((c.result.get("migration") or {}).get("knowledge") or {}).get("notes", []))
    started = (c.result.get("migration") or {}).get("started_at")
    migrated_at = datetime.fromisoformat(started) if started else None
    out = []
    for n in engineer_notes.for_cluster(engineer_notes.active(store), c, members, paths, "migrate"):
        updated = n.updated_at.replace(tzinfo=UTC, microsecond=0)  # as precise as started_at
        changed = migrated_at is not None and (n.id not in used or updated > migrated_at)
        out.append(_note(n, changed))
    return out


def _migration(record: dict | None) -> dict | None:
    if not record:
        return None
    projects = [
        {"project": p, **{k: v for k, v in info.items() if k != "path"}}
        for p, info in record.get("projects", {}).items()
    ]
    return {**record, "projects": projects, "diff": _cut(record.get("diff"))}


def _publish(record: dict | None) -> dict | None:
    if not record:
        return None
    prs = [{"project": p, **e} for p, e in record.get("projects", {}).items()]
    return {"at": record.get("at"), "error": record.get("error"), "prs": prs}


def _thresholds(store: LedgerStore, run_id: int) -> ThresholdsConfig:
    with store.session() as s:
        run = s.get(Run, run_id)
    return ThresholdsConfig(**((run.config_snapshot or {}).get("thresholds") or {}))


def _queue(store: LedgerStore, cluster: Cluster) -> str:
    return queue_of(cluster, _thresholds(store, cluster.run_id))


def review(store: LedgerStore, run_id: int) -> dict:
    _run(store, run_id)
    out = {}
    for name, clusters in queues(store, run_id, _thresholds(store, run_id)).items():
        out[name] = [_queue_item(c) for c in clusters]
    return {"queues": out, "counts": {k: len(v) for k, v in out.items()}}


def _queue_item(c: Cluster) -> dict:
    r = c.result
    prs = (r.get("publish") or {}).get("projects", {})
    return {
        "id": c.id, "name": c.name, "state": c.state.value, "confidence": c.confidence,
        "prs": [e["url"] for e in prs.values()],
        "question": (r.get("migration") or {}).get("question"),
        "reason": (r.get("verification") or r.get("migration") or {}).get("reason"),
        "review": r.get("review"),
        "jira": sorted((r.get("jira_export") or {}).keys()),
    }  # fmt: skip


def knowledge(store: LedgerStore, run_id: int) -> dict:
    _run(store, run_id)
    with store.session() as s:
        items = s.scalars(
            select(KnowledgeItem).where(KnowledgeItem.run_id == run_id).order_by(KnowledgeItem.key)
        ).all()
    footprints = {i.key: i.data for i in items if i.kind == "footprint"}
    fates = [i.data for i in items if i.kind == "file_fate"]
    return {
        "footprints": footprints,
        "fates": fates,
        "fate_counts": dict(Counter(f["status"] for f in fates)),
    }


def agent_calls(store: LedgerStore, run_id: int) -> list[dict]:
    _run(store, run_id)
    with store.session() as s:
        rows = s.scalars(
            select(AgentCall).where(AgentCall.run_id == run_id).order_by(AgentCall.id)
        ).all()
    return [
        {"id": c.id, "agent": c.agent, "task": c.task_key, "attempt": c.attempt,
         "model": c.model, "started_at": _iso(c.started_at), "duration_s": round(c.duration_s, 1),
         "credits": c.credits, "ok": c.ok, "error": c.error}
        for c in rows
    ]  # fmt: skip


def recent_transitions(store: LedgerStore, run_id: int, limit: int = 50) -> list[dict]:
    with store.session() as s:
        rows = s.scalars(
            select(Transition)
            .where(Transition.run_id == run_id)
            .order_by(Transition.id.desc())
            .limit(limit)
        ).all()
    return [
        {"entity": t.entity_type, "id": t.entity_id, "from": t.from_state.value,
         "to": t.to_state.value, "actor": t.actor, "reason": t.reason, "at": _iso(t.at)}
        for t in rows
    ]  # fmt: skip


def config_snapshot(store: LedgerStore, run_id: int) -> dict:
    """The config the run used. Secrets in it are references (env:/file:), never values."""
    run = _run(store, run_id)
    return {"hash": run.config_hash, "config": run.config_snapshot}
