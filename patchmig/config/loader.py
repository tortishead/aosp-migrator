"""Load, validate and serialise project configuration files."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import yaml
from pydantic import ValidationError

from patchmig.config.schema import DiffFolderSource, ProjectConfig, SpreadsheetSource


class ConfigError(Exception):
    """A config file is missing, unreadable or invalid. The message lists every problem."""

    def __init__(self, path: Path, problems: list[str]):
        self.path = path
        self.problems = problems
        lines = "\n".join(f"  - {p}" for p in problems)
        super().__init__(f"invalid config {path}:\n{lines}")


def _format_errors(err: ValidationError) -> list[str]:
    problems = []
    for e in err.errors():
        loc = ".".join(str(part) for part in e["loc"]) or "(top level)"
        msg = e["msg"].removeprefix("Value error, ")
        problems.append(f"{loc}: {msg}")
    return problems


def _anchor(path: Path, base: Path) -> Path:
    path = path.expanduser()
    return path if path.is_absolute() else (base / path).resolve()


def _resolve_paths(config: ProjectConfig, base: Path) -> ProjectConfig:
    """Make every relative path in the config relative to the config file's folder."""
    config.workdir = _anchor(config.workdir, base)
    config.trees.source.path = _anchor(config.trees.source.path, base)
    config.trees.target.path = _anchor(config.trees.target.path, base)
    for source in config.patch_sources:
        if isinstance(source, DiffFolderSource | SpreadsheetSource):
            source.path = _anchor(source.path, base)
    if config.memory.path is not None:
        config.memory.path = _anchor(config.memory.path, base)
    return config


def load_config(path: Path | str) -> ProjectConfig:
    path = Path(path)
    try:
        raw = yaml.safe_load(path.read_text())
    except OSError as e:
        raise ConfigError(path, [f"cannot read file: {e.strerror}"]) from e
    except yaml.YAMLError as e:
        raise ConfigError(path, [f"not valid YAML: {e}"]) from e
    if not isinstance(raw, dict):
        raise ConfigError(path, ["top level must be a mapping"])
    try:
        config = ProjectConfig.model_validate(raw)
    except ValidationError as e:
        raise ConfigError(path, _format_errors(e)) from e
    return _resolve_paths(config, path.resolve().parent)


def config_to_dict(config: ProjectConfig) -> dict:
    return config.model_dump(mode="json", exclude_none=True)


def dump_config(config: ProjectConfig) -> str:
    return yaml.safe_dump(config_to_dict(config), sort_keys=False)


def config_hash(config: ProjectConfig) -> str:
    """Stable fingerprint of a config, recorded with every run for reproducibility."""
    canonical = json.dumps(config_to_dict(config), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()
