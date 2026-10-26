"""Build environment of a workspace: `envsetup.sh`, build root."""

from pathlib import Path

from aosp_migrator.db.models import Workspace

DEFAULT_TIMEOUT = 2 * 60 * 60


def envsetup_path(workspace: Workspace) -> Path:
    """The configured `envsetup.sh`, or `build/envsetup.sh` of the repo root or target tree."""
    if workspace.envsetup_path:
        return Path(workspace.envsetup_path)
    return Path(workspace.repo_root or workspace.target_tree) / "build" / "envsetup.sh"


def build_root(workspace: Workspace) -> Path:
    """Where `m` runs: the parent of the `build/` dir holding `envsetup.sh`."""
    return envsetup_path(workspace).parent.parent
