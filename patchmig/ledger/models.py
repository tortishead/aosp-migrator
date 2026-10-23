"""Ledger tables: runs, patches, clusters and the history of every state change."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import JSON, Enum, ForeignKey, String, Text
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
    intent: Mapped[str | None] = mapped_column(Text)
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
