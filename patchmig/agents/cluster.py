"""The cluster agent and the independent reviewer: the two agents of the migrate step.

The cluster agent gets one call per cluster (plus a retry when guardrails fail), only when
the deterministic rungs could not migrate it. It edits the cluster's worktrees directly
(write tool allowed, shell denied) and reads the rest of the trees through the MCP tools.
The reviewer is read-only and only looks at agent-made changes.
"""

from __future__ import annotations

from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from patchmig.agents.adapter import AgentTask, ToolPolicy, answer_instructions
from patchmig.agents.runner import AgentOutcome, AgentRunner
from patchmig.config import ProjectConfig

SERVER = "patchmig"


class Evidence(BaseModel):
    project: str = Field(description="Target tree project, e.g. 'frameworks/base'")
    path: str = Field(description="Project-relative file path in the target tree")
    snippet: str = Field(description="Exact lines copied from that target file")


class ClusterAnswer(BaseModel):
    outcome: Literal["resolved", "rewritten", "upstream", "needs_decision", "failed"]
    summary: str = Field(description="What you did and why, in 1-4 sentences")
    files: list[str] = Field(
        default_factory=list, description="Files you changed, as <project>/<path>"
    )
    evidence: list[Evidence] = Field(
        default_factory=list, description="For 'upstream': target code meeting the intent"
    )
    question: str | None = Field(default=None, description="For 'needs_decision'")
    confidence: float = Field(ge=0, le=1)
    risks: list[str] = Field(default_factory=list, description="What a reviewer should check")


class ReviewAnswer(BaseModel):
    verdict: Literal["approve", "concerns", "reject"]
    intent_met: bool
    issues: list[str] = Field(default_factory=list)
    summary: str
    confidence: float = Field(ge=0, le=1)


@dataclass
class ClusterContext:
    """What both agents are told about a cluster, prepared by the migrate step."""

    cluster_id: int
    name: str
    members: str  # one line per patch
    intent: str
    net_diff: str  # tree-relative
    fates: str
    review_feedback: str = ""  # what humans said about earlier attempts
    memory: str = ""  # what earlier migrations learned (migration memory)


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n[... truncated, {len(text) - limit} more characters ...]"


def _policy(mcp: dict | None, write: bool) -> ToolPolicy:
    allow = (["write"] if write else []) + ([SERVER] if mcp else [])
    deny = ["shell"] if write else ["shell", "write"]
    return ToolPolicy(allow=allow, deny=deny, mcp_servers={SERVER: mcp} if mcp else {})


def _feedback_section(ctx: ClusterContext) -> str:
    if not ctx.review_feedback:
        return ""
    return (
        "\n## What reviewers said about earlier attempts\n\n"
        f"{ctx.review_feedback}\n\nAddress this feedback; it overrides the mechanical result.\n"
    )


def cluster_brief(
    config: ProjectConfig, ctx: ClusterContext, situation: str, feedback: str = ""
) -> str:
    template = files("patchmig.agents.briefs").joinpath("cluster_agent.md").read_text()
    if feedback:
        feedback = (
            "\n## Your previous attempt was rejected by the automatic checks\n\n"
            f"{feedback}\n\nThe worktrees still hold your previous edits. Fix the problems above."
        )
    return template.format(
        source_version=config.migration.source_version,
        target_version=config.migration.target_version,
        server=SERVER,
        name=ctx.name,
        members=ctx.members,
        intent=ctx.intent,
        situation=situation,
        review_feedback=_feedback_section(ctx),
        memory=(
            f"\n## From earlier migrations (migration memory)\n\n{ctx.memory}\n"
            if ctx.memory
            else ""
        ),
        fates=ctx.fates,
        net_diff=_truncate(ctx.net_diff, config.migrate.brief_max_diff_chars),
        feedback=feedback,
    ) + answer_instructions(ClusterAnswer)


