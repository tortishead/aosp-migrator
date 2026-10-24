"""Thin helpers over the git CLI."""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path


class GitError(Exception):
    def __init__(self, args: list[str], cwd: Path | None, stderr: str):
        self.args_ = args
        self.stderr = stderr.strip()
        super().__init__(f"git {' '.join(args)} failed in {cwd}: {self.stderr}")


def git(
    repo: Path | None, *args: str, input: str | None = None, env: dict[str, str] | None = None
) -> str:
    cmd = ["git", *args]
    full_env = {**os.environ, **env} if env else None
    result = subprocess.run(
        cmd, cwd=repo, input=input, capture_output=True, text=True, env=full_env
    )
    if result.returncode != 0:
        raise GitError(list(args), repo, result.stderr)
    return result.stdout


def is_repo(path: Path) -> bool:
    return (path / ".git").exists()


def rev_list(repo: Path, base: str, head: str) -> list[str]:
    """Non-merge commits in base..head, oldest first."""
    out = git(repo, "rev-list", "--reverse", "--topo-order", "--no-merges", f"{base}..{head}")
    return out.split()


def resolve(repo: Path, rev: str) -> str | None:
    try:
        return git(repo, "rev-parse", "--verify", "--quiet", f"{rev}^{{commit}}").strip()
    except GitError:
        return None


@dataclass(frozen=True)
class CommitInfo:
    sha: str
    author: str
    authored_at: datetime
    message: str
    diff: str

    @property
    def subject(self) -> str:
        return self.message.split("\n", 1)[0]


_SEP = "\x1e"


def commit_info(repo: Path, sha: str, binary: bool = False) -> CommitInfo:
    """Metadata and full diff (rename-aware) of one commit.

    The diff is text only ("Binary files ... differ") unless `binary`, which includes binary
    patches so the diff can be applied; keep those out of briefs and displays.
    """
    fmt = f"%H{_SEP}%an <%ae>{_SEP}%at{_SEP}%B{_SEP}"
    extra = ["--binary"] if binary else []
    out = git(repo, "show", "-M", *extra, "--no-color", "--no-ext-diff", f"--format={fmt}", sha)
    full_sha, author, ts, message, diff = out.split(_SEP, 4)
    return CommitInfo(
        sha=full_sha,
        author=author,
        authored_at=datetime.fromtimestamp(int(ts), UTC),
        message=message.strip(),
        diff=diff.lstrip("\n"),
    )


def patch_id(diff: str) -> str | None:
    """Stable git patch-id of a diff: equal for the same change regardless of base or message."""
    if not diff.strip():
        return None
    out = git(None, "patch-id", "--stable", input=diff)
    return out.split()[0] if out.strip() else None


def range_log(repo: Path, base: str, head: str) -> list[tuple[str, str]]:
    """(sha, message) of the non-merge commits in base..head, oldest first (as rev_list)."""
    out = git(
        repo, "log", "--reverse", "--topo-order", "--no-merges", f"--format=%H%n%B{_SEP}",
        f"{base}..{head}",
    )  # fmt: skip
    commits = []
    for entry in out.split(_SEP):
        sha, _, message = entry.strip("\n").partition("\n")
        if sha:
            commits.append((sha, message))
    return commits


def has_binary(diff: str) -> bool:
    """Whether a text diff leaves out binary content ("Binary files a/x and b/x differ")."""
    return diff.startswith("Binary files ") or "\nBinary files " in diff


def is_ancestor(repo: Path, ancestor: str, rev: str) -> bool:
    result = subprocess.run(
        ["git", "merge-base", "--is-ancestor", ancestor, rev], cwd=repo, capture_output=True
    )
    return result.returncode == 0


def file_at(repo: Path, rev: str, path: str) -> str | None:
    """Content of a file at a revision, or None if it doesn't exist there."""
    result = subprocess.run(
        ["git", "show", f"{rev}:{path}"], cwd=repo, capture_output=True, text=True,
        errors="replace",
    )  # fmt: skip
    return result.stdout if result.returncode == 0 else None


def ls_files(repo: Path, rev: str) -> list[str]:
    return git(repo, "ls-tree", "-r", "--name-only", rev).splitlines()


def name_status(repo: Path, old: str, new: str) -> list[tuple[str, str, str]]:
    """Rename-aware (status, old path, new path) of every file changed between two revs."""
    # -l raises the rename-detection limit: A14 -> A15 diffs of big projects exceed the default.
    out = git(repo, "diff", "-M", "-l5000", "--no-color", "--name-status", "-z", old, new)
    fields = out.split("\0")
    changes: list[tuple[str, str, str]] = []
    i = 0
    while i < len(fields) - 1:
        status = fields[i]
        if status[:1] in "RC":
            changes.append((status[0], fields[i + 1], fields[i + 2]))
            i += 3
        else:
            changes.append((status[0], fields[i + 1], fields[i + 1]))
            i += 2
    return changes
