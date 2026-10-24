"""Intent writer: one short, sourced statement of why each patch exists.

Intents are written once per patch fingerprint and reused across runs (and later migrations),
so re-running ingest never pays for the same intent twice.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from importlib.resources import files
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field
from sqlalchemy import select

from patchmig.agents.adapter import AgentTask, answer_instructions
from patchmig.agents.runner import AgentFailed, AgentRunner, BudgetExceeded
from patchmig.config import ProjectConfig
from patchmig.gitutil import GitError, commit_info
from patchmig.ledger import LedgerStore, Patch, Run, Ticket
from patchmig.ledger.states import DROPPED


class IntentResult(BaseModel):
    problem: str = Field(description="What was wrong or missing, in 1-2 sentences")
    change: str = Field(description="What the patch changes in behaviour, in 1-2 sentences")
    verification: str = Field(description="How to tell the intent is met in a tree")
    components: list[str] = Field(description="Affected components, e.g. 'CarPowerService'")
    sources: list[Literal["commit_message", "diff", "ticket"]] = Field(min_length=1)
    confidence: float = Field(ge=0, le=1, description="How sure the intent is right")
    open_questions: list[str] = Field(default_factory=list)


def render_intent(r: IntentResult) -> str:
    lines = [
        f"Problem: {r.problem}",
        f"Change: {r.change}",
        f"Verify: {r.verification}",
        f"Components: {', '.join(r.components) or '-'}",
        f"Sources: {', '.join(r.sources)} (confidence {r.confidence:.2f})",
    ]
    lines += [f"Open question: {q}" for q in r.open_questions]
    return "\n".join(lines)


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n[... truncated, {len(text) - limit} more characters ...]"


def _diff_for(config: ProjectConfig, patch: Patch) -> str:
    if patch.commit_sha:
        repo = config.trees.source.path / patch.project
        try:
            return commit_info(repo, patch.commit_sha).diff
        except GitError:
            pass
    if patch.diff_path and Path(patch.diff_path).exists():
        return Path(patch.diff_path).read_text(errors="replace")
    return "(diff not available)"


MAX_COMMENTS = 5


def _ticket_section(t: Ticket, linked: dict[str, Ticket]) -> str:
    """A ticket with its latest comments and, briefly, the tickets it links to."""
    parts = [f"### {t.key}: {t.summary}\nStatus: {t.status}\n\n{_truncate(t.description, 4000)}"]
    comments = (t.comments or [])[-MAX_COMMENTS:]
    if comments:
        parts.append("Latest comments:\n" + "\n".join(f"- {_truncate(c, 600)}" for c in comments))
    for key in t.issue_links or []:
        if other := linked.get(key):
            parts.append(f"Linked {key}: {other.summary} ({other.status})\n"
                         f"{_truncate(other.description, 1500)}")  # fmt: skip
    return "\n\n".join(parts)


def build_brief(
    config: ProjectConfig, patch: Patch, tickets: list[Ticket], linked: dict | None = None
) -> str:
    linked = linked or {}
    template = files("patchmig.agents.briefs").joinpath("intent_writer.md").read_text()
    if tickets:
        ticket_text = "\n\n".join(_ticket_section(t, linked) for t in tickets)
    else:
        ticket_text = "(no ticket available: derive the intent from the commit and diff only)"
    return template.format(
        source_version=config.migration.source_version,
        project=patch.project,
        subject=patch.subject,
        message=patch.message,
        tickets=ticket_text,
        diff=_truncate(_diff_for(config, patch), config.llm.intent_max_diff_chars),
    ) + answer_instructions(IntentResult)


@dataclass
class IntentReport:
    written: int = 0
    reused: int = 0
    skipped: int = 0  # already had an intent
    not_needed: int = 0  # upstream, obsolete or reverted: no agent call
    failed: list[str] = field(default_factory=list)
    stopped_by_budget: str | None = None
    credits: float = 0.0


def _reusable(store: LedgerStore, patch: Patch) -> Patch | None:
    if not patch.fingerprint:
        return None
    with store.session() as s:
        return s.scalars(
            select(Patch)
            .where(
                Patch.fingerprint == patch.fingerprint,
                Patch.id != patch.id,
                Patch.intent_detail.is_not(None),
            )
            .order_by(Patch.id.desc())
            .limit(1)
        ).first()


def write_intents(
    config: ProjectConfig,
    store: LedgerStore,
    run: Run,
    runner: AgentRunner | None,
    limit: int | None = None,
    memory=None,
) -> IntentReport:
    """Give every patch of the run an intent: reuse an earlier one, else ask the agent.

    Earlier intents come from this ledger (same patch-id) or from the migration memory
    (same Change-Id in an earlier migration). With runner=None only reuse happens.
    """
    report = IntentReport()
    with store.session() as s:
        patches = s.scalars(select(Patch).where(Patch.run_id == run.id).order_by(Patch.seq)).all()
    asked = 0
    for patch in patches:
        if patch.intent_detail is not None:
            report.skipped += 1
            continue
        if earlier := _reusable(store, patch):
            detail = {**earlier.intent_detail, "reused_from_patch": earlier.id}
            _save(store, patch.id, earlier.intent, detail)
            report.reused += 1
            continue
        if memory is not None and (remembered := _from_memory(memory, patch)):
            _save(store, patch.id, *remembered)
            report.reused += 1
            continue
        if patch.state in DROPPED:  # analysed as not needed: not worth credits
            report.not_needed += 1
            continue
        if runner is None or (limit is not None and asked >= limit):
            continue
        with store.session() as s:
            tickets = [t for k in patch.jira_keys if (t := s.get(Ticket, k))]
            linked = {k: t for x in tickets for k in x.issue_links or [] if (t := s.get(Ticket, k))}
        task = AgentTask(
            agent="intent_writer",
            key=f"patch:{patch.id}",
            brief=build_brief(config, patch, tickets, linked),
            schema=IntentResult,
            workdir=config.workdir,
            subject_type="patch",
            subject_id=patch.id,
        )
        asked += 1
        try:
            outcome = runner.run(task)
        except BudgetExceeded as e:
            report.stopped_by_budget = str(e)
            break
        except AgentFailed as e:
            report.failed.append(f"{patch.subject}: {e}")
            continue
        report.credits += outcome.credits
        result: IntentResult = outcome.data
        _save(store, patch.id, render_intent(result), result.model_dump())
        report.written += 1
    return report


def _from_memory(memory, patch: Patch) -> tuple[str, dict] | None:
    for h in reversed((memory.history(patch) or {}).get("history", [])):
        if h.get("intent"):
            return h["intent"], {"source": "memory", "migration": h["migration"]}
    return None


def _save(store: LedgerStore, patch_id: int, text: str, detail: dict) -> None:
    with store.session() as s, s.begin():
        row = s.get(Patch, patch_id)
        row.intent = text
        row.intent_detail = detail


def export_review_sheet(store: LedgerStore, run: Run, path: Path) -> int:
    """Write a Markdown sheet for an engineer to judge each intent; returns patches written."""
    from patchmig.ledger.models import AgentCall

    with store.session() as s:
        patches = s.scalars(select(Patch).where(Patch.run_id == run.id).order_by(Patch.seq)).all()
        calls = s.scalars(
            select(AgentCall).where(AgentCall.run_id == run.id, AgentCall.agent == "intent_writer")
        ).all()
    ok = [c for c in calls if c.ok]
    lines = [
        f"# Intent review, run {run.id}",
        "",
        f"Agent calls: {len(calls)} ({len(ok)} valid), estimated credits "
        f"{sum(c.credits for c in calls):.1f}, average duration "
        f"{(sum(c.duration_s for c in calls) / len(calls)) if calls else 0:.1f}s.",
        "",
        "For each patch, mark whether the intent is right and useful, and note what is wrong.",
        "",
    ]
    written = 0
    for p in patches:
        if not p.intent:
            continue
        written += 1
        lines += [
            f"## {p.seq + 1}. {p.subject}",
            "",
            f"Project: {p.project} · Commit: {(p.commit_sha or '-')[:12]} · "
            f"Jira: {', '.join(p.jira_keys) or '-'} · Role: {p.chain_role}",
            "",
            *[f"> {line}" for line in p.intent.splitlines()],
            "",
            "- [ ] Correct and useful",
            "- Notes:",
            "",
        ]
    path.write_text("\n".join(lines))
    return written
