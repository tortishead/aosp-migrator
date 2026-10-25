"""The backlog: one item per Jira ticket (or patch without a ticket), and what people add.

Import fills and refreshes the backlog (patchmig.ingest). At any point of the workflow an
engineer can add what the ticket leaves out, and then import and analyse the item again:

- text: becomes an engineer note on the ticket (or patch), given to the agents;
- a link: a commit or PR link becomes a patch of the ticket at the next import; any other
  link (a spec, a bug in another tracker) is context for the analysis;
- a file: a .diff/.patch becomes a patch of the ticket at the next import; any other file
  (a log, a document) is context for the analysis.

Additions are never deleted, only marked removed, so the history of what was known stays.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select

from patchmig import notes as engineer_notes
from patchmig.config import ProjectConfig
from patchmig.ledger import BacklogItem, LedgerStore, Patch, Run

MAX_FILE_BYTES = 5 * 1024 * 1024


class BacklogError(Exception):
    pass


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def items(store: LedgerStore, run: Run) -> list[BacklogItem]:
    with store.session() as s:
        return list(s.scalars(
            select(BacklogItem).where(BacklogItem.run_id == run.id).order_by(BacklogItem.id)
        ))  # fmt: skip


def get(store: LedgerStore, run: Run, key: str) -> BacklogItem:
    with store.session() as s:
        item = s.scalars(
            select(BacklogItem).where(BacklogItem.run_id == run.id, BacklogItem.key == key)
        ).first()
    if item is None:
        raise BacklogError(f"{key} is not in the backlog of run {run.id}")
    return item


def item_patches(store: LedgerStore, item: BacklogItem) -> list[Patch]:
    """The run's patches that belong to the item: those of the ticket, or the one patch."""
    with store.session() as s:
        if item.kind == "patch":
            p = s.get(Patch, int(item.key.removeprefix("patch:")))
            return [p] if p is not None else []
        rows = s.scalars(select(Patch).where(Patch.run_id == item.run_id).order_by(Patch.seq)).all()
    return [p for p in rows if item.key in (p.jira_keys or [])]


def item_of(patch: Patch, keys: set[str]) -> str:
    """The backlog key a patch is analysed under: its first ticket in the backlog, else itself."""
    return next((k for k in patch.jira_keys or [] if k in keys), f"patch:{patch.id}")


def active_additions(item: BacklogItem) -> list[dict]:
    return [a for a in item.added or [] if not a.get("removed_at")]


def add_info(
    config: ProjectConfig,
    store: LedgerStore,
    run: Run,
    key: str,
    by: str,
    text: str | None = None,
    link: str | None = None,
    filename: str | None = None,
    content: bytes | str | None = None,
    stages: list[str] | None = None,
) -> dict:
    """Add one piece of information to an item: text (a note), a link or a file.

    Returns what was added: {"note": id} for text, else the addition record.
    """
    item = get(store, run, key)
    given = [x for x in (text, link, filename) if x]
    if len(given) != 1:
        raise BacklogError("add one thing at a time: text, a link or a file")
    if text:
        if item.kind == "ticket":
            note = engineer_notes.add_for_run(store, run, text, by, "ticket", key, stages)
        else:
            note = engineer_notes.add_for_run(store, run, text, by, "patch",
                                              key.removeprefix("patch:"), stages)  # fmt: skip
        return {"note": note.id}
    entry: dict = {"id": max((a["id"] for a in item.added or []), default=0) + 1, "by": by,
                   "at": _now()}  # fmt: skip
    if link:
        link = link.strip()
        if not re.match(r"https?://\S+$", link):
            raise BacklogError("a link must be one http:// or https:// address")
        entry |= {"kind": "link", "url": link}
    else:
        data = content.encode() if isinstance(content, str) else content or b""
        if not data:
            raise BacklogError(f"{filename} is empty")
        if len(data) > MAX_FILE_BYTES:
            raise BacklogError(f"{filename} is larger than {MAX_FILE_BYTES // 1024 // 1024} MB")
        name = re.sub(r"[^\w.-]+", "_", Path(filename).name).lstrip(".") or "file"
        folder = config.workdir / "added" / re.sub(r"[^\w.-]+", "_", key)
        path = folder / f"{entry['id']}-{name}"
        folder.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        entry |= {"kind": "file", "filename": name, "path": str(path), "size": len(data)}
    with store.session() as s, s.begin():
        row = s.get(BacklogItem, item.id)
        row.added = [*(row.added or []), entry]
    return entry


def remove_info(store: LedgerStore, run: Run, key: str, addition_id: int, by: str) -> None:
    item = get(store, run, key)
    if not any(a["id"] == addition_id and not a.get("removed_at") for a in item.added or []):
        raise BacklogError(f"{key} has no addition {addition_id}")
    with store.session() as s, s.begin():
        row = s.get(BacklogItem, item.id)
        row.added = [
            {**a, "removed_at": _now(), "removed_by": by} if a["id"] == addition_id else a
            for a in row.added
        ]
