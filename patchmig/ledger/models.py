"""Ledger tables: runs, patches, clusters and the history of every state change."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import JSON, Enum, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from patchmig.ledger.states import State


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    type_annotation_map = {dict[str, Any]: JSON, list[Any]: JSON}


StateColumn = Enum(
    State, native_enum=False, length=32, values_callable=lambda e: [s.value for s in e]
)


class Run(Base):
    __tablename__ = "runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    config_hash: Mapped[str] = mapped_column(String(64))
    config_snapshot: Mapped[dict[str, Any]]
    note: Mapped[str | None] = mapped_column(Text)

    patches: Mapped[list[Patch]] = relationship(back_populates="run", cascade="all, delete-orphan")
    clusters: Mapped[list[Cluster]] = relationship(
        back_populates="run", cascade="all, delete-orphan"
    )


class Cluster(Base):
    __tablename__ = "clusters"

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("runs.id"))
    name: Mapped[str] = mapped_column(String(200))
    state: Mapped[State] = mapped_column(StateColumn, default=State.INGESTED)
    order_index: Mapped[int | None]
    intent: Mapped[str | None] = mapped_column(Text)
    net_diff: Mapped[str | None] = mapped_column(Text)
    confidence: Mapped[float | None]
    result: Mapped[dict[str, Any]] = mapped_column(default=dict)

    run: Mapped[Run] = relationship(back_populates="clusters")
    patches: Mapped[list[Patch]] = relationship(back_populates="cluster")


class Patch(Base):
    __tablename__ = "patches"

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("runs.id"))
    cluster_id: Mapped[int | None] = mapped_column(ForeignKey("clusters.id"))

    # Identity
    seq: Mapped[int] = mapped_column(default=0)  # order in the patch set (oldest first)
    sources: Mapped[list[Any]] = mapped_column(default=list)  # adapters that listed it
    project: Mapped[str] = mapped_column(String(300))
    commit_sha: Mapped[str | None] = mapped_column(String(64))
    diff_path: Mapped[str | None] = mapped_column(Text)
    change_id: Mapped[str | None] = mapped_column(String(64))
    fingerprint: Mapped[str | None] = mapped_column(String(64), index=True)  # git patch-id
    subject: Mapped[str] = mapped_column(Text)
    message: Mapped[str] = mapped_column(Text, default="")
    author: Mapped[str | None] = mapped_column(String(300))
    authored_at: Mapped[datetime | None]

    # Links and footprint
    jira_keys: Mapped[list[Any]] = mapped_column(default=list)
    links: Mapped[dict[str, Any]] = mapped_column(
        default=dict
    )  # reverts, fixes, cherry-picked-from
    footprint: Mapped[dict[str, Any]] = mapped_column(default=dict)  # files, symbols, ...
    chain_role: Mapped[str | None] = mapped_column(String(32))  # base, fix, improvement, revert

    # Analysis and outcome
    intent: Mapped[str | None] = mapped_column(Text)  # rendered for people
    intent_detail: Mapped[dict[str, Any] | None] = mapped_column(JSON)  # structured, with sources
    state: Mapped[State] = mapped_column(StateColumn, default=State.INGESTED)
    verdict: Mapped[dict[str, Any]] = mapped_column(default=dict)  # verdict, evidence
    confidence: Mapped[float | None]

    run: Mapped[Run] = relationship(back_populates="patches")
    cluster: Mapped[Cluster | None] = relationship(back_populates="patches")


class Transition(Base):
    """Append-only history of state changes, for audit and for the front end's timeline."""

    __tablename__ = "transitions"

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("runs.id"), index=True)
    entity_type: Mapped[str] = mapped_column(String(16))  # "patch" or "cluster"
    entity_id: Mapped[int]
    from_state: Mapped[State] = mapped_column(StateColumn)
    to_state: Mapped[State] = mapped_column(StateColumn)
    actor: Mapped[str] = mapped_column(
        String(100)
    )  # "orchestrator", "agent:<name>", "human:<user>"
    reason: Mapped[str] = mapped_column(Text, default="")
    forced: Mapped[bool] = mapped_column(default=False)
    at: Mapped[datetime] = mapped_column(default=utcnow)


