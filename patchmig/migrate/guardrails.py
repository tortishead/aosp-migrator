"""Checks every agent-made change must pass before it is committed.

They run on the worktrees after the agent has finished, and are cheap and deterministic.
A hard failure is sent back to the agent once (as feedback) and then fails the cluster;
a soft finding is recorded and lowers the confidence shown to reviewers.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from fnmatch import fnmatch

from patchmig.config.schema import MigrateConfig, PathRule
from patchmig.migrate.workspace import ProjectWorktree

_MARKER = re.compile(r"^(<{7}( |$)|>{7}( |$)|={7}$|\|{7}( |$))", re.M)


@dataclass
class Change:
    project: str
    status: str  # A, M, D, R..., from git diff --name-status
    path: str
    added: int = 0
    removed: int = 0
    binary: bool = False


@dataclass
class GuardrailReport:
    hard: list[str] = field(default_factory=list)  # must be fixed
    decisions: list[str] = field(default_factory=list)  # needs a human decision
    soft: list[str] = field(default_factory=list)  # recorded for the reviewer
    changed_lines: int = 0
    files: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.hard and not self.decisions

    def to_json(self) -> dict:
        return {
            "ok": self.ok,
            "hard": self.hard,
            "decisions": self.decisions,
            "soft": self.soft,
            "changed_lines": self.changed_lines,
            "files": self.files,
        }


def changes(wt: ProjectWorktree, since: str) -> list[Change]:
    """Changes in the worktree (committed after `since`, staged or not), untracked included."""
    wt.git("add", "-A", "--sparse")
    # -z: paths unquoted (spaces, non-ASCII) and renames as separate old and new paths.
    stats = {}
    fields = wt.git("diff", "--cached", "--numstat", "-M", "-z", since).split("\0")
    i = 0
    while i < len(fields) - 1:
        added, removed, path = fields[i].split("\t", 2)
        if path:
            i += 1
        else:  # a rename: "added<TAB>removed<TAB>" NUL old NUL new
            path, i = fields[i + 2], i + 3
        stats[path] = (added, removed)
    found = []
    fields = wt.git("diff", "--cached", "--name-status", "-M", "-z", since).split("\0")
    i = 0
    while i < len(fields) - 1:
        status = fields[i]
        step = 3 if status[:1] in "RC" else 2
        path = fields[i + step - 1]
        i += step
        added, removed = stats.get(path, ("0", "0"))
        binary = added == "-"
        found.append(
            Change(
                project=wt.project,
                status=status,
                path=path,
                added=0 if binary else int(added),
                removed=0 if binary else int(removed),
                binary=binary,
            )
        )
    return found


def check(
    worktrees: list[ProjectWorktree],
    since: dict[str, str],
    expected_files: dict[str, set[str]],
    expected_deletions: dict[str, set[str]],
    net_diff_lines: int,
    path_rules: list[PathRule],
    limits: MigrateConfig,
) -> GuardrailReport:
    """Check what changed in each worktree since `since[project]`.

    `expected_files` are the target paths the cluster is known to touch (net diff paths,
    mapped through upstream renames); anything else is allowed but noted for review.
    """
    report = GuardrailReport()
    for wt in worktrees:
        for c in changes(wt, since[wt.project]):
            tree_path = f"{wt.project}/{c.path}"
            report.files.append(tree_path)
            report.changed_lines += c.added + c.removed
            expected = c.path in expected_files.get(wt.project, set())
            if c.binary and not expected:  # the patches' own binaries (icons...) may change
                report.hard.append(f"{tree_path}: binary file changed")
            if c.status == "D" and c.path not in expected_deletions.get(wt.project, set()):
                report.hard.append(f"{tree_path}: deleted, but the patches do not delete it")
            if c.status != "D" and not c.binary:
                text = (wt.path / c.path).read_text(errors="replace")
                if _MARKER.search(text):
                    report.hard.append(f"{tree_path}: conflict markers left in the file")
            if not expected:
                report.soft.append(f"{tree_path}: not touched by the original patches")
            for rule in path_rules:
                if not fnmatch(tree_path, rule.pattern):
                    continue
                if rule.rule == "forbidden":
                    report.hard.append(f"{tree_path}: forbidden by path rule ({rule.reason})")
                elif rule.rule == "needs_decision":
                    report.decisions.append(f"{tree_path}: {rule.reason}")
                else:
                    report.soft.append(f"{tree_path}: {rule.rule} ({rule.reason})")
    allowed = max(limits.min_change_lines, int(net_diff_lines * limits.max_change_ratio))
    if report.changed_lines > allowed:
        report.hard.append(
            f"change too large: {report.changed_lines} lines changed, at most {allowed} allowed "
            f"({limits.max_change_ratio}x the net diff of {net_diff_lines} lines)"
        )
    if not report.files:
        report.hard.append("no file was changed")
    return report


def diff_lines(diff: str) -> int:
    return sum(
        1
        for line in diff.splitlines()
        if line[:1] in "+-" and not line.startswith(("+++ ", "--- "))
    )
