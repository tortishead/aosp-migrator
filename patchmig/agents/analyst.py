"""The analyst: Copilot's part of the analysis step, one call per backlog item.

For each item (a Jira ticket with its patches, a ticket without a patch, or a patch without a
ticket) the analyst gets everything the import kept and engineers added: ticket text, all
comments, linked tickets, remote links, text attachments, added links and files, engineer
notes, the patches with their diffs and the deterministic verdicts, other backlog items, and
what earlier migrations remembered. It reads the target tree through the MCP tools, then
answers with: an understanding of the item and of each patch (the patches' intents), whether
it is still needed in the target, related items, a proposed solution, and what is missing.

Copilot is asked only where it is needed: an item whose patches the deterministic checks all
dropped is not sent, unless asked for by key. Each answer records a fingerprint of what the
analyst was given; when that changes (new comments, an added file, a note, a new patch) the
item is analysed again by the next analysis step, and the earlier answer is kept.

The answer moves patches the deterministic checks left `needed` (and patches the analyst
moved before): to upstream or obsolete when it is confident they are not needed, to
needs_decision when it is unsure or information is missing, back to needed when it changes
its mind. Clusters already migrated are not touched; their intents still change, which makes
them stale for `migrate apply --stale`.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from importlib.resources import files
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field
from sqlalchemy import select

from patchmig import backlog as bl
from patchmig import notes as engineer_notes
from patchmig.agents.adapter import AgentTask, ToolPolicy, answer_instructions, truncate
from patchmig.agents.runner import AgentFailed, AgentRunner, BudgetExceeded
from patchmig.analysis import _reason, is_locked, move, refresh_clusters
from patchmig.analysis.patches import load_diff
from patchmig.confidence import describe, rubric
from patchmig.config import ProjectConfig
from patchmig.ledger import BacklogItem, Cluster, LedgerStore, Note, Patch, Run, State, Ticket

AGENT = "analyst"
ACTOR = f"agent:{AGENT}"
SERVER = "patchmig"
MAX_COMMENTS = 30
MAX_ATTACHMENT_CHARS = 8_000
MAX_ATTACHMENTS_CHARS = 30_000
MAX_DIFF_PATCHES = 6  # patches shown with their diff; the rest by subject
MAX_CANDIDATES = 40
ACTIVE = (State.NEEDED, State.NEEDS_DECISION)


class PatchUnderstanding(BaseModel):
    patch_id: int = Field(description="The patch's id, as #<id> in the brief")
    problem: str = Field(description="What was wrong or missing, in 1-2 sentences")
    change: str = Field(description="What the patch changes in behaviour, in 1-2 sentences")
    verification: str = Field(description="How to tell in a tree that the change is there")
    components: list[str] = Field(default_factory=list, description="e.g. 'CarPowerService'")


class TargetEvidence(BaseModel):
    project: str = Field(description="Target tree project, e.g. 'frameworks/base'")
    path: str = Field(description="Project-relative file path in the target tree")
    snippet: str = Field(description="Exact lines copied from that target file")


class Related(BaseModel):
    ref: str = Field(description="A backlog item key as listed (OEM-12, patch:7) or #<patch id>")
    relation: Literal["same_change", "depends_on", "needed_by", "fixes", "fixed_by", "reverts",
                      "duplicate", "related"]  # fmt: skip
    why: str


class Solution(BaseModel):
    approach: Literal["cherry_pick", "adapt", "rewrite", "drop", "needs_patch", "unclear"]
    summary: str = Field(description="What to do in the target, in 1-3 sentences")
    steps: list[str] = Field(default_factory=list)
    files: list[str] = Field(default_factory=list, description="Target files, <project>/<path>")
    risks: list[str] = Field(default_factory=list, description="What a reviewer should check")


class ItemAnalysis(BaseModel):
    understanding: str = Field(description="What the item is about and why, in 2-4 sentences")
    patches: list[PatchUnderstanding] = Field(default_factory=list)
    relevance: Literal["needed", "already_in_target", "obsolete", "not_needed", "unclear"]
    relevance_reason: str
    target_evidence: list[TargetEvidence] = Field(default_factory=list)
    related: list[Related] = Field(default_factory=list)
    solution: Solution
    missing_information: list[str] = Field(default_factory=list)
    sources: list[Literal["ticket", "comments", "attachments", "linked_tickets", "links",
                          "notes", "added", "commit_message", "diff", "target_tree",
                          "memory"]] = Field(min_length=1)  # fmt: skip
    confidence: float = Field(ge=0, le=1)


# Where a confident "not needed" sends a patch.
DROP_TO = {"already_in_target": State.UPSTREAM, "obsolete": State.OBSOLETE,
           "not_needed": State.OBSOLETE}  # fmt: skip


def notes_section(text: str) -> str:
    """Engineers' notes as a brief section; they outrank guesses, not the code."""
    if not text:
        return ""
    return (
        "\n## Notes from the migration engineers\n\n"
        f"{text}\n\nThese come from people who know this code and product. Follow them; if "
        "the code clearly contradicts a note, say so in your answer.\n"
    )


