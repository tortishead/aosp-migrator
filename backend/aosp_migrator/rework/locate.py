"""Where the target has a file now, for re-implement (rung 3c), and the source tree's before/after of it.

A file of the item's diffs that neither applied, merged nor left `.rej` hunks (`failed`:
gone at its path, moved, rewritten) is looked for in the scratch checkout's base:
1. at its path (still there, rewritten so far that nothing applies);
2. by content: up to `PROBES` distinctive lines of the file before the change (the
   preimage blob, from the target's or the source project's objects; else the
   original's context and removed lines) are searched with one `git grep` over the base;
   the file with the most of them wins when it has at least `MIN_SHARE` (ties: the same
   file name, then the shorter path);
3. by name: a file with the same name, the one sharing the most trailing directories
   (ties: the shorter path).
Not found: gone; Copilot gets the intent and the source before/after only.

The found file is written into the scratch worktree (it is outside the sparse checkout),
so Copilot can edit it there. Only the scratch repository is read and written.
"""

import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Literal

from aosp_migrator.prescan import git
from aosp_migrator.prescan.patch import FileSections
from aosp_migrator.rework.scratch import Scratch, sparse_pattern

How = Literal["path", "content", "name"]
# Lines of the file before the change looked for in the target.
PROBES = 20
# Shortest probe line (stripped), so common lines (`return 0;`, `}`) do not count.
PROBE_MIN = 12
# Share of the probes a file must have to be the one by content.
MIN_SHARE = 0.5
_INDEX = re.compile(r"^index ([0-9a-f]+)\.\.([0-9a-f]+)", re.MULTILINE)
_NULL = re.compile(r"^0+$")


@dataclass
class Location:
    path: str  # In the item's diff
    found: str | None = None  # Where the target has it now
    how: How | None = None
    matched: tuple[int, int] | None = None  # By content: probes found, probes looked for
    before: str | None = None  # The source tree's file before the change
    after: str | None = None  # And after it

    @property
    def edit_path(self) -> str:
        """The file Copilot edits: where it is now, else its path in the diff (to create there, if it belongs)."""
        return self.found or self.path

    def describe(self) -> str:
        if self.how == "path":
            return f"still at {self.path}, changed too much for the change to apply"
        if self.how == "content" and self.matched:
            return f"moved to {self.found} (found by content: {self.matched[0]} of {self.matched[1]} lines)"
        if self.how == "name":
            return f"moved to {self.found} (found by name only)"
        return "not in the target: gone"


def locate(scratch: Scratch, section: FileSections, source_project: Path | None = None) -> Location:
    """Where `section`'s file is in `scratch`'s base, with the source before/after; the found file is checked out."""
    path = section.path
    old_path = section.changes[0].old_path or path
    before_id, after_id = _blob_ids(section)
    if (before_id or after_id) and source_project is not None:
        scratch.add_source(source_project)
    location = Location(path, before=_blob(scratch, before_id), after=_blob(scratch, after_id))
    files = git.git(scratch.path, "ls-tree", "-r", "--name-only", "-z", scratch.base).stdout.split("\0")
    files = [f for f in files if f]
    if path in files or old_path in files:
        location.found, location.how = path if path in files else old_path, "path"
    elif found := _by_content(scratch, location.before or _preimage_lines(section), PurePosixPath(old_path).name):
        location.found, location.how, location.matched = found[0], "content", found[1]
    elif found_name := _by_name(files, old_path):
        location.found, location.how = found_name, "name"
    if location.found is not None and location.how != "path":
        _check_out(scratch, location.found)
    return location


def _blob_ids(section: FileSections) -> tuple[str | None, str | None]:
    """The preimage blob of the first change and the postimage of the last (None for a new or deleted file)."""
    first = _INDEX.search(section.changes[0].raw)
    last = _INDEX.search(section.changes[-1].raw)
    before = first.group(1) if first and not _NULL.match(first.group(1)) else None
    after = last.group(2) if last and not _NULL.match(last.group(2)) else None
    return before, after


def _blob(scratch: Scratch, oid: str | None) -> str | None:
    if oid is None:
        return None
    result = git.git(scratch.path, "cat-file", "-p", f"{oid}^{{blob}}", check=False)
    return result.stdout if result.returncode == 0 else None


def _preimage_lines(section: FileSections) -> str:
    """The original's context and removed lines: what it knew of the file before."""
    return "".join(
        line[1:] + "\n" for change in section.changes for hunk in change.hunks for line in hunk.splitlines()[1:] if line[:1] in (" ", "-")
    )


def probes(text: str) -> list[str]:
    """Up to `PROBES` distinct lines of `text`, stripped, at least `PROBE_MIN` long, spread over the file."""
    seen: dict[str, None] = {}
    for line in text.splitlines():
        line = line.strip()
        if len(line) >= PROBE_MIN and re.search(r"[A-Za-z]", line):
            seen[line] = None
    lines = list(seen)
    if len(lines) <= PROBES:
        return lines
    step = len(lines) / PROBES
    return [lines[int(n * step)] for n in range(PROBES)]


def _by_content(scratch: Scratch, text: str, name: str) -> tuple[str, tuple[int, int]] | None:
    wanted = probes(text)
    if not wanted:
        return None
    args = [arg for line in wanted for arg in ("-e", line)]
    result = git.git(scratch.path, "grep", "-I", "-F", "--no-color", "--full-name", "-z", *args, scratch.base, "--", check=False)
    hits: dict[str, set[str]] = {}
    prefix = f"{scratch.base}:"
    for record in result.stdout.splitlines():
        # `<base>:<path>\0<line>` with -z.
        name, _, line = record.partition("\0")
        if not name.startswith(prefix):
            continue
        found = hits.setdefault(name.removeprefix(prefix), set())
        found.update(probe for probe in wanted if probe in line)
    if not hits:
        return None
    best = max(hits, key=lambda p: (len(hits[p]), PurePosixPath(p).name == name, -len(p), p))
    if len(hits[best]) / len(wanted) < MIN_SHARE:
        return None
    return best, (len(hits[best]), len(wanted))


def _by_name(files: list[str], path: str) -> str | None:
    wanted = PurePosixPath(path)
    same = [f for f in files if PurePosixPath(f).name == wanted.name]
    if not same:
        return None

    def shared(file: str) -> int:
        parts, others = PurePosixPath(file).parts[:-1][::-1], wanted.parts[:-1][::-1]
        n = 0
        while n < min(len(parts), len(others)) and parts[n] == others[n]:
            n += 1
        return n

    return max(same, key=lambda f: (shared(f), -len(f), f))


def _check_out(scratch: Scratch, path: str) -> None:
    """Write `path` from the base into the worktree and the sparse patterns, as a file Copilot may edit."""
    with (scratch.path / ".git" / "info" / "sparse-checkout").open("a") as patterns:
        patterns.write(f"{sparse_pattern(path)}\n")
    git.git(scratch.path, "checkout", "-q", "--ignore-skip-worktree-bits", scratch.base, "--", path)
    git.git(scratch.path, "update-index", "--no-skip-worktree", "--", path)
