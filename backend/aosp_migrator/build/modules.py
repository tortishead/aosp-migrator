"""Affected modules: what to build for the files a unit changes.

With exactly one `out/target/product/*/module-info.json` under the build root, each
changed file maps to the module(s) with the longest `path` containing it, built with
`m`. With none or several, each file falls back to the nearest directory above it with
an `Android.bp` / `Android.mk`, built with `mmm`. A file under no module builds nothing.

A change that reaches other modules also builds the modules using the changed ones
(`m` only, from the dependency fields of `module-info.json`, direct users, sorted,
capped): a header, an `Android.bp` / `.mk` (exports), an AIDL interface, a Java file
whose added or removed lines touch a public or protected declaration or an
`@SystemApi` / `@TestApi`. Such a Java change, or an `api/*.txt` file, also adds the API
check target.

Changed files are relative to the target tree (`<project>/<path in project>`);
`module-info.json` paths are relative to the build root. The two differ when the
build root is a `.repo` root above the target tree (or a tree below it).
Read-only: nothing here runs a build.
"""

import json
import os
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Literal

from aosp_migrator.importer.diffparse import FileChange
from aosp_migrator.prescan.mapping import ROOT_PROJECT

MODULE_INFO_GLOB = "out/target/product/*/module-info.json"
BUILD_FILES = ("Android.bp", "Android.mk")
HEADER_SUFFIXES = (".h", ".hh", ".hpp", ".hxx", ".inl")
# Fields of a `module-info.json` entry naming the modules it uses; whichever are present.
DEPENDENCY_FIELDS = ("dependencies", "shared_libs", "static_libs", "libs")
# Runs every API check of the tree (`api/*.txt` against the stubs).
API_CHECK_TARGET = "checkapi"
DEFAULT_DEPENDENTS_CAP = 20

# An added or removed line declaring something public or protected, or an API annotation.
_API_LINE = re.compile(r"^[+-]\s*(?:@(?:SystemApi|TestApi)\b|(?:@\w+(?:\([^)]*\))?\s+)*(?:public|protected)\b)")

Tool = Literal["m", "mmm"]


@dataclass
class AffectedModules:
    tool: Tool
    # Module names for `m`, directories relative to the build root for `mmm`: the changed
    # modules (sorted), then their dependents (sorted), then the API check target.
    targets: list[str] = field(default_factory=list)
    # Changed files (relative to the target tree) under no module, or outside the build root.
    unbuilt: list[str] = field(default_factory=list)
    # Users of the changed modules built too, and how many more the cap left out.
    dependents: list[str] = field(default_factory=list)
    dependents_left_out: int = 0
    api_check: bool = False

    @property
    def nothing_to_build(self) -> bool:
        return not self.targets


def changed_files(project: str, changes: Iterable[FileChange]) -> list[str]:
    """Paths a diff touches, relative to the target tree: new and old side of renames."""
    paths: list[str] = []
    for change in changes:
        for path in (change.old_path, change.path):
            if path is None:
                continue
            full = path if project == ROOT_PROJECT else f"{project}/{path}"
            if full not in paths:
                paths.append(full)
    return paths


def widening_files(project: str, changes: Iterable[FileChange]) -> tuple[list[str], list[str]]:
    """Paths (relative to the target tree) whose change reaches other modules, and those changing a Java API."""
    reaching: list[str] = []
    api: list[str] = []
    for change in changes:
        full = change.path if project == ROOT_PROJECT else f"{project}/{change.path}"
        name = PurePosixPath(change.path).name
        java_api = name.endswith(".java") and any(_API_LINE.match(line) for hunk in change.hunks for line in hunk.splitlines()[1:])
        if java_api or name in BUILD_FILES or name.endswith((*HEADER_SUFFIXES, ".aidl")):
            reaching.append(full)
        if java_api or (PurePosixPath(change.path).parent.name == "api" and name.endswith(".txt")):
            api.append(full)
    return reaching, api