def render_intent(item: ItemAnalysis, p: PatchUnderstanding | None) -> str:
    """A patch's intent for people and for the cluster agent."""
    if p is None:  # the analyst said nothing about this patch on its own
        lines = [f"Problem: {item.understanding}"]
    else:
        lines = [f"Problem: {p.problem}", f"Change: {p.change}", f"Verify: {p.verification}",
                 f"Components: {', '.join(p.components) or '-'}"]  # fmt: skip
    lines += [
        f"Still needed: {item.relevance.replace('_', ' ')}: {item.relevance_reason}",
        f"Solution: {item.solution.approach.replace('_', ' ')}: {item.solution.summary}",
        f"Sources: {', '.join(item.sources)} (confidence {describe(item.confidence)})",
    ]
    lines += [f"Open question: {q}" for q in item.missing_information]
    return "\n".join(lines)


# ---- what the analyst is given --------------------------------------------------------


@dataclass
class ItemContext:
    item: BacklogItem
    members: list[Patch]  # the item's patches
    owned: list[Patch]  # those whose verdict and intent this item decides
    ticket: Ticket | None
    linked: dict[str, Ticket]
    notes: list[Note]


def _item_notes(notes: list[Note], item: BacklogItem, members: list[Patch]) -> list[Note]:
    out = []
    for n in notes:
        if "analyse" not in n.stages:
            continue
        if (
            (n.scope == "ticket" and n.target == item.key)
            or n.scope == "all"
            or any(engineer_notes.applies_to_patch(n, m) for m in members)
        ):
            out.append(n)
    return out


def inputs_fingerprint(c: ItemContext) -> str:
    """What the analyst is given, in 16 hex digits: changes when new information arrives.

    Patches count by patch-id, so an item imported into a later run with the same patches
    and information matches its earlier analysis.
    """
    t = c.ticket
    parts = {
        "ticket": [t.summary, t.description, t.status, t.comments or [], t.remote_links or [],
                   [(a.get("filename"), a.get("size")) for a in t.attachments or []]]
        if t else None,
        "linked": sorted((k, x.summary, x.status) for k, x in c.linked.items()),
        "added": [(a["id"], a.get("url") or a.get("filename"), a.get("size"))
                  for a in bl.active_additions(c.item)],
        "notes": [(n.id, n.text) for n in c.notes],
        "patches": sorted(p.fingerprint or p.commit_sha or str(p.id) for p in c.members),
    }  # fmt: skip
    return hashlib.sha256(json.dumps(parts, default=str).encode()).hexdigest()[:16]


