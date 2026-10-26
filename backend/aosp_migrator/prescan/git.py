"""Read-only git calls into user trees. Nothing here may fetch, write refs or touch the index."""

import os
import subprocess
from pathlib import Path

# No lazy blob fetches in partial clones, no credential prompts, no opportunistic
# index refreshes (git status and friends would otherwise rewrite .git/index).
_ENV = {
    "GIT_NO_LAZY_FETCH": "1",
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_OPTIONAL_LOCKS": "0",
    "LC_ALL": "C",
}
TIMEOUT = 300


class GitError(Exception):
    pass


def git(cwd: Path, *args: str, input: str | None = None, check: bool = True) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=cwd,
            input=input,
            env={**os.environ, **_ENV},
            capture_output=True,
            text=True,
            errors="replace",
            timeout=TIMEOUT,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise GitError(f"git {args[0]}: {exc}") from exc
    if check and result.returncode != 0:
        raise GitError(f"git {args[0]} failed: {result.stderr.strip()}")
    return result


def head(cwd: Path) -> str | None:
    result = git(cwd, "rev-parse", "--verify", "-q", "HEAD", check=False)
    return result.stdout.strip() or None


def is_shallow(cwd: Path) -> bool:
    return git(cwd, "rev-parse", "--is-shallow-repository").stdout.strip() == "true"
