"""Diff dependencies: a diff written on top of another item's diff, found by blob ids.

A git diff names each file's blobs before and after it (`index <pre>..<post>`). A diff
whose preimage blob the target project lacks was written on a version the target does
not have; a diff of another item whose postimage blob for the same path is that blob
made that version. Abbreviated ids match when one is a prefix of the other. A blob that
a diff of the same item makes (a series, several attachments) is no dependency.

States: `pending`, another diff makes the blob and the target lacks it; `in_target`,
another diff makes it but the target has it already (that diff, or the same content,
is in); `missing`, no known diff makes it and the target lacks it: the base is somewhere
nobody attached. A diff often applies on another version of a file, so `missing` is only
told for files the apply check found conflicting; `pending` always, as a diff that applies
may still need what the other one adds. Without a mapped project or history the target is
not checked, so such a file only gets `pending` rows.
Read-only: the only git call is `cat-file --batch-check`.
"""

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from aosp_migrator.importer.diffparse import parse_diff
from aosp_migrator.prescan import git

DependencyState = Literal["pending", "in_target", "missing"]

_INDEX = re.compile(r"^index ([0-9a-f]{7,40})\.\.([0-9a-f]{7,40})", re.MULTILINE)
# Made by any diff that empties a file: no evidence of anything.
EMPTY_BLOB = "e69de29bb2d1d6434b8b29ae775ad8c2e48c5391"
# Shortest abbreviation git writes; index key for prefix matches.
_ABBREV = 7


@dataclass(frozen=True)
class FileBlobs:
    path: str  # Before the change
    new_path: str  # After the change
    # None for a new (or deleted) file, an empty one, or a diff without `index` lines.
    pre: str | None
    post: str | None


@dataclass(frozen=True)
class DiffFiles:
    """One current diff of an item, with its files' blob ids."""

    attachment_id: int
    item_id: int
    project: str | None
    files: list[FileBlobs]
    # Paths (after the change) the apply check found conflicting.
    conflicts: frozenset[str] = frozenset()


@dataclass(frozen=True)
class Dependency:
    attachment_id: int
    path: str
    blob: str
    depends_on_id: int | None
    state: DependencyState


def file_blobs(text: str) -> list[FileBlobs]:
    """Each file section's blob ids; a file of a series once per commit."""
    out = []
    for change in parse_diff(text).files:
        found = _INDEX.search(change.raw)
        pre, post = (found.group(1), found.group(2)) if found else (None, None)
        out.append(FileBlobs(change.old_path or change.path, change.path, _blob(pre), _blob(post)))
    return out


def _blob(blob: str | None) -> str | None:
    if blob is None or not blob.strip("0") or EMPTY_BLOB.startswith(blob):
        return None
    return blob


def same_blob(a: str, b: str) -> bool:
    return a.startswith(b) or b.startswith(a)


def find(tree: Path, diffs: Sequence[DiffFiles]) -> list[Dependency]:
    """Every dependency of `diffs` on each other, and on blobs no diff makes, in `diffs` order."""
    made: dict[str, list[tuple[DiffFiles, FileBlobs]]] = {}
    own: dict[int, list[str]] = {}
    for diff in diffs:
        for f in diff.files:
            if f.post is not None:
                made.setdefault(f.post[:_ABBREV], []).append((diff, f))
                own.setdefault(diff.item_id, []).append(f.post)
    needs = [
        (diff, f)
        for diff in diffs
        for f in diff.files
        if f.pre is not None and not any(same_blob(f.pre, post) for post in own.get(diff.item_id, []))
    ]
    by_project: dict[str, set[str]] = {}
    for diff, f in needs:
        if diff.project is not None and f.pre is not None:
            by_project.setdefault(diff.project, set()).add(f.pre)
    present = {project: target_blobs(tree / project, blobs) for project, blobs in by_project.items()}

    deps: dict[Dependency, None] = {}
    for diff, f in needs:
        assert f.pre is not None
        makers = [
            other
            for other, g in made.get(f.pre[:_ABBREV], [])
            if other.item_id != diff.item_id
            and g.post is not None
            and same_blob(f.pre, g.post)
            and g.new_path == f.path
            and (None in (other.project, diff.project) or other.project == diff.project)
        ]
        known = present.get(diff.project or "", {}).get(f.pre)
        for other in makers:
            deps[Dependency(diff.attachment_id, f.path, f.pre, other.attachment_id, "in_target" if known else "pending")] = None
        if not makers and known is False and f.new_path in diff.conflicts:
            deps[Dependency(diff.attachment_id, f.path, f.pre, None, "missing")] = None
    return list(deps)


def target_blobs(project_dir: Path, blobs: Iterable[str]) -> dict[str, bool | None]:
    """Whether the project has each blob; None for all when it has no history to look in."""
    names = sorted(blobs)
    if not project_dir.is_dir() or git.head(project_dir) is None:
        return dict.fromkeys(names)
    result = git.git(project_dir, "cat-file", "--batch-check", input="".join(f"{name}\n" for name in names), check=False)
    lines = result.stdout.splitlines()
    if result.returncode != 0 or len(lines) != len(names):
        return dict.fromkeys(names)
    # `<name> missing` when absent; `<name> ambiguous` means several objects have that prefix.
    return {name: not line.endswith(" missing") for name, line in zip(names, lines, strict=True)}
