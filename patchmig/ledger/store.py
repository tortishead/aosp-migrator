"""Access to the ledger database: the single source of truth of a migration."""

from __future__ import annotations

from collections import Counter
from pathlib import Path

from sqlalchemy import Engine, create_engine, func, inspect, select, text
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from patchmig.config import ProjectConfig, config_hash
from patchmig.config.loader import config_to_dict
from patchmig.ledger.models import Base, Cluster, Patch, Run, Transition
from patchmig.ledger.states import InvalidTransition, State, can_transition

ENTITY_TYPES: dict[type, str] = {Patch: "patch", Cluster: "cluster"}
# Writers wait this long for each other (parallel verify slots, the web settings, a CLI run)
# instead of failing at once with "database is locked".
BUSY_TIMEOUT_S = 30


def _add_missing_columns(engine: Engine) -> None:
    """Ledgers made by an older version lack columns added since: add them (empty).

    New columns are always optional, so old rows read as None and code treats that as empty.
    """
    existing = inspect(engine)
    with engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            have = {c["name"] for c in existing.get_columns(table.name)}
            for column in table.columns:
                if column.name not in have:
                    kind = column.type.compile(dialect=engine.dialect)
                    conn.execute(
                        text(f'ALTER TABLE "{table.name}" ADD COLUMN "{column.name}" {kind}')
                    )


class LedgerStore:
    def __init__(self, engine: Engine):
        self.engine = engine
        self._sessions = sessionmaker(engine, expire_on_commit=False)

    @classmethod
    def open(cls, path: Path | str) -> LedgerStore:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        engine = create_engine(f"sqlite:///{path}", connect_args={"timeout": BUSY_TIMEOUT_S})
        store = cls(engine)
        Base.metadata.create_all(store.engine)
        _add_missing_columns(store.engine)
        return store

    @classmethod
    def in_memory(cls) -> LedgerStore:
        """A throwaway ledger (tests, demos). One shared connection, so every thread sees the
        same database: by default each thread would get a new, empty one."""
        engine = create_engine(
            "sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
        )
        store = cls(engine)
        Base.metadata.create_all(store.engine)
        return store

    def session(self) -> Session:
        return self._sessions()

    def create_run(self, config: ProjectConfig, note: str | None = None) -> Run:
        with self.session() as s, s.begin():
            run = Run(
                config_hash=config_hash(config),
                config_snapshot=config_to_dict(config),
                note=note,
            )
            s.add(run)
        return run

    def discard_run(self, run: Run) -> bool:
        """Delete a run that holds nothing yet (a failed ingest); True if it was deleted."""
        with self.session() as s, s.begin():
            if s.scalar(select(func.count()).select_from(Patch).where(Patch.run_id == run.id)):
                return False
            if (row := s.get(Run, run.id)) is not None:
                s.delete(row)
        return True

    def latest_run(self) -> Run | None:
        with self.session() as s:
            return s.scalars(select(Run).order_by(Run.id.desc()).limit(1)).first()

    def add_patch(self, run: Run, **fields) -> Patch:
        with self.session() as s, s.begin():
            patch = Patch(run_id=run.id, **fields)
            s.add(patch)
        return patch

    def transition(
        self,
        entity: Patch | Cluster,
        target: State,
        *,
        actor: str,
        reason: str = "",
        force: bool = False,
    ) -> Patch | Cluster:
        """Move a patch or cluster to a new state and record why.

        Only humans may force a transition outside the allowed table, and they must give a
        reason. The entity is re-read inside the transaction so a stale object can't
        overwrite a newer state.
        """
        if force and not actor.startswith("human:"):
            raise PermissionError("only a human actor may force a transition")
        if force and not reason:
            raise ValueError("a forced transition needs a reason")
        model = type(entity)
        with self.session() as s, s.begin():
            current = s.get(model, entity.id, with_for_update=True)
            if current is None:
                raise LookupError(f"{model.__name__} {entity.id} not found")
            if not force and not can_transition(current.state, target):
                raise InvalidTransition(current.state, target)
            s.add(
                Transition(
                    run_id=current.run_id,
                    entity_type=ENTITY_TYPES[model],
                    entity_id=current.id,
                    from_state=current.state,
                    to_state=target,
                    actor=actor,
                    reason=reason,
                    forced=force,
                )
            )
            current.state = target
        return current

    def history(self, entity: Patch | Cluster) -> list[Transition]:
        entity_type = ENTITY_TYPES[type(entity)]
        with self.session() as s:
            return list(
                s.scalars(
                    select(Transition)
                    .where(Transition.entity_type == entity_type, Transition.entity_id == entity.id)
                    .order_by(Transition.id)
                )
            )

    def patch_counts(self, run: Run) -> Counter[State]:
        with self.session() as s:
            rows = s.execute(
                select(Patch.state, func.count())
                .where(Patch.run_id == run.id)
                .group_by(Patch.state)
            )
            return Counter({state: n for state, n in rows})
