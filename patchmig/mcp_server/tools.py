"""Read-only tools that agents use to look at the trees, the ledger and Jira.

Plain functions over a ToolContext, so they are tested directly; server.py exposes them to
Copilot CLI over MCP. Trees are read from git refs (source head, target base), never from the
working tree, so agents see exactly the versions being migrated.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from sqlalchemy import select

from patchmig.config import ProjectConfig
from patchmig.gitutil import GitError, git
from patchmig.ingest.manifest import tree_projects
from patchmig.ledger import Cluster, KnowledgeItem, LedgerStore, Patch, Ticket

Tree = Literal["source", "target"]
MAX_LINES = 400
MAX_RESULTS = 100


class ToolError(Exception):
    pass


@dataclass
class ToolContext:
    config: ProjectConfig
    store: LedgerStore
    run_id: int
    _projects: dict[str, list[str]] = field(default_factory=dict, repr=False)

    def tree(self, tree: Tree) -> tuple[Path, str]:
        t = self.config.trees.source if tree == "source" else self.config.trees.target
        ref = t.head_ref if tree == "source" else t.base_ref
        return t.path, ref

    def projects(self, tree: Tree) -> list[str]:
        """The tree's projects, from its manifest (read once: tools look them up per project)."""
        if tree not in self._projects:
            t = self.config.trees.source if tree == "source" else self.config.trees.target
            self._projects[tree] = tree_projects(t.path, t.manifest)
        return self._projects[tree]

    def repo(self, tree: Tree, project: str) -> tuple[Path, str]:
        if project not in self.projects(tree):
            raise ToolError(f"unknown project '{project}' in the {tree} tree")
        root, ref = self.tree(tree)
        return root / project, ref


def get_patch(ctx: ToolContext, patch_id: int) -> dict:
    """A patch of the current run: metadata, links, footprint and intent."""
    with ctx.store.session() as s:
        p = s.get(Patch, patch_id)
    if p is None or p.run_id != ctx.run_id:
        raise ToolError(f"patch {patch_id} is not part of run {ctx.run_id}")
    return {
        "id": p.id,
        "project": p.project,
        "commit": p.commit_sha,
        "subject": p.subject,
        "message": p.message,
        "jira_keys": p.jira_keys,
        "chain_role": p.chain_role,
        "links": p.links,
        "footprint": p.footprint,
        "intent": p.intent,
        "state": p.state.value,
        "verdict": p.verdict,
        "cluster_id": p.cluster_id,
    }


def list_patches(ctx: ToolContext, project: str | None = None) -> list[dict]:
    """All patches of the current run in order (id, project, subject, role, state)."""
    with ctx.store.session() as s:
        q = select(Patch).where(Patch.run_id == ctx.run_id).order_by(Patch.seq)
        if project:
            q = q.where(Patch.project == project)
        rows = s.scalars(q).all()
    return [
        {"id": p.id, "project": p.project, "subject": p.subject, "role": p.chain_role,
         "state": p.state.value}
        for p in rows
    ]  # fmt: skip


def get_ticket(ctx: ToolContext, key: str) -> dict:
    """A cached Jira ticket (summary, description, status, linked issues)."""
    with ctx.store.session() as s:
        t = s.get(Ticket, key)
    if t is None:
        raise ToolError(f"ticket {key} is not in the cache")
    return {"key": t.key, "summary": t.summary, "description": t.description,
            "status": t.status, "issue_links": t.issue_links}  # fmt: skip


def search_code(
    ctx: ToolContext, tree: Tree, pattern: str, project: str | None = None, fixed: bool = False
) -> list[str]:
    """Lines matching a regex (or fixed string) as 'project/path:line: text', at most 100."""
    projects = [project] if project else ctx.projects(tree)
    hits: list[str] = []
    for name in projects:
        repo, ref = ctx.repo(tree, name)
        args = ["grep", "-n", "-I", "-F" if fixed else "-E", "-e", pattern, ref, "--"]
        try:
            out = git(repo, *args)
        except GitError as e:
            if e.stderr:  # exit 1 without stderr = no match
                raise ToolError(e.stderr) from e
            continue
        for line in out.splitlines():
            _, _, rest = line.partition(":")  # drop "<ref>:"
            hits.append(f"{name}/{rest}")
            if len(hits) >= MAX_RESULTS:
                return hits + ["[... more results; narrow the pattern or project]"]
    return hits