def _ticket_text(c: ItemContext) -> str:
    t = c.ticket
    if c.item.kind == "patch":
        return "(no Jira ticket: this patch has none; judge it from the commit and diff)"
    if t is None:
        return "(the ticket could not be read from Jira; judge it from its patches)"
    parts = [f"Jira {t.key}, status {t.status or '?'}\n\n{truncate(t.description, 6000)}"]
    comments = (t.comments or [])[-MAX_COMMENTS:]
    if comments:
        cut = len(t.comments) - len(comments)
        head = f"### Comments (oldest first{f', {cut} earlier ones left out' if cut else ''})"
        parts.append(head + "\n\n" + "\n".join(f"- {truncate(x, 1500)}" for x in comments))
    for key in t.issue_links or []:
        if other := c.linked.get(key):
            parts.append(f"### Linked ticket {key}: {other.summary} ({other.status})\n\n"
                         f"{truncate(other.description, 1500)}")  # fmt: skip
        else:
            parts.append(f"### Linked ticket {key} (not readable)")
    if t.remote_links:
        parts.append("### Links on the ticket\n\n" + "\n".join(
            f"- {r.get('title') or r['url']}: {r['url']}" for r in t.remote_links))  # fmt: skip
    if t.attachments:
        lines, budget = [], MAX_ATTACHMENTS_CHARS
        for a in t.attachments:
            text = bl.read_text(a.get("path"), a["filename"], min(MAX_ATTACHMENT_CHARS, budget))
            if text and budget > 0:
                budget -= len(text)
                lines.append(f"#### {a['filename']}\n\n```\n{text}\n```")
            else:
                why = a.get("note") or ("kept, not text" if a.get("path") else "listed")
                lines.append(f"- {a['filename']} ({a.get('size', '?')} bytes; {why})")
        parts.append("### Attachments\n\n" + "\n".join(lines))
    return "\n\n".join(parts)


def _added_text(c: ItemContext) -> str:
    added = bl.active_additions(c.item)
    if not added:
        return ""
    lines = []
    for a in added:
        if a["kind"] == "link":
            lines.append(f"- link from {a['by']}: {a['url']}")
            continue
        text = bl.read_text(a.get("path"), a["filename"], MAX_ATTACHMENT_CHARS)
        lines.append(f"- file from {a['by']}: {a['filename']}"
                     + (f"\n\n```\n{text}\n```" if text else " (not text)"))  # fmt: skip
    return "\n## Added by the migration engineers\n\n" + "\n".join(lines) + "\n"


def _check_text(p: Patch) -> str:
    v = p.verdict or {}
    if not v.get("verdict"):
        return "not checked yet"
    text = f"{v['verdict']} ({_reason(v)})"
    if v.get("question"):
        text += f"; question: {v['question']}"
    if v.get("analyst_disagrees"):
        text += f"; you disagreed before: {v['analyst_disagrees']}"
    return text


def _patches_text(config: ProjectConfig, c: ItemContext) -> str:
    if not c.members:
        return ("(no patch found for this ticket: say whether the behaviour is already in the "
                "target, and what a patch would have to do)")  # fmt: skip
    out = []
    for n, p in enumerate(c.members):
        head = (f"### #{p.id} {p.project} · {(p.commit_sha or 'diff file')[:12]} · role "
                f"{p.chain_role or 'base'} · state {p.state.value}\n\nSubject: {p.subject}\n"
                f"Checks: {_check_text(p)}")  # fmt: skip
        if n >= MAX_DIFF_PATCHES:
            out.append(head + "\n(diff left out: read it with get_patch)")
            continue
        diff = truncate(load_diff(config, p) or "(diff not available)",
                        config.llm.intent_max_diff_chars)  # fmt: skip
        out.append(f"{head}\n\n```\n{p.message.strip()}\n```\n\n```diff\n{diff}\n```")
    return "\n\n".join(out)


def _paths(p: Patch) -> set[str]:
    return {f"{p.project}/{f['path']}" for f in p.footprint.get("files", [])}


