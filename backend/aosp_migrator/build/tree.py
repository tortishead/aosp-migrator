"""Applying a unit's patches to the user's target tree for a build, and taking them back out.

The only writes to user trees. Per unit: `prepare` refuses a project with tracked
changes, a HEAD other than the patch base, or an untracked file where a patch adds
one, before anything is touched, and returns the in-flight record (HEADs, added files,
created directories). The caller stores that record, then `apply_patches`, builds, and
`restore`s: `git reset --hard <recorded HEAD>` in every project, then the added files
and the directories made for them are deleted. Never `git clean`: the user's own
untracked files stay. `restore` works from a stored record alone, so a unit left half
applied by a crash is restored the same way.
"""

from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from aosp_migrator.importer.diffparse import parse_diff
from aosp_migrator.prescan import git


class TreeRefused(Exception):
    """The unit cannot be applied safely; nothing was touched."""


class ApplyFailed(Exception):
    """A patch did not apply; the tree is partly applied and must be restored."""


@dataclass
class UnitPatch:
    project: str  # Relative to the target tree
    diff: str
    base_head: str | None


@dataclass
class InFlight:
    """What `restore` needs to put the tree back; stored before the first apply."""

    heads: dict[str, str] = field(default_factory=dict)  # Project to its HEAD before the unit
    # Files the patches add and directories made for them, per project, relative to it.
    added_files: dict[str, list[str]] = field(default_factory=dict)
    added_dirs: dict[str, list[str]] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "InFlight":
        return cls(**data)


def prepare(tree: Path, patches: Sequence[UnitPatch]) -> InFlight:
    """Check every affected project and record its state; raise `TreeRefused` if unsafe."""
    inflight = InFlight()
    for patch in patches:
        cwd = tree / patch.project
        if patch.project not in inflight.heads:
            inflight.heads[patch.project] = _check_project(cwd, patch.project)
            inflight.added_files[patch.project] = []
            inflight.added_dirs[patch.project] = []
        if patch.base_head != inflight.heads[patch.project]:
            raise TreeRefused(f"HEAD moved: {patch.project}")
        files, dirs = inflight.added_files[patch.project], inflight.added_dirs[patch.project]
        for path in _added(patch.diff):
            if path in files:
                continue
            if (cwd / path).exists():
                raise TreeRefused(f"project dirty: {patch.project} (untracked {path})")
            files.append(path)
            for parent in reversed(PurePosixPath(path).parents):
                if str(parent) != "." and not (cwd / parent).exists() and str(parent) not in dirs:
                    dirs.append(str(parent))
    return inflight


def apply_patches(tree: Path, patches: Sequence[UnitPatch], three_way: bool = False) -> None:
    """`git apply` each patch into its project's working tree, in order; `--3way` for clusters."""
    for patch in patches:
        options = ("--3way",) if three_way else ()
        result = git.apply(tree / patch.project, patch.diff, *options)
        if result.returncode != 0:
            raise ApplyFailed(f"{patch.project}: {result.stderr.strip()}")


def restore(tree: Path, inflight: InFlight) -> None:
    """Reset every recorded project to its recorded HEAD and delete what the patches added."""
    for project, head in inflight.heads.items():
        cwd = tree / project
        git.git(cwd, "reset", "-q", "--hard", head)
        for path in inflight.added_files.get(project, []):
            (cwd / path).unlink(missing_ok=True)
        # Innermost first; a directory the user has since put files in stays.
        for path in sorted(inflight.added_dirs.get(project, []), key=lambda p: -p.count("/")):
            directory = cwd / path
            if directory.is_dir() and not any(directory.iterdir()):
                directory.rmdir()


def _check_project(cwd: Path, project: str) -> str:
    """The project's HEAD; refused when it is no git project or has tracked changes."""
    head = git.head(cwd) if cwd.is_dir() else None
    if head is None:
        raise TreeRefused(f"not a git project: {project}")
    if git.git(cwd, "status", "--porcelain", "--untracked-files=no").stdout.strip():
        raise TreeRefused(f"project dirty: {project}")
    return head


def _added(diff: str) -> list[str]:
    """Files a patch creates: added files and rename / copy targets."""
    return [change.path for change in parse_diff(diff).files if change.status in ("added", "renamed", "copied")]
