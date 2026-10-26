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


def validate_repo_root(value: str, target: Path) -> Path:
    """The `repo` checkout holding `target`: a directory with `.repo`, or that `.repo` itself."""
    root = validate_tree_path(value)
    if root.name == ".repo":
        root = root.parent
    if not (root / ".repo").is_dir():
        raise TreePathError(f"has no .repo directory: {root}")
    if not target.is_relative_to(root):
        raise TreePathError(f"does not contain the target tree: {root}")
    return root


def validate_envsetup(value: str, root: Path) -> Path:
    """An `envsetup.sh` in a `build/` directory inside `root` (the repo root or target tree)."""
    path = Path(value.strip()).expanduser()
    if not path.is_absolute():
        raise TreePathError(f"must be an absolute path: {value}")
    if not path.is_file():
        raise TreePathError(f"is not a file: {path}")
    if path.parent.name != "build":
        raise TreePathError(f"is not in a build/ directory: {path}")
    if not path.parent.parent.is_relative_to(root):
        raise TreePathError(f"is not inside {root}: {path}")
    return path


def detect_tree_type(path: Path) -> TreeType:
    if (path / ".repo").is_dir():
        return "repo"
    # .git is a file in worktrees and submodules.
    if (path / ".git").exists():
        return "git"
    return "plain"
