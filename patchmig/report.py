"""A Markdown summary of one run: what a pilot needs to judge the tool.

Patches by outcome and by the rung that decided them, clusters by strategy and review queue,
build and test results, agent calls and credits per agent, and the clusters that need a
person. Everything comes from the ledger; nothing is called.
"""

from __future__ import annotations

from collections import Counter, defaultdict

from sqlalchemy import select

from patchmig.ledger import Cluster, LedgerStore
from patchmig.ledger.models import AgentCall
from patchmig.web import data


def _table(header: list[str], rows: list[list]) -> list[str]:
    out = ["| " + " | ".join(header) + " |", "|" + " --- |" * len(header)]
    out += ["| " + " | ".join(str(c) for c in row) + " |" for row in rows]
    return out


def run_report(store: LedgerStore, run_id: int) -> str:
    s = data.summary(store, run_id)
    review = data.review(store, run_id)
    with store.session() as session:
        clusters = session.scalars(
            select(Cluster).where(Cluster.run_id == run_id).order_by(Cluster.order_index)
        ).all()
        calls = session.scalars(select(AgentCall).where(AgentCall.run_id == run_id)).all()
    m = s["migration"] or {}
    lines = [
        f"# {s['project']}: Android {m.get('source_version')} → {m.get('target_version')}, "
        f"run {run_id}",
        "",
        f"{s['patches']} patches in {s['clusters']} clusters; {s['agent_calls']} agent calls, "
        f"{s['credits']} credits (estimated).",
        "",
        "## Patches",
        "",
        *_table(["State", "Patches"], [[k, v] for k, v in sorted(s["states"].items())]),
        "",
        "Decided by: "
        + ", ".join(
            f"{'no upstream match' if k == 'none' else k} {v}"
            for k, v in sorted(s["rungs"].items())
        ),
        "",
        "## Clusters",
        "",
    ]
    strategies = Counter(
        (c.result.get("migration") or {}).get("strategy") or "not migrated" for c in clusters
    )
    lines += _table(["Strategy", "Clusters"], [[k, v] for k, v in sorted(strategies.items())])
    queue_rows = [[k.replace("_", " "), v] for k, v in review["counts"].items() if v]
    lines += ["", *_table(["Review queue", "Clusters"], queue_rows)]

    builds = [b for c in clusters for b in (c.result.get("verification") or {}).get("builds", [])]
    tests = [t for c in clusters if (t := (c.result.get("verification") or {}).get("tests"))]
    fixes = [f for c in clusters for f in (c.result.get("verification") or {}).get("fixes", [])]
    if builds:
        minutes = sum(b["duration_s"] for b in builds) / 60
        lines += [
            "",
            "## Build and test",
            "",
            f"- {len(builds)} module builds, {sum(b['ok'] for b in builds)} ok, "
            f"{minutes:.1f} min in total",
            f"- {len(fixes)} build fix attempt(s) by the agent, "
            f"{sum(1 for f in fixes if (f.get('answer') or {}).get('outcome') == 'fixed')} fixed",
            f"- tests run for {len(tests)} cluster(s), {sum(t['ok'] for t in tests)} passed",
        ]

    per_agent: dict[str, list] = defaultdict(list)
    for c in calls:
        per_agent[c.agent].append(c)
    if per_agent:
        lines += ["", "## Agent calls", ""]
        lines += _table(
            ["Agent", "Calls", "Failed", "Credits", "Minutes"],
            [[a, len(cs), sum(not c.ok for c in cs), round(sum(c.credits for c in cs), 1),
              round(sum(c.duration_s for c in cs) / 60, 1)] for a, cs in sorted(per_agent.items())],
        )  # fmt: skip

    attention = review["queues"]["needs_decision"] + review["queues"]["failed"]
    if attention:
        lines += ["", "## Needs a person", ""]
        for item in attention:
            note = item["question"] or item["reason"] or ""
            lines.append(f"- [{item['id']}] {item['name']} ({item['state']}): {note}")
    return "\n".join(lines) + "\n"
