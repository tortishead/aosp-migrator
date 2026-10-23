"""Net-diff folding: replay a cluster's chain on the source base and diff base to tip.

The replay runs in a temporary index (no checkout), so it is cheap even for large projects.
Fixes then show up merged into the change they fix, and revert pairs cancel out.
"""

from __future__ import annotations

import re
import tempfile
from pathlib import Path

from patchmig.gitutil import GitError, git
from patchmig.knowledge.trees import Trees

_PATH_LINE = re.compile(r"^(diff --git a/|--- a/|\+\+\+ b/|rename from |rename to )")


def fold(trees: Trees, project: str, diffs: list[str]) -> tuple[str | None, str | None]:
    """Return (net diff, error). The net diff is project-relative and may be empty."""
    repo, base = trees.source_repo(project), trees.source_base(project)
    if base is None:
        return None, f"{project}: source base {trees.source.base_ref} not found"
    with tempfile.TemporaryDirectory(prefix="patchmig-fold-") as tmp:
        env = {"GIT_INDEX_FILE": str(Path(tmp) / "index")}
        try:
            git(repo, "read-tree", base, env=env)
            for i, diff in enumerate(diffs, 1):
                if not diff.strip():
                    continue
                try:
                    git(repo, "apply", "--cached", "-", input=diff, env=env)
                except GitError as e:
                    return None, (
                        f"{project}: patch {i} of {len(diffs)} does not apply on the base "
                        f"together with the earlier ones (it likely depends on a patch outside "
                        f"this cluster): {e.stderr.splitlines()[-1] if e.stderr else e}"
                    )
            tree = git(repo, "write-tree", env=env).strip()
            return git(repo, "diff", "-M", "--no-color", "--no-ext-diff", base, tree), None
        except GitError as e:
            return None, f"{project}: {e}"


def tree_relative(diff: str, project: str) -> str:
    """Prefix a project-relative diff's paths with the project, for a whole-tree view."""
    prefix = project.rstrip("/") + "/"
    out = []
    for line in diff.splitlines(keepends=True):
        if m := _PATH_LINE.match(line):
            rest = line[m.end() :]
            if line.startswith("diff --git"):
                rest = rest.replace(" b/", f" b/{prefix}", 1)
            line = m.group(1) + prefix + rest
        out.append(line)
    return "".join(out)