def _candidates(
    c: ItemContext, others: list[BacklogItem], members_of: dict[str, list[Patch]]
) -> str:
    mine = {x for p in c.members for x in _paths(p)}
    my_clusters = {p.cluster_id for p in c.members if p.cluster_id}
    linked = set(c.ticket.issue_links or []) if c.ticket else set()
    scored = []
    for o in others:
        if o.key == c.item.key:
            continue
        theirs = members_of.get(o.key, [])
        shared = sorted(mine & {x for p in theirs for x in _paths(p)})
        clusters = my_clusters & {p.cluster_id for p in theirs if p.cluster_id}
        why = (["linked in Jira"] if o.key in linked else []) + (
            [f"same cluster {', '.join(map(str, sorted(clusters)))}"] if clusters else []) + (
            [f"shares {', '.join(shared[:3])}"] if shared else [])  # fmt: skip
        score = 3 * (o.key in linked) + 2 * bool(clusters) + 2 * bool(shared)
        patches = "; ".join(f"#{p.id} {p.subject}" for p in theirs[:3]) or "no patch"
        scored.append((-score, o.id, f"- {o.key}: {o.title} ({patches})"
                       + (f" [{'; '.join(why)}]" if why else "")))  # fmt: skip
    scored.sort()
    lines = [line for *_, line in scored[:MAX_CANDIDATES]]
    if len(scored) > MAX_CANDIDATES:
        lines.append(f"- ... {len(scored) - MAX_CANDIDATES} more, with no link to this item")
    return "\n".join(lines) or "(none)"


def _earlier_text(item: BacklogItem) -> str:
    a = item.analysis
    if not a:
        return ""
    return (
        "\n## Your earlier analysis of this item\n\nNew information has arrived since (see "
        "above); revise the analysis where it changes anything.\n\n"
        f"- Understanding: {a.get('understanding')}\n"
        f"- Still needed: {a.get('relevance')}: {a.get('relevance_reason')}\n"
        f"- Solution: {(a.get('solution') or {}).get('summary')}\n"
        + "".join(f"- Missing then: {q}\n" for q in a.get("missing_information", []))
    )


def build_brief(
    config: ProjectConfig,
    c: ItemContext,
    others: list[BacklogItem],
    members_of: dict[str, list[Patch]],
    memory_text: str = "",
) -> str:
    template = files("patchmig.agents.briefs").joinpath("analyst.md").read_text()
    return (
        template.format(
            source_version=config.migration.source_version,
            target_version=config.migration.target_version,
            server=SERVER,
            key=c.item.key,
            title=c.item.title,
            ticket=_ticket_text(c),
            added=_added_text(c),
            notes=notes_section(engineer_notes.render(c.notes)),
            memory=(
                f"\n## From earlier migrations (migration memory)\n\n{memory_text}\n"
                if memory_text
                else ""
            ),  # fmt: skip
            earlier=_earlier_text(c.item),
            patches=_patches_text(config, c),
            candidates=_candidates(c, others, members_of),
        )
        + "\n"
        + rubric("your analysis")
        + answer_instructions(ItemAnalysis)
    )


# ---- the step -------------------------------------------------------------------------


@dataclass
class AnalystReport:
    analysed: int = 0
    reused: int = 0  # same item with the same information, analysed in an earlier run
    current: int = 0  # analysed before with the same information
    not_required: int = 0  # every patch dropped by the deterministic checks
    moved: list[str] = field(default_factory=list)  # "#12 needed -> upstream"
    failed: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    stopped_by_budget: str | None = None
    credits: float = 0.0


