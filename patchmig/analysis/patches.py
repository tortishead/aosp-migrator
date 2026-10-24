"""An analysis-time view of a ledger patch: the record plus its parsed diff."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from patchmig.analysis.diffs import FileDiff, split_diff
from patchmig.config import ProjectConfig
from patchmig.gitutil import GitError, commit_info
from patchmig.ledger import Patch


@dataclass
class PatchView:
    patch: Patch
    diff: str
    files: list[FileDiff]
    state: str | None = None  # decided state (a State value)
    verdict: dict = field(default_factory=dict)
    confidence: float | None = None

    @property
    def id(self) -> int:
        return self.patch.id

    @property
    def key(self) -> str:
        """The key ingest used in links (see NormalizedPatch.key)."""
        p = self.patch
        return p.commit_sha or p.fingerprint or f"{p.project}:{p.subject}"

    @property
    def project(self) -> str:
        return self.patch.project

    def file_keys(self) -> list[tuple[str, str]]:
        return [(self.project, f.path) for f in self.files]


def load_diff(config: ProjectConfig, patch: Patch, binary: bool = False) -> str:
    """A patch's diff: its commit's from the source tree, else its diff file; "" if neither.

    With `binary`, a commit's diff carries binary files too (for applying, not for showing).
    """
    if patch.commit_sha:
        repo = config.trees.source.path / patch.project
        try:
            return commit_info(repo, patch.commit_sha, binary=binary).diff
        except GitError:
            pass
    if patch.diff_path and Path(patch.diff_path).exists():
        return Path(patch.diff_path).read_text(errors="replace")
    return ""


def view(config: ProjectConfig, patch: Patch) -> PatchView:
    diff = load_diff(config, patch)
    return PatchView(patch=patch, diff=diff, files=split_diff(diff))
