"""Repo mapping: which project of a `repo` target tree a diff belongs to.
A `git` or `plain` tree has no project list: its projects are the directories holding
a `.git`, nested ones included (`ROOT_PROJECT` for the root), or `ROOT_PROJECT` alone
when there are none.

Diff paths are relative to the project root. A project matches a diff when the files
the diff expects to exist (modified, deleted, rename/copy sources) exist under it.
Paths in the ticket description (`{AOSP_HOME}/frameworks/base/...`) are hints: the
longest project-list prefix wins, and a hinted project must match the same way.
Read-only: nothing here touches git.
"""

import os
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Literal

from aosp_migrator.importer.diffparse import FileChange

PROJECT_LIST = Path(".repo") / "project.list"
# The tree root as a project of a `git` or `plain` tree.
ROOT_PROJECT = "."
# Build output at the tree root: huge, and never a project.
_SKIP_AT_ROOT = {"out"}

MappingSource = Literal["hint", "files", "copilot", "manual"]

# A path-like token: `{VAR}/a/b`, `${VAR}/a/b`, `$VAR/a/b` or a bare `a/b/...`.
_PATH_TOKEN = re.compile(r"(?:\$?\{\w+\}/|\$\w+/)?[\w.+\-]+(?:/[\w.+\-]+)+/?")
_VAR_PREFIX = re.compile(r"^(?:\$?\{\w+\}|\$\w+)/")


class ProjectListMissing(Exception):
    def __init__(self, tree: Path) -> None:
        super().__init__(f"{tree / PROJECT_LIST} not found; run `repo sync` in the target tree")


@dataclass
class DiffMapping:
    project: str | None  # Set when exactly one project was found
    source: MappingSource | None
    # Projects still in the running when `project` is None; empty when none matched.
    candidates: list[str] = field(default_factory=list)


def read_project_list(tree: Path) -> list[str]:
    """Project paths of a `repo` tree, relative to its root, in file order."""
    try:
        text = (tree / PROJECT_LIST).read_text()
    except FileNotFoundError:
        raise ProjectListMissing(tree) from None
    return [line.strip().strip("/") for line in text.splitlines() if line.strip()]


def discover_projects(tree: Path) -> list[str]:
    """Projects of a tree without a project list, relative to its root, sorted.

    Does not follow symlinks or enter hidden directories (`.git`, `.repo`, ...).
    """
    projects: list[str] = []
    stack = [tree]
    while stack:
        directory = stack.pop()
        try:
            entries = list(os.scandir(directory))
        except OSError:
            continue
        if any(entry.name == ".git" for entry in entries):
            projects.append(directory.relative_to(tree).as_posix())
        for entry in entries:
            if entry.name.startswith(".") or (directory == tree and entry.name in _SKIP_AT_ROOT):
                continue
            if entry.is_dir(follow_symlinks=False):
                stack.append(Path(entry.path))
    return sorted(projects) or [ROOT_PROJECT]


def path_hints(text: str, projects: Iterable[str]) -> list[str]:
    """Projects named by paths in `text`, longest prefix per path, in order of appearance."""
    by_length = sorted(set(projects), key=len, reverse=True)
    hints: list[str] = []
    for token in _PATH_TOKEN.findall(text):
        path = _VAR_PREFIX.sub("", token).strip("/")
        project = next((p for p in by_length if path == p or path.startswith(p + "/")), None)
        if project is not None and project not in hints:
            hints.append(project)
    return hints


def map_diff(tree: Path, projects: Iterable[str], files: Iterable[FileChange], hints: Iterable[str] = ()) -> DiffMapping:
    """Map one diff to a project of `tree`. A verified hint beats a file-only match."""
    projects = list(projects)
    evidence = _evidence(files)
    if not evidence:
        # Only new files: nothing to check, so a hint (or the only project) is taken on trust.
        hints = list(hints)
        if len(hints) == 1:
            return DiffMapping(hints[0], "hint")
        if not hints and len(projects) == 1:
            return DiffMapping(projects[0], "files")
        return DiffMapping(None, None, hints)
    matches = [p for p in projects if all((tree / p / path).exists() for path in evidence)]
    hinted = [p for p in hints if p in matches]
    if len(hinted) == 1:
        return DiffMapping(hinted[0], "hint")
    if len(matches) == 1:
        return DiffMapping(matches[0], "files")
    return DiffMapping(None, None, hinted or matches)


def _evidence(files: Iterable[FileChange]) -> list[str]:
    """Paths the diff expects to exist before it is applied."""
    paths: list[str] = []
    for change in files:
        if change.status == "added":
            continue
        path = change.old_path if change.status in ("renamed", "copied") and change.old_path else change.path
        # Guard against `..` escaping the project.
        if path and ".." not in PurePosixPath(path).parts and path not in paths:
            paths.append(path)
    return paths
