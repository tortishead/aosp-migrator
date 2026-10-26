"""Affected modules: what to build for the files a unit changes.

With exactly one `out/target/product/*/module-info.json` under the build root, each
changed file maps to the module(s) with the longest `path` containing it, built with
`m`. With none or several, each file falls back to the nearest directory above it with
an `Android.bp` / `Android.mk`, built with `mmm`. A file under no module builds nothing.

Changed files are relative to the target tree (`<project>/<path in project>`);
`module-info.json` paths are relative to the build root. The two differ when the
build root is a `.repo` root above the target tree (or a tree below it).
Read-only: nothing here runs a build.
"""

import json
import os
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Literal

from aosp_migrator.importer.diffparse import FileChange
from aosp_migrator.prescan.mapping import ROOT_PROJECT

MODULE_INFO_GLOB = "out/target/product/*/module-info.json"
BUILD_FILES = ("Android.bp", "Android.mk")

Tool = Literal["m", "mmm"]


@dataclass
class AffectedModules:
    tool: Tool
    # Module names for `m`, directories relative to the build root for `mmm`; sorted.
    targets: list[str] = field(default_factory=list)
    # Changed files (relative to the target tree) under no module, or outside the build root.
    unbuilt: list[str] = field(default_factory=list)

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


def read_module_info(path: Path) -> dict[str, set[str]]:
    """Module directories (relative to the build root) to the module names in them."""
    by_path: dict[str, set[str]] = {}
    for key, info in json.loads(path.read_text()).items():
        # Arch variants (`libfoo_32`) carry the name `m` takes in `module_name`.
        name = info.get("module_name") or key
        for directory in info.get("path") or []:
            by_path.setdefault(directory.strip("/"), set()).add(name)
    return by_path


def affected_modules(changed: Iterable[str], target_tree: Path, build_root: Path) -> AffectedModules:
    """What to build for files changed in the target tree, as the build root sees it."""
    info = find_module_info(build_root)
    by_path = read_module_info(info) if info else None
    result = AffectedModules(tool="m" if by_path is not None else "mmm")
    targets: set[str] = set()
    for path in changed:
        rel = to_build_root(path, target_tree, build_root)
        found = None
        if rel is not None:
            found = _modules_at(rel, by_path) if by_path is not None else _build_dir(rel, build_root)
        if found:
            targets.update(found)
        else:
            result.unbuilt.append(path)
    result.targets = sorted(targets)
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
