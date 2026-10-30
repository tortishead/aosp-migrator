"""Scratch checkout of one target project for a rework attempt, and rungs 1-2 of the ladder in it.

Never written in the user's tree: a fresh repository at the attempt's scratch path whose
only objects are the project's object store as alternate (what `git clone --shared
--no-checkout` gives, without copying the project's refs), checked out detached at the
project HEAD, sparse: only the files the item's diffs touch. The base patches go in with
`git apply --cached` (3-way when that does not apply) and are committed, so the base
commit is HEAD plus the base and `git diff <base commit>` is the item's change alone.
The project itself is only read.

Rungs, per file of the item's diffs (files the caller found present left out):
1. Stack: `git apply --index`, else `--3way` on the base: `applied` / `merged`, or
   `conflict` with the markers left in the file (diff3 style), or a preimage blob the
   target lacks.
2. Source base: for those, the same-path project of the source tree is added as a
   further alternate and the 3-way merge runs again. No source project there, or not a
   git project: the rung is skipped (noted).
What is still neither applied nor in conflict goes through `git apply --reject`:
`rejected` with the `.rej` hunks (the hunks that did apply are in the file), or `failed`
when git wrote no `.rej` (file gone, binary section without data).

When every file is `applied` / `merged`, the patch is the diff of the index against the
base commit, level `stacked`, or `source3way` when rung 2 merged a file.
"""

import re
import shutil
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from aosp_migrator.prescan import git
from aosp_migrator.prescan.apply import BINARY, MISSING_BLOB
from aosp_migrator.prescan.patch import FileSections, Patch

FileStatus = Literal["applied", "merged", "conflict", "rejected", "failed"]
Rung = Literal["stack", "source"]
Level = Literal["stacked", "source3way"]

IDENTITY = ("aosp-migrator", "aosp-migrator@localhost")
# Local config of every scratch repository, whatever the user's global config says.
_CONFIG = {
    "core.sparseCheckout": "true",
    "core.autocrlf": "false",
    "core.fsmonitor": "false",
    "commit.gpgsign": "false",
    "gc.auto": "0",
    "merge.conflictStyle": "diff3",
    "user.name": IDENTITY[0],
    "user.email": IDENTITY[1],
}
# Fixed prefixes and no external tools in every diff we keep.
_DIFF = ("diff", "--binary", "-M", "--no-color", "--no-ext-diff", "--no-textconv", "--src-prefix=a/", "--dst-prefix=b/")
_MARKER = re.compile(r"^(<{7}|\|{7}|={7}|>{7})(?: |$)")
_SPARSE_SPECIAL = re.compile(r"([\\*?\[])")


class ScratchError(Exception):
    """The scratch checkout could not be made (no history, a base patch that does not apply)."""


@dataclass
class Region:
    """One conflict region of a file, lines 1-based, markers included."""

    start: int
    end: int
    ours: str
    base: str | None
    theirs: str


@dataclass
class FileResult:
    path: str
    status: FileStatus
    rung: Rung | None = None  # The rung that applied or merged the file, or left its markers
    error: str | None = None  # Why it is not applied or merged
    notes: list[str] = field(default_factory=list)  # Rungs skipped and why
    regions: list[Region] = field(default_factory=list)  # `conflict`
    hunks: list[str] = field(default_factory=list)  # `rejected`: the `.rej` hunks, `@@` header included


@dataclass
class RungsResult:
    files: list[FileResult]
    level: Level | None = None  # Set when every file is applied or merged
    patch: str | None = None  # Against the base commit, with `level`


def touched_files(diffs: Iterable[str]) -> list[str]:
    """Every path the diffs read or write (both sides of a rename), in diff order."""
    paths: dict[str, None] = {}
    for text in diffs:
        for section in Patch.parse(text).files:
            for change in section.changes:
                paths.update(dict.fromkeys(p for p in (change.old_path, change.path) if p))
    return list(paths)