def contexts(store: LedgerStore, run: Run) -> list[ItemContext]:
    """Every backlog item with what the analyst would be given."""
    items = bl.items(store, run)
    keys = {i.key for i in items if i.kind == "ticket"}
    notes = engineer_notes.active(store)
    with store.session() as s:
        patches = s.scalars(select(Patch).where(Patch.run_id == run.id).order_by(Patch.seq)).all()
        tickets = {t.key: t for t in s.scalars(select(Ticket))}
    out = []
    for item in items:
        if item.kind == "patch":
            members = [p for p in patches if f"patch:{p.id}" == item.key]
        else:
            members = [p for p in patches if item.key in (p.jira_keys or [])]
        owned = [p for p in members if bl.item_of(p, keys) == item.key]
        ticket = tickets.get(item.key) if item.kind == "ticket" else None
        linked = (
            {k: tickets[k] for k in (ticket.issue_links or []) if k in tickets} if ticket else {}
        )
        out.append(ItemContext(item, members, owned, ticket, linked,
                               _item_notes(notes, item, members)))  # fmt: skip
    return out


def is_stale(c: ItemContext) -> bool:
    return c.item.analysis is not None and c.item.analysis.get("inputs") != inputs_fingerprint(c)


def required(c: ItemContext) -> bool:
    """Whether Copilot is needed: no patch at all, a patch still open, or one it decided."""
    return not c.members or any(
        p.state in (*ACTIVE, State.INGESTED) or (p.verdict or {}).get("rung") == "analyst"
        for p in c.members
    )


def _reusable(store: LedgerStore, run: Run, c: ItemContext, fp: str) -> BacklogItem | None:
    with store.session() as s:
        earlier = s.scalars(
            select(BacklogItem)
            .where(BacklogItem.key == c.item.key, BacklogItem.run_id != run.id,
                   BacklogItem.analysis.is_not(None))
            .order_by(BacklogItem.id.desc())
        ).all()  # fmt: skip
    return next((e for e in earlier if e.analysis.get("inputs") == fp), None)


def analyse_items(
    config: ProjectConfig,
    store: LedgerStore,
    run: Run,
    runner: AgentRunner,
    keys: list[str] | None = None,
    limit: int | None = None,
    memory=None,
    mcp: dict | None = None,
) -> AnalystReport:
    """Ask the analyst about the items that need it: never analysed, or given new
    information since. `keys` analyses these items again, whatever their state."""
    report = AnalystReport()
    every = contexts(store, run)
    known = {c.item.key for c in every}
    if keys and (unknown := [k for k in keys if k not in known]):
        raise bl.BacklogError(f"not in the backlog of run {run.id}: {', '.join(unknown)}")
    others = [c.item for c in every]
    members_of = {c.item.key: c.members for c in every}
    asked = 0
    for c in every:
        fp = inputs_fingerprint(c)
        if keys is not None:
            if c.item.key not in keys:
                continue
        elif c.item.analysis is not None and c.item.analysis.get("inputs") == fp:
            report.current += 1
            continue
        elif not required(c):
            report.not_required += 1
            continue
        if keys is None and (earlier := _reusable(store, run, c, fp)):
            answer = ItemAnalysis.model_validate(
                {k: v for k, v in earlier.analysis.items() if k in ItemAnalysis.model_fields}
            )
            prints = earlier.analysis.get("patch_fingerprints", {})
            by_print = {p.fingerprint: p.id for p in c.members if p.fingerprint}
            answer.patches = [x.model_copy(update={"patch_id": by_print[prints[str(x.patch_id)]]})
                              for x in answer.patches
                              if prints.get(str(x.patch_id)) in by_print]  # fmt: skip
            _save(config, store, run, c, answer, fp, report,
                  {"reused_from_run": earlier.run_id})  # fmt: skip
            report.reused += 1
            continue
        if limit is not None and asked >= limit:
            continue
        files_ = sorted({(p.project, f["path"]) for p in c.members
                         for f in p.footprint.get("files", [])})  # fmt: skip
        memory_text = (memory.brief_for(c.members, files_, config.memory.brief_max_chars)
                       if memory is not None and c.members else "")  # fmt: skip
        task = AgentTask(
            agent=AGENT,
            key=f"item:{c.item.key}",
            brief=build_brief(config, c, others, members_of, memory_text),
            schema=ItemAnalysis,
            workdir=config.workdir,
            tools=ToolPolicy(
                allow=[SERVER] if mcp else [],
                deny=["shell", "write"],
                mcp_servers={SERVER: mcp} if mcp else {},
            ),  # fmt: skip
            subject_type="item",
            subject_id=c.item.id,
        )
        asked += 1
        try:
            outcome = runner.run(task)
        except BudgetExceeded as e:
            report.stopped_by_budget = str(e)
            break
        except AgentFailed as e:
            report.failed.append(f"{c.item.key}: {e}")
            continue
        report.credits += outcome.credits
        _save(config, store, run, c, outcome.data, fp, report, {})
        report.analysed += 1
    return report


