"""Thin helpers over the git CLI."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path


class GitError(Exception):
    def __init__(self, args: list[str], cwd: Path | None, stderr: str):
        self.args_ = args
        self.stderr = stderr.strip()
        super().__init__(f"git {' '.join(args)} failed in {cwd}: {self.stderr}")


def git(repo: Path | None, *args: str, input: str | None = None) -> str:
    cmd = ["git", *args]
    result = subprocess.run(cmd, cwd=repo, input=input, capture_output=True, text=True)
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


def commit_info(repo: Path, sha: str) -> CommitInfo:
    """Metadata and full diff (rename-aware) of one commit."""
    fmt = f"%H{_SEP}%an <%ae>{_SEP}%at{_SEP}%B{_SEP}"
    out = git(repo, "show", "-M", "--no-color", "--no-ext-diff", f"--format={fmt}", sha)
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


def grep_log(repo: Path, base: str, head: str, pattern: str) -> list[str]:
    """Non-merge commits in base..head whose message matches pattern (fixed string)."""
    out = git(
        repo, "log", "--reverse", "--no-merges", "--format=%H", "-F", f"--grep={pattern}",
        f"{base}..{head}",
    )  # fmt: skip
    return out.split()