class KnowledgeItem(Base):
    """One item of a run's knowledge pack (footprint map, upstream fate of a file, notes).

    Items are hypotheses with their source; agents confirm them in the code before relying
    on them.
    """

    __tablename__ = "knowledge"

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("runs.id"), index=True)
    kind: Mapped[str] = mapped_column(String(32))  # "footprint", "file_fate", ...
    key: Mapped[str] = mapped_column(String(500))  # e.g. "frameworks/base:core/Foo.java"
    data: Mapped[dict[str, Any]]
    source: Mapped[str] = mapped_column(String(64), default="deterministic")
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class Ticket(Base):
    """Cached Jira ticket, shared across runs; refreshed on every ingest.

    Everything the ticket carries is kept, for the analysis: text, comments, linked tickets,
    remote links and every attachment (downloaded up to jira.attachment_max_kb; the path is
    the local copy, None when it was only listed).
    """

    __tablename__ = "tickets"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    summary: Mapped[str] = mapped_column(Text, default="")
    description: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(100), default="")
    issue_links: Mapped[list[Any]] = mapped_column(default=list)
    comments: Mapped[list[Any] | None] = mapped_column(default=list)  # "author: text"
    # filename, url, size, mime, path (local copy or None), note (why not downloaded)
    attachments: Mapped[list[Any] | None] = mapped_column(default=list)
    remote_links: Mapped[list[Any] | None] = mapped_column(default=list)  # url, title
    fetched_at: Mapped[datetime] = mapped_column(default=utcnow)


class BacklogItem(Base):
    """One item of a run's backlog: a Jira ticket, or a patch that has no ticket.

    The backlog lasts: importing again refreshes the items and adds new ones, it never
    starts over. Engineers add what the ticket leaves out at any time: text as notes (see
    patchmig.notes), links and files here, in `added`. The item's analysis is kept with the
    ones before it, so a rerun after new information can be compared with the last one.
    """

    __tablename__ = "backlog"
    __table_args__ = (UniqueConstraint("run_id", "key"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("runs.id"), index=True)
    key: Mapped[str] = mapped_column(String(200))  # Jira key, or "patch:<patch id>"
    kind: Mapped[str] = mapped_column(String(16))  # ticket, patch
    title: Mapped[str] = mapped_column(Text, default="")
    sources: Mapped[list[Any]] = mapped_column(default=list)  # how it came in
    # Engineer additions: {id, kind: link|file, url | filename+path, by, at, removed_at}
    added: Mapped[list[Any]] = mapped_column(default=list)
    imported_at: Mapped[datetime] = mapped_column(default=utcnow)
    refreshed_at: Mapped[datetime] = mapped_column(default=utcnow)
    analysis: Mapped[dict[str, Any] | None] = mapped_column(JSON)  # the latest
    analyses: Mapped[list[Any]] = mapped_column(default=list)  # earlier ones, oldest first
    analysed_at: Mapped[datetime | None]
    # What failed at the item's last import (Jira refused, a download failed): empty when it
    # was read completely. `migrate ingest --incomplete` imports these items again.
    import_problems: Mapped[list[Any] | None] = mapped_column(JSON)


class AgentCall(Base):
    """One call to the agent runtime: for credit accounting, audit and the front end."""

    __tablename__ = "agent_calls"

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("runs.id"), index=True)
    agent: Mapped[str] = mapped_column(String(64))
    task_key: Mapped[str] = mapped_column(String(200))
    subject_type: Mapped[str | None] = mapped_column(String(16))
    subject_id: Mapped[int | None]
    cluster_id: Mapped[int | None] = mapped_column(index=True)
    attempt: Mapped[int] = mapped_column(default=1)
    model: Mapped[str | None] = mapped_column(String(100))
    started_at: Mapped[datetime] = mapped_column(default=utcnow)
    duration_s: Mapped[float] = mapped_column(default=0.0)
    credits: Mapped[float] = mapped_column(default=0.0)  # estimate from llm.credits_per_call
    ok: Mapped[bool] = mapped_column(default=False)
    error: Mapped[str | None] = mapped_column(Text)
    prompt_chars: Mapped[int] = mapped_column(default=0)
    output_chars: Mapped[int] = mapped_column(default=0)
    output: Mapped[str] = mapped_column(Text, default="")  # tail of the raw answer


class Note(Base):
    """What an engineer tells the agents, beyond the patches and tickets.

    Notes belong to the project, not to one run, so they carry over to later runs. A note
    applies to every patch, to patches touching a path glob, to one patch (by Change-Id or
    patch-id, so the same patch in a later run), to one cluster, or to the patches of one
    Jira ticket; and to the analyse step (analyst), the migrate step (cluster agent,
    reviewer, build fixer) or both. Edits keep the earlier texts; removal is a timestamp.
    """

    __tablename__ = "notes"

    id: Mapped[int] = mapped_column(primary_key=True)
    scope: Mapped[str] = mapped_column(String(16))  # all, path, patch, cluster, ticket
    target: Mapped[str] = mapped_column(String(500), default="")
    label: Mapped[str] = mapped_column(Text, default="")  # what the target is, for people
    stages: Mapped[list[Any]] = mapped_column(default=list)  # analyse, migrate
    text: Mapped[str] = mapped_column(Text)
    author: Mapped[str] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow)
    removed_at: Mapped[datetime | None]
    revisions: Mapped[list[Any]] = mapped_column(default=list)  # earlier {text, by, at}