def _related(answer: ItemAnalysis, known: set[str], patch_ids: set[int]) -> list[dict]:
    out = []
    for r in answer.related:
        ref = r.ref.strip()
        ok = ref in known or (
            ref.startswith("#") and ref[1:].isdigit() and int(ref[1:]) in patch_ids
        )
        out.append({**r.model_dump(), "ref": ref, "known": ok})
    return out


def _save(
    config: ProjectConfig,
    store: LedgerStore,
    run: Run,
    c: ItemContext,
    answer: ItemAnalysis,
    fp: str,
    report: AnalystReport,
    extra: dict,
) -> None:
    now = datetime.now(UTC)
    with store.session() as s:
        known = set(s.scalars(select(BacklogItem.key).where(BacklogItem.run_id == run.id)))
        patch_ids = set(s.scalars(select(Patch.id).where(Patch.run_id == run.id)))
    moves = _apply(config, store, run, c, answer)
    report.moved += moves
    record = {
        **answer.model_dump(),
        **extra,
        "related": _related(answer, known, patch_ids),
        "inputs": fp,
        "at": now.isoformat(timespec="seconds"),
        "moved": moves,
        "patch_fingerprints": {str(p.id): p.fingerprint for p in c.members},
    }
    with store.session() as s, s.begin():
        row = s.get(BacklogItem, c.item.id)
        if row.analysis:
            row.analyses = [*(row.analyses or []), row.analysis]
        row.analysis, row.analysed_at = record, now
    notes = engineer_notes.active(store)
    by_id = {x.patch_id: x for x in answer.patches}
    with store.session() as s, s.begin():
        for p in c.owned:
            row = s.get(Patch, p.id)
            mine = engineer_notes.for_patch(notes, row, "analyse")
            understood = by_id.get(p.id)
            detail = {**(understood.model_dump() if understood else {}),
                      "item": c.item.key, "sources": answer.sources,
                      "confidence": answer.confidence}  # fmt: skip
            if fp_notes := engineer_notes.fingerprint(mine):
                detail["notes"] = fp_notes
            row.intent, row.intent_detail = render_intent(answer, understood), detail


