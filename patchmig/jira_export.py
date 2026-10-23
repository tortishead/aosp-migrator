"""Export approved results back to Jira, without custom fields.

Only clusters a human approved are exported. Per Jira ticket of their patches:

- a label per outcome: `<migration label>-migrated`, `-upstream`, `-obsolete`, `-reverted`
- a remote link to each PR (idempotent through its globalId)
- one comment, found by `<comment_marker> <migration label>` and updated in place

Patches without a ticket get one tracking ticket per cluster when asked (create_missing);
its key is added to those patches, so the next migration finds them through Jira.
Running the export again changes nothing that is already there.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import select

from patchmig.config import ProjectConfig
from patchmig.jira_client import JiraError, JiraWriter
from patchmig.ledger import Cluster, LedgerStore, Patch, Run, State
from patchmig.review import APPROVABLE

OUTCOME_LABEL = {
    State.VERIFIED: "migrated",
    State.UPSTREAM: "upstream",
    State.OBSOLETE: "obsolete",
    State.REVERTED: "reverted",
}


@dataclass
class TicketExport:
    key: str | None  # None: a tracking ticket to create
    cluster_ids: list[int]
    labels: list[str]
    links: list[tuple[str, str, str]]  # url, title, global id
    comment: str
    patch_ids: list[int] = field(default_factory=list)
    summary: str | None = None  # for a ticket to create

    def to_json(self) -> dict:
        return {
            "key": self.key,
            "cluster_ids": self.cluster_ids,
            "labels": self.labels,
            "links": [{"url": u, "title": t} for u, t, _ in self.links],
            "comment": self.comment,
            "summary": self.summary,
        }


@dataclass
class ExportReport:
    updated: list[str] = field(default_factory=list)
    created: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def marker(config: ProjectConfig) -> str:
    return f"{config.jira.comment_marker if config.jira else '[migration-agent]'} " + (
        config.migration.label
    )


def _outcome(config: ProjectConfig, p: Patch, cluster: Cluster) -> str:
    tv = config.migration.target_version
    v = p.verdict or {}
    ev = v.get("evidence") or {}
    if p.state == State.VERIFIED:
        mig = cluster.result.get("migration") or {}
        strategy = ((mig.get("projects") or {}).get(p.project) or {}).get("strategy") or mig.get(
            "strategy", "migrated"
        )
        ver = cluster.result.get("verification") or {}
        return f"migrated to Android {tv} ({strategy.replace('_', '-')}); {ver.get('reason', '')}"
    if p.state == State.UPSTREAM:
        if ev.get("target_commit"):
            return f"not needed: already in Android {tv} as {ev['target_commit'][:12]}"
        if ev.get("origin"):
            return f"not needed: its original commit {ev['origin'][:12]} is in Android {tv}"
        return f"not needed: Android {tv} already has this change"
    if p.state == State.OBSOLETE:
        return f"not needed: the code it changes was removed in Android {tv}"
    return "not needed: cancelled out by a revert in the patch set"


def _comment(config: ProjectConfig, run: Run, rows: list[tuple[Patch, Cluster]]) -> str:
    m = config.migration
    lines = [marker(config),
             f"Migration Android {m.source_version} → Android {m.target_version} "
             f"(patchmig run {run.id}). Updated automatically; edits here are overwritten.",
             ""]  # fmt: skip
    for p, c in rows:
        review = c.result.get("review") or {}
        sha = (p.commit_sha or "diff")[:12]
        lines.append(f"* {sha} {p.subject}: {_outcome(config, p, c)}")
        prs = (c.result.get("publish") or {}).get("projects", {})
        if p.state == State.VERIFIED and p.project in prs:
            lines.append(f"** PR: {prs[p.project]['url']}")
        lines.append(f"** approved by {review.get('by', '?')} on {review.get('at', '?')[:10]}")
    return "\n".join(lines) + "\n"


def plan(config: ProjectConfig, store: LedgerStore, run: Run) -> list[TicketExport]:
    with store.session() as s:
        clusters = s.scalars(
            select(Cluster).where(Cluster.run_id == run.id).order_by(Cluster.order_index)
        ).all()
        patches = s.scalars(select(Patch).where(Patch.run_id == run.id).order_by(Patch.seq)).all()
    approved = {
        c.id: c
        for c in clusters
        if (c.result.get("review") or {}).get("decision") == "approved" and c.state in APPROVABLE
    }
    by_key: dict[str, list[tuple[Patch, Cluster]]] = {}
    untracked: dict[int, list[Patch]] = {}
    for p in patches:
        c = approved.get(p.cluster_id)
        if c is None or p.state not in OUTCOME_LABEL:
            continue
        if p.jira_keys:
            for key in p.jira_keys:
                by_key.setdefault(key, []).append((p, c))
        else:
            untracked.setdefault(c.id, []).append(p)

    label = config.migration.label
    out = []

    def links(rows) -> list[tuple[str, str, str]]:
        found = {}
        for p, c in rows:
            prs = (c.result.get("publish") or {}).get("projects", {})
            if p.state == State.VERIFIED and p.project in prs:
                e = prs[p.project]
                gid = f"patchmig:{label}:{e['repo']}#{e['number']}"
                found[gid] = (e["url"], f"{label}: {c.name} ({e['repo']} #{e['number']})", gid)
        return list(found.values())

    for key, rows in sorted(by_key.items()):
        out.append(
            TicketExport(
                key=key,
                cluster_ids=sorted({c.id for _, c in rows}),
                labels=sorted({f"{label}-{OUTCOME_LABEL[p.state]}" for p, _ in rows}),
                links=links(rows),
                comment=_comment(config, run, rows),
                patch_ids=[p.id for p, _ in rows],
            )
        )
    for cid, ps in untracked.items():
        c = approved[cid]
        rows = [(p, c) for p in ps]
        out.append(
            TicketExport(
                key=c.result.get("jira_tracking_key"),
                cluster_ids=[cid],
                labels=sorted({f"{label}-{OUTCOME_LABEL[p.state]}" for p in ps}),
                links=links(rows),
                comment=_comment(config, run, rows),
                patch_ids=[p.id for p in ps],
                summary=f"[{label}] {c.name}",
            )
        )
    return out


def export(
    config: ProjectConfig,
    store: LedgerStore,
    run: Run,
    jira: JiraWriter,
    *,
    create_missing: bool = False,
) -> ExportReport:
    if config.jira is None:
        raise JiraError("the project has no 'jira' section")
    report = ExportReport()
    tag = marker(config)
    for t in plan(config, store, run):
        try:
            key = t.key
            if key is None:
                if not create_missing:
                    report.skipped.append(f"untracked patches of cluster {t.cluster_ids[0]}")
                    continue
                key = jira.create_issue(
                    config.jira.project, config.jira.issue_type, t.summary, t.comment, t.labels
                )
                report.created.append(key)
                _remember_tracking(store, t, key)
            jira.add_labels(key, t.labels)
            for url, title, gid in t.links:
                jira.add_link(key, url, title, gid)
            existing = [c for c in jira.comments(key) if tag in c["body"]]
            if existing:
                if existing[0]["body"] != t.comment:
                    jira.update_comment(key, existing[0]["id"], t.comment)
            else:
                jira.add_comment(key, t.comment)
            report.updated.append(key)
            _record(store, t, key)
        except JiraError as e:
            report.errors.append(f"{t.key or t.summary}: {e}")
    return report


def _remember_tracking(store: LedgerStore, t: TicketExport, key: str) -> None:
    with store.session() as s, s.begin():
        c = s.get(Cluster, t.cluster_ids[0])
        c.result = {**c.result, "jira_tracking_key": key}
        for pid in t.patch_ids:
            p = s.get(Patch, pid)
            p.jira_keys = [*p.jira_keys, key]


def _record(store: LedgerStore, t: TicketExport, key: str) -> None:
    with store.session() as s, s.begin():
        for cid in t.cluster_ids:
            c = s.get(Cluster, cid)
            exported = {**(c.result.get("jira_export") or {}), key: _now()}
            c.result = {**c.result, "jira_export": exported}
