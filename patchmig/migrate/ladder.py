"""The deterministic rungs of the migration ladder, per project worktree.

1. cherry-pick: replay the member commits in order (`-x`, authors kept). Rename-aware, so a
   file that moved upstream still applies.
2. net diff: a 3-way apply of the cluster's folded net diff. When this conflicts, the
   conflict markers are left in the worktree as the starting point for the cluster agent.

Neither rung costs credits. Both leave the worktree clean on success (committed) and reset
to the branch base when they cannot help.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass, field

from patchmig.gitutil import GitError
from patchmig.migrate.workspace import ProjectWorktree


@dataclass
class RungResult:
    ok: bool
    strategy: str  # cherry_pick, net_diff
    commits: list[str] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)  # files left with conflict markers
    skipped_empty: list[str] = field(default_factory=list)  # commits already in the target
    error: str = ""

    def to_json(self) -> dict:
        return {
            "ok": self.ok,
            "strategy": self.strategy,
            "commits": self.commits,
            "conflicts": self.conflicts,
            "skipped_empty": self.skipped_empty,
            "error": self.error,
        }


def unmerged(wt: ProjectWorktree) -> list[str]:
    out = wt.git("diff", "--name-only", "--diff-filter=U", "-z")
    return sorted({p for p in out.split("\0") if p})


def reset(wt: ProjectWorktree, rev: str | None = None) -> None:
    for op in ("cherry-pick", "am"):
        with contextlib.suppress(GitError):
            wt.git(op, "--abort")
    # Unstage first: a staged new file outside the sparse cone would block a hard reset.
    wt.git("reset", "-q", rev or wt.base)
    wt.git("reset", "-q", "--hard", rev or wt.base)
    wt.git("clean", "-q", "-fd")


def _last_line(e: GitError) -> str:
    text = e.stderr or str(e)
    lines = [ln for ln in text.splitlines() if ln.strip() and not ln.startswith("hint:")]
    return lines[-1] if lines else str(e)


def cherry_pick(wt: ProjectWorktree, shas: list[str]) -> RungResult:
    start = wt.head()
    result = RungResult(ok=False, strategy="cherry_pick")
    for sha in shas:
        try:
            wt.git("cherry-pick", "-x", "--allow-empty-message", sha)
        except GitError as e:
            conflicts = unmerged(wt)
            if not conflicts and "empty" in (e.stderr or ""):
                # The change is already in the target: nothing to commit for this patch.
                wt.git("cherry-pick", "--skip")
                result.skipped_empty.append(sha)
                continue
            reset(wt, start)
            result.conflicts = conflicts
            result.error = f"{sha[:12]}: {_last_line(e)}"
            return result
        result.commits.append(wt.head())
    result.ok = True
    return result


def apply_net_diff(wt: ProjectWorktree, diff: str) -> RungResult:
    """3-way apply of the net diff. On conflict, keep the markers (and return ok=False)."""
    result = RungResult(ok=False, strategy="net_diff")
    if not diff.strip():
        result.ok = True
        return result
    try:
        wt.git("apply", "--3way", "--index", "--whitespace=nowarn", "-", input=diff)
    except GitError as e:
        result.conflicts = unmerged(wt)
        result.error = _last_line(e)
        if not result.conflicts:
            reset(wt)  # nothing usable was applied
        return result
    result.ok = True
    return result


def commit(wt: ProjectWorktree, message: str, author: str | None = None) -> str | None:
    """Stage everything in the worktree and commit; None if there is nothing to commit."""
    wt.git("add", "-A", "--sparse")
    if not wt.git("diff", "--cached", "--name-only").strip():
        return None
    args = ["commit", "-q", "--no-verify", "-F", "-"]
    if author:
        args += ["--author", author]
    wt.git(*args, input=message)
    return wt.head()