class Scratch:
    def __init__(self, path: Path, project_dir: Path, head: str, base: str) -> None:
        self.path = path
        self.project_dir = project_dir
        self.head = head  # The project HEAD
        self.base = base  # HEAD plus the base patches
        self._source_added: dict[Path, str | None] = {}

    @classmethod
    def create(cls, path: Path, project_dir: Path, files: Iterable[str], base_patches: Sequence[str] = ()) -> "Scratch":
        """Scratch checkout of `project_dir` at `path` (must not exist), `files` checked out, `base_patches` committed."""
        head = git.head(project_dir) if project_dir.is_dir() else None
        if head is None:
            raise ScratchError(f"{project_dir.name}: {'no git history' if project_dir.is_dir() else 'not found'}")
        objects = _objects_dir(project_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        # No template: hooks of the user's `init.templateDir` stay out.
        git.git(path.parent, "init", "-q", "--template=", path.name)
        try:
            (path / ".git" / "objects" / "info").mkdir(parents=True, exist_ok=True)
            (path / ".git" / "objects" / "info" / "alternates").write_text(f"{objects}\n")
            (path / ".git" / "hooks").mkdir(exist_ok=True)
            for key, value in {**_CONFIG, "core.hooksPath": str(path / ".git" / "hooks")}.items():
                git.git(path, "config", key, value)
            (path / ".git" / "info").mkdir(exist_ok=True)
            (path / ".git" / "info" / "sparse-checkout").write_text("".join(f"{sparse_pattern(f)}\n" for f in files))
            git.git(path, "checkout", "-q", "--detach", head)
            for n, patch in enumerate(base_patches, 1):
                if git.apply(path, patch, "--cached").returncode != 0 and (error := git.apply(path, patch, "--3way", "--cached")).returncode != 0:
                    raise ScratchError(f"base patch {n} does not apply: {_first_error(error.stderr)}")
            if base_patches:
                git.git(path, "commit", "-q", "--no-verify", "-m", "Base patches")
                # The checked-out files take the base's content; the others stay out.
                git.git(path, "reset", "-q", "--hard")
            base = git.git(path, "rev-parse", "HEAD").stdout.strip()
        except BaseException:
            shutil.rmtree(path, ignore_errors=True)
            raise
        return cls(path, project_dir, head, base)

    def checked_out(self) -> list[str]:
        """Files in the worktree, `.git` left out."""
        return sorted(p.relative_to(self.path).as_posix() for p in self.path.rglob("*") if p.is_file() and ".git" not in p.relative_to(self.path).parts)

    def rungs(self, diffs: Sequence[str], source_project: Path | None = None, present: Iterable[str] = ()) -> RungsResult:
        """Rungs 1-2 for each file of `diffs` but those in `present`; `source_project` is the same-path project of the source tree, if any."""
        skip = set(present)
        sections: dict[str, list[FileSections]] = {}
        for text in diffs:
            for section in Patch.parse(text).files:
                if section.path not in skip:
                    sections.setdefault(section.path, []).append(section)
        results: dict[str, FileResult] = {}
        missing: list[str] = []
        for path, parts in sections.items():
            patch = "".join(s.patch for s in parts)
            if any(s.binary for s in parts):
                results[path] = FileResult(path, "failed", error=BINARY)
            elif git.apply(self.path, patch, "--index").returncode == 0:
                results[path] = FileResult(path, "applied", "stack")
            else:
                results[path] = self._merge(path, patch, "stack")
                if results[path].error == MISSING_BLOB:
                    missing.append(path)
        for path in missing:
            patch = "".join(s.patch for s in sections[path])
            reason = self.add_source(source_project)
            if reason is None:
                merged = self._merge(path, patch, "source")
                if merged.status != "failed":
                    results[path] = merged
                    continue
                results[path].notes.append(f"source base: {merged.error}")
            else:
                results[path].notes.append(f"source base skipped: {reason}")
        for path, result in results.items():
            if result.status == "failed" and result.error != BINARY:
                results[path] = self._reject(result, "".join(s.patch for s in sections[path]))
        files = list(results.values())
        if not all(f.status in ("applied", "merged") for f in files):
            return RungsResult(files)
        level: Level = "source3way" if any(f.rung == "source" for f in files) else "stacked"
        return RungsResult(files, level, self.patch())

    def patch(self) -> str | None:
        """The change against the base commit, worktree edits included; None when there is none."""
        # `--sparse`: also files written outside the sparse patterns (Copilot creating one).
        git.git(self.path, "add", "-A", "--sparse")
        return git.git(self.path, *_DIFF, "--cached", self.base).stdout or None

    def commit(self, message: str) -> None:
        """Commit the change so far: it becomes part of the base (the next item of a unit goes on top of it)."""
        if self.patch() is not None:
            git.git(self.path, "commit", "-q", "--no-verify", "-m", message)
            self.base = git.git(self.path, "rev-parse", "HEAD").stdout.strip()

    def remove(self) -> None:
        shutil.rmtree(self.path, ignore_errors=True)

    def _merge(self, path: str, patch: str, rung: Rung) -> FileResult:
        """`git apply --3way` (index and worktree): merged, conflict with markers, or failed with why."""
        result = git.apply(self.path, patch, "--3way")
        if result.returncode == 0:
            return FileResult(path, "merged", rung)
        if "with conflicts" in result.stderr:
            return FileResult(path, "conflict", rung, error="3-way merge conflicts", regions=read_regions(self.path / path))
        error = MISSING_BLOB if "lacks the necessary blob" in result.stderr else _first_error(result.stderr)
        return FileResult(path, "failed", error=error)

    def _reject(self, result: FileResult, patch: str) -> FileResult:
        """`git apply --reject` into the worktree: the hunks that apply go in, the others to `<path>.rej`."""
        (self.path / f"{result.path}.rej").unlink(missing_ok=True)
        applied = git.apply(self.path, patch, "--reject")
        if hunks := read_rejects(self.path / f"{result.path}.rej"):
            return FileResult(result.path, "rejected", error=result.error, notes=result.notes, hunks=hunks)
        return FileResult(result.path, "failed", error=_first_error(applied.stderr) or result.error, notes=result.notes)

    def add_source(self, project: Path | None) -> str | None:
        """Add `project`'s objects as an alternate (once); None when added, else why the rung is skipped."""
        if project is None:
            return "no source tree"
        if project not in self._source_added:
            reason: str | None = None
            if not project.is_dir():
                reason = f"no source project at {project.name}"
            elif git.head(project) is None:
                reason = f"source project {project.name} has no git history"
            else:
                with (self.path / ".git" / "objects" / "info" / "alternates").open("a") as alternates:
                    alternates.write(f"{_objects_dir(project)}\n")
            self._source_added[project] = reason
        return self._source_added[project]


def sparse_pattern(path: str) -> str:
    """A non-cone sparse-checkout pattern matching exactly `path` from the top."""
    return "/" + _SPARSE_SPECIAL.sub(r"\\\1", path)


def _objects_dir(project_dir: Path) -> Path:
    objects = Path(git.git(project_dir, "rev-parse", "--git-path", "objects").stdout.strip())
    return (objects if objects.is_absolute() else project_dir / objects).resolve()


def _first_error(stderr: str) -> str:
    errors = [line.removeprefix("error: ").removeprefix("fatal: ") for line in stderr.splitlines() if line.startswith(("error: ", "fatal: "))]
    return errors[-1] if errors else stderr.strip()


def read_regions(file: Path) -> list[Region]:
    """The conflict regions of a file with markers (`merge` or `diff3` style)."""
    try:
        lines = file.read_text(errors="replace").splitlines(keepends=True)
    except OSError:
        return []
    regions: list[Region] = []
    start: int | None = None
    parts: dict[str, list[str]] = {}
    part = ""
    for n, line in enumerate(lines, 1):
        marker = _MARKER.match(line)
        kind = marker.group(1)[0] if marker else None
        if kind == "<":
            start, parts, part = n, {"ours": []}, "ours"
        elif start is None:
            continue
        elif kind == "|" and part == "ours":
            parts["base"], part = [], "base"
        elif kind == "=" and part in ("ours", "base"):
            parts["theirs"], part = [], "theirs"
        elif kind == ">" and part == "theirs":
            base = parts.get("base")
            regions.append(Region(start, n, "".join(parts["ours"]), "".join(base) if base is not None else None, "".join(parts["theirs"])))
            start = None
        else:
            parts[part].append(line)
    return regions


def read_rejects(file: Path) -> list[str]:
    """The hunks of a `.rej` file, `@@` header included; empty when there is none."""
    try:
        text = file.read_text(errors="replace")
    except OSError:
        return []
    hunks: list[str] = []
    for line in text.splitlines(keepends=True):
        if line.startswith("@@ "):
            hunks.append(line)
        elif hunks:
            hunks[-1] += line
    return hunks