def to_build_root(path: str, target_tree: Path, build_root: Path) -> str | None:
    """A target-tree path as a build-root path; None when it is outside the build root."""
    full = Path(os.path.normpath(target_tree / path))
    if not full.is_relative_to(build_root):
        return None
    return full.relative_to(build_root).as_posix()


def to_target_tree(path: str, target_tree: Path, build_root: Path) -> str | None:
    """A build-root path as a target-tree path; None when it is outside the target tree."""
    full = Path(os.path.normpath(build_root / path))
    if not full.is_relative_to(target_tree):
        return None
    return full.relative_to(target_tree).as_posix()


def find_module_info(build_root: Path) -> Path | None:
    """The `module-info.json` of the build root's only product; None for none or several."""
    found = sorted(build_root.glob(MODULE_INFO_GLOB))
    return found[0] if len(found) == 1 else None


@dataclass
class ModuleInfo:
    # Module directories (relative to the build root) to the module names in them.
    by_path: dict[str, set[str]] = field(default_factory=dict)
    # Module name to the names of the modules using it.
    users: dict[str, set[str]] = field(default_factory=dict)


def read_module_info(path: Path) -> ModuleInfo:
    info = ModuleInfo()
    for key, entry in json.loads(path.read_text()).items():
        # Arch variants (`libfoo_32`) carry the name `m` takes in `module_name`.
        name = entry.get("module_name") or key
        for directory in entry.get("path") or []:
            info.by_path.setdefault(directory.strip("/"), set()).add(name)
        for used in {u for f in DEPENDENCY_FIELDS for u in entry.get(f) or []}:
            if used != name:
                info.users.setdefault(used, set()).add(name)
    return info


def affected_modules(
    changed: Iterable[str],
    target_tree: Path,
    build_root: Path,
    reaching: Iterable[str] = (),
    api: Iterable[str] = (),
    dependents_cap: int = DEFAULT_DEPENDENTS_CAP,
) -> AffectedModules:
    """What to build for files changed in the target tree, as the build root sees it.

    `reaching` and `api` are the changed files from `widening_files`: the users of their
    modules are built too (at most `dependents_cap`), and an `api` file adds the API check.
    Both need `module-info.json`: with `mmm` only the changed directories build.
    """
    found_info = find_module_info(build_root)
    info = read_module_info(found_info) if found_info else None
    result = AffectedModules(tool="m" if info is not None else "mmm")
    targets: set[str] = set()
    reaching = set(reaching)
    reached: set[str] = set()
    for path in changed:
        rel = to_build_root(path, target_tree, build_root)
        found = None
        if rel is not None:
            found = _modules_at(rel, info.by_path) if info is not None else _build_dir(rel, build_root)
        if found:
            targets.update(found)
            if path in reaching:
                reached.update(found)
        else:
            result.unbuilt.append(path)
    result.targets = sorted(targets)
    if info is None:
        return result
    users = sorted({user for module in reached for user in info.users.get(module, ())} - targets)
    result.dependents = users[:dependents_cap]
    result.dependents_left_out = len(users) - len(result.dependents)
    result.api_check = bool(list(api))
    result.targets += result.dependents + ([API_CHECK_TARGET] if result.api_check else [])
    return result


def _modules_at(path: str, by_path: dict[str, set[str]]) -> set[str]:
    """Modules of the longest module directory containing `path`."""
    for directory in _parents(path):
        if directory in by_path:
            return by_path[directory]
    return set()


def _build_dir(path: str, build_root: Path) -> set[str]:
    """The nearest directory above `path` with a build file; the build root itself never."""
    for directory in _parents(path):
        if directory != "." and any((build_root / directory / name).is_file() for name in BUILD_FILES):
            return {directory}
    return set()


def _parents(path: str) -> list[str]:
    """Directories holding `path`, innermost first, `.` (the build root) last."""
    return [p.as_posix() for p in PurePosixPath(path).parents]