def _apply(
    config: ProjectConfig, store: LedgerStore, run: Run, c: ItemContext, answer: ItemAnalysis
) -> list[str]:
    """Move the item's patches as the answer says; returns what moved."""
    sure = answer.confidence >= config.thresholds.careful_review
    moves: list[str] = []
    touched: set[int] = set()
    with store.session() as s:
        locked = {x.id for x in s.scalars(select(Cluster).where(Cluster.run_id == run.id))
                  if is_locked(x)}  # fmt: skip
    for p in c.owned:
        with store.session() as s:
            p = s.get(Patch, p.id)
        if p.cluster_id in locked:
            continue
        verdict = dict(p.verdict or {})
        by_analyst = verdict.get("rung") == "analyst"
        if p.state not in ACTIVE and not by_analyst:  # a deterministic rung dropped it
            if answer.relevance == "needed":
                verdict["analyst_disagrees"] = answer.relevance_reason
                _set_verdict(store, p, verdict)
            continue
        if p.state == State.NEEDS_DECISION and not by_analyst:  # a path rule's question
            continue
        before = verdict.get("before_analyst") or verdict  # what the checks said
        if answer.relevance == "needed":
            if not by_analyst:  # the checks said needed too
                continue
            target, new = State.NEEDED, before
        elif answer.relevance in DROP_TO and sure:
            target = DROP_TO[answer.relevance]
            new = {
                "verdict": target.value,
                "rung": "analyst",
                "before_analyst": before,
                "evidence": {
                    "reason": answer.relevance_reason,
                    "target": [e.model_dump() for e in answer.target_evidence],
                },
            }
        else:
            question = "; ".join(answer.missing_information) or (
                f"the analyst thinks {answer.relevance.replace('_', ' ')} "
                f"({describe(answer.confidence)}): {answer.relevance_reason}")  # fmt: skip
            target = State.NEEDS_DECISION
            new = {"verdict": "needs_decision", "rung": "analyst", "before_analyst": before,
                   "question": question}  # fmt: skip
        if target == p.state and new == verdict:
            continue
        _set_verdict(store, p, new)
        if target != p.state:
            move(store, p, target, f"analyst: {answer.relevance_reason}"[:500], ACTOR)
            moves.append(f"#{p.id} {p.state.value} -> {target.value}")
        if p.cluster_id:
            touched.add(p.cluster_id)
    if touched:
        refresh_clusters(config, store, run, touched, ACTOR)
    return moves


def _set_verdict(store: LedgerStore, p: Patch, verdict: dict) -> None:
    with store.session() as s, s.begin():
        s.get(Patch, p.id).verdict = verdict


def stale_items(store: LedgerStore, run: Run) -> list[engineer_notes.Stale]:
    """Items analysed with information that has changed since (notes, additions, ticket)."""
    return [
        engineer_notes.Stale(
            "item", c.item.id, c.item.key, "analysed", "new information since its analysis"
        )  # fmt: skip
        for c in contexts(store, run)
        if is_stale(c)
    ]


def export_review_sheet(store: LedgerStore, run: Run, path: Path) -> int:
    """A Markdown sheet for an engineer to judge each analysis; returns items written."""
    from patchmig.ledger.models import AgentCall

    with store.session() as s:
        calls = s.scalars(
            select(AgentCall).where(AgentCall.run_id == run.id, AgentCall.agent == AGENT)
        ).all()
    ok = [c for c in calls if c.ok]
    lines = [
        f"# Analysis review, run {run.id}",
        "",
        f"Agent calls: {len(calls)} ({len(ok)} valid), estimated credits "
        f"{sum(c.credits for c in calls):.1f}, average duration "
        f"{(sum(c.duration_s for c in calls) / len(calls)) if calls else 0:.1f}s.",
        "",
        "For each item, mark whether the analysis is right and useful, and note what is wrong.",
        "",
    ]
    written = 0
    for c in contexts(store, run):
        a = c.item.analysis
        if not a:
            continue
        written += 1
        lines += [
            f"## {c.item.key}: {c.item.title}",
            "",
            f"Patches: {', '.join(f'#{p.id}' for p in c.members) or 'none'} · still needed: "
            f"{a['relevance']} · solution: {a['solution']['approach']} · confidence "
            f"{describe(a['confidence'])}",
            "",
            f"> {a['understanding']}",
            ">",
            f"> {a['relevance_reason']}",
            ">",
            f"> {a['solution']['summary']}",
            "",
            *[f"- Missing: {q}" for q in a.get("missing_information", [])],
            *[f"- Related: {r['ref']} ({r['relation']}): {r['why']}" for r in a.get("related", [])],
            "- [ ] Correct and useful",
            "- Notes:",
            "",
        ]
    path.write_text("\n".join(lines))
    return written
