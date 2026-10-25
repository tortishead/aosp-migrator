"""Project configuration: schema, loading and validation."""

from patchmig.config.loader import ConfigError, config_hash, dump_config, load_config, new_project
from patchmig.config.schema import ProjectConfig, SecretRef

__all__ = [
    "ConfigError",
    "ProjectConfig",
    "SecretRef",
    "config_hash",
    "dump_config",
    "load_config",
    "new_project",
]
