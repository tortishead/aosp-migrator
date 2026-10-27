"""Stacked apply check: items' diffs applied on top of each other, in an order the user chose.

One scratch index per project, read from HEAD, with a temp object dir whose alternate is
the real store (see `git.scratch_env`), so the user's tree, index and objects stay
untouched. An item's diffs go into their projects' indexes in turn: `git apply --cached`
when that applies, else `--3way --cached`. When one of them does not, or is unmapped, the
whole item is left out: every index goes back to before it and the items after it stack
without it. Files the caller found already present are left out of a diff, a diff with
nothing left is `present`. An applied diff's stacked patch is its project's index after
it diffed against the index before it, so applying the patches in stack order on HEAD
gives the stack.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from aosp_migrator.prescan import git
from aosp_migrator.prescan.apply import BINARY, NO_HISTORY, merge_patch
from aosp_migrator.prescan.patch import Patch

StackedStatus = Literal["applies", "3way", "conflicts", "present", "unmapped"]

NOT_MAPPED = "not mapped to a target project"


@dataclass
class StackDiff:
    filename: str
    project: str | None
    text: str
    # Files already in the target, left out.
    present: Sequence[str] = ()


@dataclass
class StackedDiff:
    filename: str
    project: str | None
    status: StackedStatus
    error: str | None = None
    # The diff on top of the stack before it; None unless it applies, as is or 3-way.
    patch: str | None = None


@dataclass
class StackedItem:
    applied: bool
    diffs: list[StackedDiff] = field(default_factory=list)


class Stack:
    def __init__(self, tree: Path, scratch: Path) -> None:
        self._tree = tree
        self._scratch = scratch
        self._envs: dict[str, dict[str, str] | None] = {}
        self.heads: dict[str, str | None] = {}

    def add(self, diffs: Sequence[StackDiff]) -> StackedItem:
        """Stack one item's diffs; all of them or, when one does not apply, none."""
        before = {project: self._write_tree(project) for project in {d.project for d in diffs if d.project is not None}}
        results = [self._add(diff) for diff in diffs]
        applied = all(r.status in ("applies", "3way", "present") for r in results)
        if not applied:
            for project, tree in before.items():
                env = self._envs[project]
                if env is not None and tree is not None:
                    git.git(self._tree / project, "read-tree", tree, env=env)
            for result in results:
                result.patch = None
        return StackedItem(applied, results)

    def _add(self, diff: StackDiff) -> StackedDiff:
        def result(status: StackedStatus, error: str | None = None, patch: str | None = None) -> StackedDiff:
            return StackedDiff(diff.filename, diff.project, status, error, patch)

        if diff.project is None:
            return result("unmapped", NOT_MAPPED)
        skip = set(diff.present)
        sections = [s for s in Patch.parse(diff.text).files if s.path not in skip]
        if not sections:
            return result("present")
        if binary := [s.path for s in sections if s.binary]:
            return result("conflicts", f"{binary[0]}: {BINARY}")
        env = self._env(diff.project)
        if env is None:
            return result("conflicts", NO_HISTORY)
        cwd = self._tree / diff.project
        patch = "".join(s.patch for s in sections)
        before = self._write_tree(diff.project)
        assert before is not None
        # `git apply` is all or nothing: a failed plain apply leaves the index as it was.
        if git.apply(cwd, patch, "--cached", env=env).returncode == 0:
            status: StackedStatus = "applies"
        elif (error := merge_patch(cwd, patch, env)) is None:
            status = "3way"
        else:
            # A failed merge may leave conflict entries; the item's other diffs check against a clean index.
            git.git(cwd, "read-tree", before, env=env)
            return result("conflicts", error)
        after = self._write_tree(diff.project)
        assert after is not None
        # Fixed prefixes and no external tools, whatever the user's git config says.
        stacked = git.git(
            cwd, "diff", "--binary", "-M", "--no-color", "--no-ext-diff", "--no-textconv",
            "--src-prefix=a/", "--dst-prefix=b/", before, after, env=env,
        )
        return result(status, patch=stacked.stdout or None)

    def _env(self, project: str) -> dict[str, str] | None:
        """The project's scratch env, made on first use; None without history to stack on."""
        if project not in self._envs:
            cwd = self._tree / project
            head = git.head(cwd) if cwd.is_dir() else None
            self.heads[project] = head
            self._envs[project] = git.scratch_env(cwd, self._scratch / str(len(self._envs))) if head is not None else None
        return self._envs[project]

    def _write_tree(self, project: str) -> str | None:
        env = self._env(project)
        if env is None:
            return None
        return git.git(self._tree / project, "write-tree", env=env).stdout.strip()
