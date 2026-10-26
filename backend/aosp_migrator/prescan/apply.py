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

The migration patch comes with it: for `applies`, the sections of the checked files as
written; for `3way`, the merged result, i.e. the temp index diffed against HEAD after
every checked file went in with `--3way --cached`. `conflicts` gives none.
"""

import tempfile
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from aosp_migrator.prescan import git
from aosp_migrator.prescan.patch import Patch

ApplyStatus = Literal["applies", "3way", "conflicts"]

# Worst first.
ORDER: list[ApplyStatus] = ["conflicts", "3way", "applies"]

NO_HISTORY = "no git history for a 3-way merge"
MISSING_BLOB = "preimage blob not in the target history"
MERGE_CONFLICT = "3-way merge conflicts"
BINARY = "binary file without patch data"


def worst(statuses: Iterable[ApplyStatus]) -> ApplyStatus | None:
    return min(statuses, key=ORDER.index, default=None)


@dataclass
class ApplyCheck:
    status: ApplyStatus
    # files: {path: applies / 3way / conflicts}; errors: {path: why} for each conflicting file.
    detail: dict[str, Any] = field(default_factory=dict)
    # Migration patch for `applies` / `3way`, None for `conflicts`; left out of comparisons.
    patch: str | None = field(default=None, compare=False)


def check_apply(project_dir: Path, diff_text: str, present: Iterable[str] = ()) -> ApplyCheck:
    """Check each file of `diff_text` but those in `present`."""
    skip = set(present)
    files: dict[str, ApplyStatus] = {}
    errors: dict[str, str] = {}
    checked: dict[str, str] = {}
    failed: dict[str, str] = {}
    for section in Patch.parse(diff_text).files:
        path = section.path
        if path in skip:
            continue
        if section.binary:
            files[path], errors[path] = "conflicts", BINARY
            continue
        checked[path] = section.patch
        if git.apply(project_dir, section.patch, "--check").returncode == 0:
            files[path] = "applies"
        else:
            failed[path] = section.patch
    merged = _three_way(project_dir, failed, checked if not errors else {})
    for path, error in merged.errors.items():
        files[path] = "conflicts" if error else "3way"
        if error:
            errors[path] = error
    detail: dict[str, Any] = {"files": files}
    if errors:
        detail["errors"] = errors
    status = worst(files.values()) or "applies"
    patch = {"applies": "".join(checked.values()) or None, "3way": merged.patch, "conflicts": None}[status]
    return ApplyCheck(status, detail, patch)


@dataclass
class _Merge:
    # {path: None when clean, else why}
    errors: dict[str, str | None]
    # The merged result against HEAD when every file merged cleanly and a result was asked for.
    patch: str | None = None


def _three_way(project_dir: Path, patches: dict[str, str], result: dict[str, str]) -> _Merge:
    """3-way merge each file's patch onto HEAD in a temp index and object dir.

    When all of them merge cleanly, `result` (every checked file's patch) goes into a fresh
    temp index the same way, and its diff against HEAD is the merged patch.
    """
    if not patches:
        return _Merge({})
    if git.head(project_dir) is None:
        return _Merge(dict.fromkeys(patches, NO_HISTORY))
    objects = Path(git.git(project_dir, "rev-parse", "--git-path", "objects").stdout.strip())
    objects = (objects if objects.is_absolute() else project_dir / objects).resolve()
    with tempfile.TemporaryDirectory(prefix="aosp-migrator-apply-") as tmp:
        env = {
            "GIT_INDEX_FILE": str(Path(tmp) / "index"),
            "GIT_OBJECT_DIRECTORY": str(Path(tmp) / "objects"),
            "GIT_ALTERNATE_OBJECT_DIRECTORIES": str(objects),
        }
        (Path(tmp) / "objects").mkdir()
        git.git(project_dir, "read-tree", "HEAD", env=env)
        # One index for all files: each merge only touches its own entries.
        merge = _Merge({path: _merge(project_dir, patch, env) for path, patch in patches.items()})
        if result and not any(merge.errors.values()):
            merge.patch = _merged_patch(project_dir, result, env)
        return merge


def _merged_patch(project_dir: Path, patches: dict[str, str], env: dict[str, str]) -> str | None:
    """Every patch merged into a fresh temp index, diffed against HEAD; None if one no longer merges."""
    git.git(project_dir, "read-tree", "HEAD", env=env)
    for patch in patches.values():
        if _merge(project_dir, patch, env) is not None:
            return None
    # Fixed prefixes and no external tools, whatever the user's git config says.
    diff = git.git(
        project_dir, "diff", "--cached", "--binary", "-M", "--no-color", "--no-ext-diff", "--no-textconv",
        "--src-prefix=a/", "--dst-prefix=b/", "HEAD", env=env,
    )
    return diff.stdout or None


def _merge(project_dir: Path, patch: str, env: dict[str, str]) -> str | None:
    result = git.apply(project_dir, patch, "--3way", "--cached", env=env)
    if result.returncode == 0:
        return None
    if "lacks the necessary blob" in result.stderr:
        return MISSING_BLOB
    if "with conflicts" in result.stderr:
        return MERGE_CONFLICT
    errors = [line.removeprefix("error: ") for line in result.stderr.splitlines() if line.startswith(("error: ", "fatal: "))]
    return errors[0] if errors else MERGE_CONFLICT