def find_files(ctx: ToolContext, tree: Tree, name: str) -> list[str]:
    """Paths whose file name contains `name` (case-sensitive), e.g. after an upstream move."""
    found: list[str] = []
    for project in ctx.projects(tree):
        repo, ref = ctx.repo(tree, project)
        out = git(repo, "ls-tree", "-r", "--name-only", ref)
        found += [f"{project}/{p}" for p in out.splitlines() if name in p.rsplit("/", 1)[-1]]
    return found[:MAX_RESULTS]


def read_file(
    ctx: ToolContext, tree: Tree, project: str, path: str, start: int = 1, end: int | None = None
) -> str:
    """Numbered lines start..end of a file (at most 400 lines per call)."""
    repo, ref = ctx.repo(tree, project)
    try:
        text = git(repo, "show", f"{ref}:{path}")
    except GitError as e:
        raise ToolError(f"{project}/{path} does not exist in the {tree} tree") from e
    lines = text.splitlines()
    start = max(start, 1)
    end = min(end or len(lines), start + MAX_LINES - 1, len(lines))
    body = "\n".join(f"{n:>5}  {lines[n - 1]}" for n in range(start, end + 1))
    if end < len(lines):
        body += f"\n[... {len(lines) - end} more lines]"
    return body


def file_history(
    ctx: ToolContext,
    tree: Tree,
    project: str,
    path: str,
    start: int,
    end: int,
    max_commits: int = 10,
) -> str:
    """How lines start..end of a file evolved (git log -L), newest first."""
    repo, ref = ctx.repo(tree, project)
    try:
        return git(
            repo, "log", f"-{max_commits}", "--format=commit %h %ad %s", "--date=short",
            f"-L{start},{end}:{path}", ref,
        )  # fmt: skip
    except GitError as e:
        raise ToolError(e.stderr) from e


def get_cluster(ctx: ToolContext, cluster_id: int) -> dict:
    """A cluster of the current run: members in order, links, net diff and forecast."""
    with ctx.store.session() as s:
        c = s.get(Cluster, cluster_id)
    if c is None or c.run_id != ctx.run_id:
        raise ToolError(f"cluster {cluster_id} is not part of run {ctx.run_id}")
    r = c.result
    return {
        "id": c.id, "name": c.name, "order": c.order_index, "state": c.state.value,
        "members": r.get("members", []), "edges": r.get("edges", []),
        "forecast": r.get("forecast"), "fold_errors": r.get("fold_errors", []),
        "net_diff": c.net_diff,
    }  # fmt: skip


def upstream_fate(ctx: ToolContext, project: str, path: str) -> dict:
    """What upstream did to a file the OEM patches touch (source-tree project and path).

    Status is unchanged, modified, renamed, moved_project, deleted or added_upstream, with
    the new location, upstream commits, changed line ranges and changed method signatures.
    A hypothesis from the knowledge pack: confirm it in the target code before relying on it.
    """
    with ctx.store.session() as s:
        item = s.scalars(
            select(KnowledgeItem).where(
                KnowledgeItem.run_id == ctx.run_id,
                KnowledgeItem.kind == "file_fate",
                KnowledgeItem.key == f"{project}:{path}",
            )
        ).first()
    if item is None:
        raise ToolError(f"{project}:{path} is not in the knowledge pack (not touched by patches?)")
    return item.data


TOOLS = [
    get_patch, list_patches, get_cluster, get_ticket, upstream_fate, search_code, find_files,
    read_file, file_history,
]  # fmt: skip
