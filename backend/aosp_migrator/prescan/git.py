"""Read-only git calls into user trees. Nothing here may fetch, write refs or touch the index.

A scratch env (`scratch_env`) points git at a temp index and a temp object dir with the
project's objects as alternate: writes go there, never into the project.
"""

import os
import subprocess
from pathlib import Path

from aosp_migrator.prescan.patch import strip_option

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


def git(cwd: Path, *args: str, input: str | None = None, check: bool = True, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=cwd,
            input=input,
            # Never discover a repository above `cwd`: a `plain` tree inside some checkout
            # would otherwise apply and log against that checkout.
            env={**os.environ, **_ENV, "GIT_CEILING_DIRECTORIES": str(cwd.resolve().parent), **(env or {})},
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
    # The `shallow` file, not `--is-shallow-repository`: git before 2.15 echoes that flag back.
    path = Path(git(cwd, "rev-parse", "--git-path", "shallow").stdout.strip())
    return (path if path.is_absolute() else cwd / path).is_file()


def apply(cwd: Path, patch: str, *options: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    """`git apply` of `patch` from stdin with `options`; the caller reads the return code."""
    return git(cwd, "apply", *options, strip_option(patch), "-", input=patch, check=False, env=env)


def scratch_env(cwd: Path, scratch: Path) -> dict[str, str]:
    """Git env with an index of HEAD and an object dir in `scratch`, the project's objects as alternate."""
    objects = Path(git(cwd, "rev-parse", "--git-path", "objects").stdout.strip())
    objects = (objects if objects.is_absolute() else cwd / objects).resolve()
    (scratch / "objects").mkdir(parents=True)
    env = {
        "GIT_INDEX_FILE": str(scratch / "index"),
        "GIT_OBJECT_DIRECTORY": str(scratch / "objects"),
        "GIT_ALTERNATE_OBJECT_DIRECTORIES": str(objects),
    }
    git(cwd, "read-tree", "HEAD", env=env)
    return env
