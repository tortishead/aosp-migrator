"""Per-cluster workspaces: one sparse git worktree per target project, on its own branch.

Layout: <workdir>/worktrees/run-<run>/cluster-<id>/<project path>/, so an agent started in
the cluster folder sees a small tree shaped like the real one. Worktrees are sparse (only the
directories the cluster touches) because a full checkout of a large project per cluster would
cost gigabytes. Agents read anything else through the MCP tools.

The worktrees and branches live in the target tree's repositories (as any `git worktree`
does): branches are named patchmig/r<run>/c<cluster>. The source commits are fetched into
each target repository under refs/patchmig/source-head so they can be cherry-picked.
"""

from __future__ import annotations

import posixpath
import shutil
from dataclasses import dataclass
from pathlib import Path

from patchmig.gitutil import GitError, git, resolve
from patchmig.knowledge.trees import Trees

SOURCE_REF = "refs/patchmig/source-head"
# Committer of every migrated commit; cherry-picks keep the original author.
IDENTITY = {
    "GIT_AUTHOR_NAME": "patchmig",
    "GIT_AUTHOR_EMAIL": "patchmig@localhost",
    "GIT_COMMITTER_NAME": "patchmig",
    "GIT_COMMITTER_EMAIL": "patchmig@localhost",
}


class WorkspaceError(Exception):
    pass


@dataclass
class ProjectWorktree:
    project: str
    repo: Path  # the target project's repository
    path: Path  # the worktree
    branch: str
    base: str  # commit the branch started from

    def git(self, *args: str, input: str | None = None) -> str:
        return git(self.path, *args, input=input, env=IDENTITY)

    def head(self) -> str:
        return self.git("rev-parse", "HEAD").strip()


def branch_name(run_id: int, cluster_id: int) -> str:
    return f"patchmig/r{run_id}/c{cluster_id}"


class Workspaces:
    def __init__(self, trees: Trees, root: Path):
        self.trees = trees
        self.root = root
        self._fetched: set[str] = set()

    def cluster_dir(self, run_id: int, cluster_id: int) -> Path:
        return self.root / f"run-{run_id}" / f"cluster-{cluster_id}"

    def _fetch_source(self, project: str, repo: Path) -> None:
        if project in self._fetched:
            return
        source = self.trees.source_repo(project)
        head = resolve(source, self.trees.source.head_ref or "HEAD")
        if head is None:
            raise WorkspaceError(f"{project}: source head {self.trees.source.head_ref} not found")
        git(repo, "fetch", "-q", "--no-tags", str(source.resolve()), f"+{head}:{SOURCE_REF}")
        self._fetched.add(project)

    def remove(self, run_id: int, cluster_id: int, projects: list[str]) -> None:
        """Drop a cluster's worktrees and branches (before a retry)."""
        branch = branch_name(run_id, cluster_id)
        folder = self.cluster_dir(run_id, cluster_id)
        for project in projects:
            repo = self.trees.target_repo(project)
            if repo is None:
                continue
            path = folder / project
            if path.exists():
                try:
                    git(repo, "worktree", "remove", "--force", str(path))
                except GitError:
                    shutil.rmtree(path, ignore_errors=True)
            git(repo, "worktree", "prune")
            if resolve(repo, f"refs/heads/{branch}"):
                git(repo, "branch", "-D", branch)
        shutil.rmtree(folder, ignore_errors=True)

    def create(
        self,
        run_id: int,
        cluster_id: int,
        project: str,
        dirs: set[str],
        base: str | None = None,
    ) -> ProjectWorktree:
        """A sparse worktree of `project` with `dirs` checked out, on a new cluster branch.

        `base` defaults to the target base; a cluster that depends on another one starts
        from that cluster's branch instead.
        """
        repo = self.trees.target_repo(project)
        start = base or self.trees.target_base(project)
        if repo is None or start is None:
            raise WorkspaceError(f"{project}: no such project or base in the target tree")
        self._fetch_source(project, repo)
        path = self.cluster_dir(run_id, cluster_id) / project
        if path.exists():
            raise WorkspaceError(f"{path} already exists; remove the cluster's workspace first")
        path.parent.mkdir(parents=True, exist_ok=True)
        branch = branch_name(run_id, cluster_id)
        git(repo, "worktree", "add", "-q", "--no-checkout", "-b", branch, str(path), start)
        wt = ProjectWorktree(project, repo, path, branch, start)
        # No directories (only top-level files touched): the cone holds just the root files.
        wt.git("sparse-checkout", "set", "--cone", *sorted(d for d in dirs if d))
        wt.git("checkout", "-q")
        return wt


def dirs_for(paths: list[str]) -> set[str]:
    """Parent directories to check out for these project-relative paths."""
    return {posixpath.dirname(p) for p in paths}
