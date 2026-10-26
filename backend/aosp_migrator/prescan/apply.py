"""Apply check: does a diff apply to the mapped target project as is, or with a 3-way merge?

Each file is checked on its own, with its sections from every commit of a series (as in
`presence`). First `git apply --check` against the working tree: `applies`. A file that
does not apply is merged with `git apply --3way --cached` against HEAD, in a temp index
(`GIT_INDEX_FILE`) and a temp object directory whose alternate is the real object store,
so neither the user's index nor their objects change: a clean merge gives `3way`.
Anything else is `conflicts`: a real conflict, a missing preimage blob (common for
OEM-only files), a binary section without data, or a plain tree without history.
Files the presence check found present are left out: they would only conflict.
The diff takes the worst file result.
"""

import tempfile
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from aosp_migrator.prescan.git import git, head
from aosp_migrator.prescan.presence import file_sections, is_binary, split_commits, strip_option

ApplyStatus = Literal["applies", "3way", "conflicts"]

NO_HISTORY = "no git history for a 3-way merge"
MISSING_BLOB = "preimage blob not in the target history"
MERGE_CONFLICT = "3-way merge conflicts"
BINARY = "binary file without patch data"

_ORDER: list[ApplyStatus] = ["applies", "3way", "conflicts"]


@dataclass
class ApplyCheck:
    status: ApplyStatus
    # files: {path: applies / 3way / conflicts}; errors: {path: why} for each conflicting file.
    detail: dict[str, Any] = field(default_factory=dict)


def check_apply(project_dir: Path, diff_text: str, present: Iterable[str] = ()) -> ApplyCheck:
    """Check each file of `diff_text` but those in `present`."""
    sections = file_sections(split_commits(diff_text))
    skip = set(present)
    files: dict[str, ApplyStatus] = {}
    errors: dict[str, str] = {}
    failed: dict[str, str] = {}
    for changes in sections.values():
        path = changes[-1].path
        if path in skip:
            continue
        if is_binary(changes):
            files[path], errors[path] = "conflicts", BINARY
            continue
        patch = "".join(c.raw for c in changes)
        if git(project_dir, "apply", "--check", strip_option(patch), "-", input=patch, check=False).returncode == 0:
            files[path] = "applies"
        else:
            failed[path] = patch
    if failed:
        merged = _three_way(project_dir, failed)
        for path, error in merged.items():
            files[path] = "conflicts" if error else "3way"
            if error:
                errors[path] = error
    detail: dict[str, Any] = {"files": files}
    if errors:
        detail["errors"] = errors
    status = max(files.values(), key=_ORDER.index, default="applies")
    return ApplyCheck(status, detail)


def _three_way(project_dir: Path, patches: dict[str, str]) -> dict[str, str | None]:
    """3-way merge each file's patch onto HEAD in a temp index and object dir: {path: None when clean, else why}."""
    if head(project_dir) is None:
        return dict.fromkeys(patches, NO_HISTORY)
    objects = Path(git(project_dir, "rev-parse", "--git-path", "objects").stdout.strip())
    objects = (objects if objects.is_absolute() else project_dir / objects).resolve()
    with tempfile.TemporaryDirectory(prefix="aosp-migrator-apply-") as tmp:
        env = {
            "GIT_INDEX_FILE": str(Path(tmp) / "index"),
            "GIT_OBJECT_DIRECTORY": str(Path(tmp) / "objects"),
            "GIT_ALTERNATE_OBJECT_DIRECTORIES": str(objects),
        }
        (Path(tmp) / "objects").mkdir()
        git(project_dir, "read-tree", "HEAD", env=env)
        # One index for all files: each merge only touches its own entries.
        return {path: _merge(project_dir, patch, env) for path, patch in patches.items()}


def _merge(project_dir: Path, patch: str, env: dict[str, str]) -> str | None:
    result = git(project_dir, "apply", "--3way", "--cached", strip_option(patch), "-", input=patch, check=False, env=env)
    if result.returncode == 0:
        return None
    if "lacks the necessary blob" in result.stderr:
        return MISSING_BLOB
    if "with conflicts" in result.stderr:
        return MERGE_CONFLICT
    errors = [line.removeprefix("error: ") for line in result.stderr.splitlines() if line.startswith(("error: ", "fatal: "))]
    return errors[0] if errors else MERGE_CONFLICT
