"""Shallow checkouts: fetch the history the upstream checks need, and no more.

Trees are usually synced with `repo sync --depth=1`: every project holds one commit. That is
enough to build, but not to analyse:

- the source tree needs the OEM range base_ref..head_ref, to list and read the patches;
- the target tree needs its history back to the source base (when the trees share it) or
  `max_commits` commits, so the identity and provenance rungs can find a Google fix that is
  already there (same Change-Id or patch-id, or the commit a backport was picked from), and
  the upstream change map can list what changed in a file.

Without that history the checks do not fail loudly: they find nothing, and a fix Google
already made looks like a patch that is still needed.

`deepen` fetches in growing steps (`git fetch --deepen`) until that history is there, so a
project costs only the commits it needs. The remote is the project's only remote, or the one
named in the tree config. With a `host` and token on the tree, HTTPS fetches sign in with it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from patchmig.config import ProjectConfig
from patchmig.githosts import git_env
from patchmig.gitutil import GitError, git, is_ancestor, is_repo, resolve

FIRST_STEP = 500


@dataclass
class DeepenResult:
    project: str
    side: str  # source or target
    shallow: bool
    commits_before: int = 0
    commits_after: int = 0
    fetches: int = 0
    ok: bool = True
    detail: str = ""
    errors: list[str] = field(default_factory=list)


def is_shallow(repo: Path) -> bool:
    try:
        return git(repo, "rev-parse", "--is-shallow-repository").strip() == "true"
    except GitError:
        return False


def commit_count(repo: Path, rev: str) -> int:
    try:
        return int(git(repo, "rev-list", "--count", rev).strip())
    except (GitError, ValueError):
        return 0


def remote_of(repo: Path, configured: str | None) -> str:
    remotes = git(repo, "remote").split()
    if configured:
        if configured not in remotes:
            raise GitError(["remote"], repo, f"remote '{configured}' not found ({remotes})")
        return configured
    if len(remotes) != 1:
        raise GitError(["remote"], repo, f"several remotes {remotes}; set trees.<side>.remote")
    return remotes[0]


def _merge_base(repo: Path, a: str, b: str) -> str | None:
    try:
        return git(repo, "merge-base", a, b).strip() or None
    except GitError:
        return None


class Deepener:
    def __init__(
        self, repo: Path, remote: str, max_commits: int, dry_run: bool = False,
        env: dict[str, str] | None = None,
    ):  # fmt: skip
        self.repo = repo
        self.remote = remote
        self.max_commits = max_commits
        self.dry_run = dry_run
        self.env = env
        self.fetches = 0

    def fetch_ref(self, ref: str) -> str | None:
        """A tag or branch the shallow clone does not have yet (one commit deep)."""
        if sha := resolve(self.repo, ref):
            return sha
        if self.dry_run:
            return None
        for spec in (f"refs/tags/{ref}:refs/tags/{ref}",
                     f"refs/heads/{ref}:refs/remotes/{self.remote}/{ref}"):  # fmt: skip
            try:
                git(self.repo, "fetch", "--quiet", "--depth=1", self.remote, spec, env=self.env)
                self.fetches += 1
                return resolve(self.repo, ref) or resolve(self.repo, f"{self.remote}/{ref}")
            except GitError:
                continue
        return None

    def until(self, done, tip: str) -> bool:
        """Deepen in doubling steps until done() or the tip has max_commits commits."""
        step = FIRST_STEP
        while not done():
            have = commit_count(self.repo, tip)
            if have >= self.max_commits or not is_shallow(self.repo) or self.dry_run:
                return done()
            step = min(step, self.max_commits - have)
            # The tip's SHA as refspec: deepen only this line, not every branch of the remote.
            git(self.repo, "fetch", "--quiet", f"--deepen={step}", self.remote, tip, env=self.env)
            self.fetches += 1
            if commit_count(self.repo, tip) == have:  # the remote has nothing older
                return done()
            step *= 2
        return True


def deepen_source(
    repo: Path, project: str, base_ref: str, head_ref: str, remote: str | None,
    max_commits: int, dry_run: bool = False, env: dict[str, str] | None = None,
) -> DeepenResult:  # fmt: skip
    """Source: base_ref and head_ref present, with their merge base (the OEM range)."""
    r = DeepenResult(project, "source", is_shallow(repo))
    try:
        d = Deepener(repo, remote_of(repo, remote), max_commits, dry_run, env)
        head, base = d.fetch_ref(head_ref), d.fetch_ref(base_ref)
        if head is None or base is None:
            r.ok = False
            r.detail = f"{head_ref if head is None else base_ref} not found"
            return r
        r.commits_before = commit_count(repo, head)
        r.ok = d.until(lambda: _merge_base(repo, base, head) is not None, head)
        r.commits_after, r.fetches = commit_count(repo, head), d.fetches
        r.detail = "OEM range complete" if r.ok else f"no merge base within {max_commits} commits"
    except GitError as e:
        r.ok, r.detail = False, str(e)
    return r


def deepen_target(
    repo: Path, project: str, base_ref: str, source_base: str | None, remote: str | None,
    max_commits: int, dry_run: bool = False, env: dict[str, str] | None = None,
) -> DeepenResult:  # fmt: skip
    """Target: history back to the source base if shared, else max_commits commits."""
    r = DeepenResult(project, "target", is_shallow(repo))
    try:
        d = Deepener(repo, remote_of(repo, remote), max_commits, dry_run, env)
        tip = d.fetch_ref(base_ref)
        if tip is None:
            r.ok, r.detail = False, f"{base_ref} not found"
            return r
        r.commits_before = commit_count(repo, tip)

        def shared() -> bool:
            return bool(source_base) and resolve(repo, source_base) is not None and (
                is_ancestor(repo, source_base, tip))  # fmt: skip

        found = d.until(shared, tip)
        r.commits_after, r.fetches = commit_count(repo, tip), d.fetches
        if found:
            r.detail = "history back to the source base"
        elif r.commits_after >= max_commits or not is_shallow(repo):
            r.detail = f"no shared history; {r.commits_after} commits to search"
        else:
            r.ok = False
            r.detail = f"only {r.commits_after} commits (dry run or remote has no more)"
    except GitError as e:
        r.ok, r.detail = False, str(e)
    return r


def deepen_trees(
    config: ProjectConfig,
    source_projects: list[str],
    target_projects: list[str],
    dry_run: bool = False,
) -> list[DeepenResult]:
    """Deepen the shallow projects among these; complete projects are left as they are."""
    trees, results, env = config.trees, [], {}
    for side, tree in (("source", trees.source), ("target", trees.target)):
        try:
            env[side] = git_env(tree.host)
        except (KeyError, OSError) as e:  # never show the secret, only that it is missing
            detail = f"trees.{side}.host.token not available: {e}"
            results.append(DeepenResult("(all)", side, True, ok=False, detail=detail))
            return results
    for project in source_projects:
        repo = trees.source.path / project
        if is_repo(repo) and is_shallow(repo):
            results.append(deepen_source(repo, project, trees.source.base_ref,
                                         trees.source.head_ref, trees.source.remote,
                                         trees.history_commits, dry_run,
                                         env["source"]))  # fmt: skip
    for project in target_projects:
        repo = trees.target.path / project
        if not (is_repo(repo) and is_shallow(repo)):
            continue
        source_repo = trees.source.path / project
        source_base = resolve(source_repo, trees.source.base_ref) if is_repo(source_repo) else None
        results.append(
            deepen_target(repo, project, trees.target.base_ref, source_base,
                          trees.target.remote, trees.history_commits, dry_run, env["target"])
        )  # fmt: skip
    return results


def gaps(config: ProjectConfig, projects: list[str]) -> list[str]:
    """Warnings for shallow projects whose history is too short for the upstream checks."""
    trees, out = config.trees, []
    for project in projects:
        source, target = trees.source.path / project, trees.target.path / project
        if is_repo(source) and is_shallow(source):
            base = resolve(source, trees.source.base_ref)
            head = resolve(source, trees.source.head_ref or "HEAD")
            if base is None or head is None or _merge_base(source, base, head) is None:
                out.append(f"{project}: source checkout is shallow and lacks the OEM range; "
                           "run migrate history")  # fmt: skip
        if is_repo(target) and is_shallow(target):
            tip = resolve(target, trees.target.base_ref)
            source_base = resolve(source, trees.source.base_ref) if is_repo(source) else None
            shared = bool(tip and source_base and resolve(target, source_base)
                          and is_ancestor(target, source_base, tip))  # fmt: skip
            have = commit_count(target, tip) if tip else 0
            if not shared and have < trees.history_commits:
                out.append(f"{project}: target checkout is shallow ({have} commits): upstream "
                           "fixes may be missed; run migrate history")  # fmt: skip
    return out


def has_range(repo: Path, base: str, head: str) -> bool:
    """base and head are both there and share history: base..head is the real range."""
    b, h = resolve(repo, base), resolve(repo, head)
    return bool(b and h and _merge_base(repo, b, h))
