"""Find the projects of a tree: from its repo manifest, or by scanning for git repos."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

from patchmig.gitutil import is_repo


class ManifestError(Exception):
    pass


def _read(path: Path, seen: set[Path]) -> tuple[dict[str, str], set[str]]:
    """Return (path -> name) and names removed via <remove-project>, following <include>."""
    if path in seen:
        raise ManifestError(f"manifest include loop at {path}")
    seen.add(path)
    try:
        root = ET.parse(path).getroot()
    except (OSError, ET.ParseError) as e:
        raise ManifestError(f"cannot read manifest {path}: {e}") from e
    projects: dict[str, str] = {}
    removed: set[str] = set()
    for el in root:
        if el.tag == "include":
            sub, sub_removed = _read(path.parent / el.attrib["name"], seen)
            projects.update(sub)
            removed |= sub_removed
        elif el.tag == "project":
            name = el.attrib["name"]
            projects[el.attrib.get("path", name)] = name
        elif el.tag == "remove-project":
            removed.add(el.attrib["name"])
    return projects, removed


def manifest_projects(manifest: Path) -> list[str]:
    projects, removed = _read(manifest, set())
    return sorted(path for path, name in projects.items() if name not in removed)


def scan_projects(tree: Path, max_depth: int = 4) -> list[str]:
    """Fallback without a manifest: every git repo up to max_depth below the tree root."""
    found: list[str] = []

    def walk(directory: Path, depth: int) -> None:
        if is_repo(directory) and directory != tree:
            found.append(directory.relative_to(tree).as_posix())
            return
        if depth == max_depth:
            return
        for child in sorted(directory.iterdir()):
            if child.is_dir() and not child.name.startswith("."):
                walk(child, depth + 1)

    walk(tree, 0)
    return found


def tree_projects(tree: Path, manifest: str | None) -> list[str]:
    if manifest:
        return manifest_projects(tree / manifest)
    return scan_projects(tree)


def project_for_path(path: str, projects: list[str]) -> str | None:
    """Longest project path that prefixes a tree-relative file path."""
    best = None
    for project in projects:
        if (path == project or path.startswith(project + "/")) and (
            best is None or len(project) > len(best)
        ):
            best = project
    return best