def ask_cluster_agent(
    runner: AgentRunner,
    config: ProjectConfig,
    ctx: ClusterContext,
    workdir: Path,
    situation: str,
    mcp: dict | None,
    feedback: str = "",
    attempt: int = 1,
) -> AgentOutcome:
    task = AgentTask(
        agent="cluster_agent",
        key=f"cluster:{ctx.cluster_id}:migrate:{attempt}",
        brief=cluster_brief(config, ctx, situation, feedback),
        schema=ClusterAnswer,
        workdir=workdir,
        tools=_policy(mcp, write=True),
        subject_type="cluster",
        subject_id=ctx.cluster_id,
        cluster_id=ctx.cluster_id,
    )
    return runner.run(task)


def review_brief(
    config: ProjectConfig, ctx: ClusterContext, migrated_diff: str, agent_summary: str, notes: str
) -> str:
    template = files("patchmig.agents.briefs").joinpath("reviewer.md").read_text()
    limit = config.migrate.brief_max_diff_chars
    return template.format(
        source_version=config.migration.source_version,
        target_version=config.migration.target_version,
        server=SERVER,
        name=ctx.name,
        members=ctx.members,
        intent=ctx.intent,
        net_diff=_truncate(ctx.net_diff, limit),
        migrated_diff=_truncate(migrated_diff, limit),
        agent_summary=agent_summary,
        notes=notes or "(none)",
    ) + answer_instructions(ReviewAnswer)


def ask_reviewer(
    runner: AgentRunner,
    config: ProjectConfig,
    ctx: ClusterContext,
    workdir: Path,
    migrated_diff: str,
    agent_summary: str,
    notes: str,
    mcp: dict | None,
) -> AgentOutcome:
    task = AgentTask(
        agent="reviewer",
        key=f"cluster:{ctx.cluster_id}:review",
        brief=review_brief(config, ctx, migrated_diff, agent_summary, notes),
        schema=ReviewAnswer,
        workdir=workdir,
        tools=_policy(mcp, write=False),
        subject_type="cluster",
        subject_id=ctx.cluster_id,
        cluster_id=ctx.cluster_id,
    )
    return runner.run(task)


class FixAnswer(BaseModel):
    outcome: Literal["fixed", "not_caused_by_cluster", "cannot_fix"]
    summary: str = Field(description="What was wrong and what you changed, in 1-3 sentences")
    files: list[str] = Field(default_factory=list, description="Files you changed")
    confidence: float = Field(ge=0, le=1)


def fix_brief(
    config: ProjectConfig,
    ctx: ClusterContext,
    migrated_diff: str,
    command: str,
    errors: str,
    previous: list[str],
) -> str:
    template = files("patchmig.agents.briefs").joinpath("build_fixer.md").read_text()
    earlier = ""
    if previous:
        earlier = "\n## Earlier fix attempts (the build still failed after them)\n\n" + "\n".join(
            f"- {p}" for p in previous
        )
    return template.format(
        source_version=config.migration.source_version,
        target_version=config.migration.target_version,
        server=SERVER,
        name=ctx.name,
        members=ctx.members,
        intent=ctx.intent,
        migrated_diff=_truncate(migrated_diff, config.migrate.brief_max_diff_chars),
        command=command,
        errors=errors,
        previous=earlier,
    ) + answer_instructions(FixAnswer)


def ask_build_fixer(
    runner: AgentRunner,
    config: ProjectConfig,
    ctx: ClusterContext,
    workdir: Path,
    migrated_diff: str,
    command: str,
    errors: str,
    previous: list[str],
    mcp: dict | None,
    attempt: int,
) -> AgentOutcome:
    task = AgentTask(
        agent="cluster_agent",
        key=f"cluster:{ctx.cluster_id}:fix:{attempt}",
        brief=fix_brief(config, ctx, migrated_diff, command, errors, previous),
        schema=FixAnswer,
        workdir=workdir,
        tools=_policy(mcp, write=True),
        subject_type="cluster",
        subject_id=ctx.cluster_id,
        cluster_id=ctx.cluster_id,
    )
    return runner.run(task)
