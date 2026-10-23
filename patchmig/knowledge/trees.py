"""Read access to the source and target trees at their base revisions, with caching.

The analysis asks the same questions many times (a file's content at the target base, the
target's projects), so answers are cached for the lifetime of one analysis.
"""

from __future__ import annotations

from functools import cache
from pathlib import Path

from patchmig.config import ProjectConfig
from patchmig.gitutil import GitError, file_at, git, is_repo, ls_files, resolve
from patchmig.ingest.manifest import ManifestError, tree_projects


class Trees:
    def __init__(self, config: ProjectConfig):
        self.config = config
        self.source = config.trees.source
        self.target = config.trees.target
        self.warnings: list[str] = []

    def source_repo(self, project: str) -> Path:
        return self.source.path / project

    def target_repo(self, project: str) -> Path | None:
        repo = self.target.path / project
        return repo if is_repo(repo) else None

    @cache  # noqa: B019  (one Trees object per analysis)
    def target_projects(self) -> list[str]:
        try:
            return tree_projects(self.target.path, self.target.manifest)
        except ManifestError as e:
            self.warnings.append(f"target projects: {e}")
            return []

    @cache  # noqa: B019
    def source_base(self, project: str) -> str | None:
        return resolve(self.source_repo(project), self.source.base_ref)

    @cache  # noqa: B019
    def target_base(self, project: str) -> str | None:
        repo = self.target_repo(project)
        return resolve(repo, self.target.base_ref) if repo else None

    @cache  # noqa: B019
    def shared_base(self, project: str) -> str | None:
        """The source base commit, if the target project's history contains it.

        With shared history (the usual case: both trees descend from the same AOSP commits),
        upstream changes are exact diffs between the two bases. Without it, the analysis
        falls back to comparing file contents.
        """
        repo, base = self.target_repo(project), self.source_base(project)
        if repo is None or base is None:
            return None
        if resolve(repo, base) is None:
            return None
        return base

    @cache  # noqa: B019
    def source_file(self, project: str, rev: str, path: str) -> str | None:
        return file_at(self.source_repo(project), rev, path)

    def source_base_file(self, project: str, path: str) -> str | None:
        base = self.source_base(project)
        return self.source_file(project, base, path) if base else None

    @cache  # noqa: B019
    def target_file(self, project: str, path: str) -> str | None:
        repo, base = self.target_repo(project), self.target_base(project)
        if repo is None or base is None:
            return None
        return file_at(repo, base, path)

    @cache  # noqa: B019
    def target_files(self, project: str) -> tuple[str, ...]:
        repo, base = self.target_repo(project), self.target_base(project)
        if repo is None or base is None:
            return ()
        return tuple(ls_files(repo, base))

    def upstream_range(self, project: str) -> str | None:
        """'source_base..target_base' in the target project, or None without shared history."""
        shared, target = self.shared_base(project), self.target_base(project)
        return f"{shared}..{target}" if shared and target else None

    def target_log(self, project: str, *args: str) -> str:
        repo = self.target_repo(project)
        if repo is None:
            return ""
        try:
            return git(repo, "log", "--no-merges", "--no-color", *args)
        except GitError as e:
            self.warnings.append(f"{project}: {e}")
            return ""
