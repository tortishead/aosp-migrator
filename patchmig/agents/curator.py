"""Memory curator: turns a run's human feedback and fixes into proposed lessons.

One call per run, and only when there is something to learn from (feedback, decisions,
build fixes, reviewer concerns). Proposed lessons are not used until an engineer approves
them (`migrate memory approve`).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from importlib.resources import files

from pydantic import BaseModel, Field
from sqlalchemy import select

from patchmig.agents.adapter import AgentTask, answer_instructions
from patchmig.agents.runner import AgentRunner
from patchmig.config import ProjectConfig
from patchmig.ledger import Cluster, LedgerStore, Run
from patchmig.memory import Memory

MAX_SIGNAL_CHARS = 20_000


class ProposedLesson(BaseModel):
    title: str = Field(description="Short name, under 80 characters")
    text: str = Field(description="The lesson in 1-4 sentences")
    applies_to: list[str] = Field(default_factory=list, description="Globs over tree paths")
    clusters: list[str] = Field(default_factory=list, description="Clusters it comes from")


class CuratorAnswer(BaseModel):
    lessons: list[ProposedLesson] = Field(default_factory=list)


@dataclass
class CurateReport:
    signals: int = 0
    proposed: list[str] = field(default_factory=list)
    credits: float = 0.0


def signals(store: LedgerStore, run: Run) -> list[str]:
    with store.session() as s:
        clusters = s.scalars(
            select(Cluster).where(Cluster.run_id == run.id).order_by(Cluster.order_index)
        ).all()
    out = []
    for c in clusters:
        r = c.result
        projects = sorted((r.get("migration") or {}).get("projects") or {})
        head = f"### {c.name} ({c.state}; projects: {', '.join(projects) or '-'})"
        lines = []
        lines += [f"- reviewer {f['by']}: {f['text']}" for f in r.get("feedback", [])]
        lines += [f"- decision on '{d.get('question')}': {d['choice']}, {d['reason']}"
                  for d in r.get("decisions", [])]  # fmt: skip
        for fix in (r.get("verification") or {}).get("fixes", []):
            if ans := fix.get("answer"):
                lines.append(f"- build fix ({ans['outcome']}): {ans['summary']}")
        review = (r.get("migration") or {}).get("review") or {}
        if review.get("verdict") in ("concerns", "reject"):
            lines += [f"- review agent {review['verdict']}: {i}" for i in review.get("issues", [])]
        if lines:
            out.append("\n".join([head, *lines]))
    return out


def curate(
    config: ProjectConfig, store: LedgerStore, run: Run, runner: AgentRunner, memory: Memory
) -> CurateReport:
    found = signals(store, run)
    report = CurateReport(signals=len(found))
    if not found:
        return report  # nothing to learn from: no call, no credits
    existing = [f"- {x.title}" for x in memory.lessons("approved") + memory.lessons("proposed")]
    text = "\n\n".join(found)
    template = files("patchmig.agents.briefs").joinpath("memory_curator.md").read_text()
    brief = template.format(
        source_version=config.migration.source_version,
        target_version=config.migration.target_version,
        max_lessons=config.memory.max_lessons,
        existing="\n".join(existing) or "(none yet)",
        signals=text[:MAX_SIGNAL_CHARS],
    ) + answer_instructions(CuratorAnswer)
    task = AgentTask(
        agent="memory_curator", key=f"run:{run.id}:curate", brief=brief,
        schema=CuratorAnswer, workdir=config.workdir,
    )  # fmt: skip
    outcome = runner.run(task)
    report.credits = outcome.credits
    for lesson in outcome.data.lessons[: config.memory.max_lessons]:
        saved = memory.propose(
            lesson.title, lesson.text, lesson.applies_to,
            {"migration": config.migration.label, "run": run.id, "clusters": lesson.clusters},
        )  # fmt: skip
        report.proposed.append(saved.name)
    memory.commit(f"Propose {len(report.proposed)} lesson(s) from {config.migration.label} "
                  f"run {run.id}")  # fmt: skip
    return report
