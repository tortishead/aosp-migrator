from pathlib import Path

from aosp_migrator.db.models import TreeType


class TreePathError(ValueError):
    pass


def validate_tree_path(value: str) -> Path:
    """Expand and absolutize a tree path; it must be an existing directory."""
    path = Path(value.strip()).expanduser()
    if not path.is_absolute():
        raise TreePathError(f"must be an absolute path: {value}")
    if not path.exists():
        raise TreePathError(f"does not exist: {path}")
    if not path.is_dir():
        raise TreePathError(f"is not a directory: {path}")
    return path


def detect_tree_type(path: Path) -> TreeType:
    if (path / ".repo").is_dir():
        return "repo"
    # .git is a file in worktrees and submodules.
    if (path / ".git").exists():
        return "git"
    return "plain"
