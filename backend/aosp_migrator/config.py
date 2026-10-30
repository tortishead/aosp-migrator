"""Data dir resolution and config.toml handling.

Precedence for every setting: environment variable > config.toml > default.
Secrets are only ever written to config.toml with 0600 permissions and are
never returned by the API (see `public_view`).
"""

import os
import secrets
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import tomli_w

DATA_DIR_ENV = "AOSP_MIGRATOR_DATA_DIR"
DEFAULT_DATA_DIR = Path.home() / ".aosp-migrator"
CONFIG_FILENAME = "config.toml"

Source = Literal["env", "file", "unset"]


@dataclass(frozen=True)
class Field:
    section: str
    key: str
    env: str
    secret: bool


JIRA_BASE_URL = Field("jira", "base_url", "AOSP_MIGRATOR_JIRA_BASE_URL", secret=False)
JIRA_PAT = Field("jira", "pat", "AOSP_MIGRATOR_JIRA_PAT", secret=True)
GITHUB_PAT = Field("github", "pat", "AOSP_MIGRATOR_GITHUB_PAT", secret=True)
LAN_TOKEN = Field("server", "lan_token", "AOSP_MIGRATOR_LAN_TOKEN", secret=True)
# Comma-separated author email domains whose patches are looked up in target history.
UPSTREAM_DOMAINS = Field("prescan", "upstream_domains", "AOSP_MIGRATOR_UPSTREAM_DOMAINS", secret=False)

# Copilot CLI for rework (MS5): binary, model per rung (unset: the CLI's default), seconds per call.
COPILOT_BINARY = Field("copilot", "binary", "AOSP_MIGRATOR_COPILOT_BINARY", secret=False)
COPILOT_MODEL_RESOLVE = Field("copilot", "model_resolve", "AOSP_MIGRATOR_COPILOT_MODEL_RESOLVE", secret=False)
COPILOT_MODEL_PORT = Field("copilot", "model_port", "AOSP_MIGRATOR_COPILOT_MODEL_PORT", secret=False)
COPILOT_MODEL_REIMPL = Field("copilot", "model_reimpl", "AOSP_MIGRATOR_COPILOT_MODEL_REIMPL", secret=False)
COPILOT_TIMEOUT = Field("copilot", "timeout", "AOSP_MIGRATOR_COPILOT_TIMEOUT", secret=False)
# Rework caps: Copilot calls per item and per job, build-fix rounds per attempt.
REWORK_ITEM_CAP = Field("rework", "item_cap", "AOSP_MIGRATOR_REWORK_ITEM_CAP", secret=False)
REWORK_JOB_CAP = Field("rework", "job_cap", "AOSP_MIGRATOR_REWORK_JOB_CAP", secret=False)
REWORK_BUILD_ROUNDS = Field("rework", "build_rounds", "AOSP_MIGRATOR_REWORK_BUILD_ROUNDS", secret=False)
# Most modules using a changed header, export or API that a build adds; 0 for none.
BUILD_DEPENDENTS_CAP = Field("build", "dependents_cap", "AOSP_MIGRATOR_BUILD_DEPENDENTS_CAP", secret=False)

FIELDS = (
    JIRA_BASE_URL,
    JIRA_PAT,
    GITHUB_PAT,
    LAN_TOKEN,
    UPSTREAM_DOMAINS,
    COPILOT_BINARY,
    COPILOT_MODEL_RESOLVE,
    COPILOT_MODEL_PORT,
    COPILOT_MODEL_REIMPL,
    COPILOT_TIMEOUT,
    REWORK_ITEM_CAP,
    REWORK_JOB_CAP,
    REWORK_BUILD_ROUNDS,
    BUILD_DEPENDENTS_CAP,
)
DEFAULT_UPSTREAM_DOMAINS = ("google.com", "android.com")
DEFAULT_COPILOT_BINARY = "copilot"
# Field to (default, lowest, highest).
INT_SETTINGS: dict[Field, tuple[int, int, int]] = {
    COPILOT_TIMEOUT: (900, 30, 7200),
    REWORK_ITEM_CAP: (5, 1, 50),
    REWORK_JOB_CAP: (100, 1, 10000),
    REWORK_BUILD_ROUNDS: (2, 0, 10),
    BUILD_DEPENDENTS_CAP: (20, 0, 1000),
}
# Rework rung to its model field.
COPILOT_MODELS = {"resolve": COPILOT_MODEL_RESOLVE, "port": COPILOT_MODEL_PORT, "reimpl": COPILOT_MODEL_REIMPL}


@dataclass(frozen=True)
class CopilotSettings:
    binary: str
    # Rung to model; None: the CLI's default.
    models: dict[str, str | None]
    timeout: int
    item_cap: int
    job_cap: int
    build_rounds: int


def resolve_data_dir(cli_value: str | Path | None = None) -> Path:
    if cli_value:
        return Path(cli_value).expanduser()
    if env_value := os.environ.get(DATA_DIR_ENV):
        return Path(env_value).expanduser()
    return DEFAULT_DATA_DIR


class Config:
    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir
        self.path = data_dir / CONFIG_FILENAME

    def attachments_dir(self, workspace_id: int) -> Path:
        return self.workspace_dir(workspace_id) / "attachments"

    def builds_dir(self, workspace_id: int) -> Path:
        return self.workspace_dir(workspace_id) / "builds"

    def rework_dir(self, workspace_id: int, attempt_id: int) -> Path:
        """Scratch checkouts of one rework attempt, one per project at its path below."""
        return self.workspace_dir(workspace_id) / "rework" / str(attempt_id)

    def workspace_dir(self, workspace_id: int) -> Path:
        return self.data_dir / "workspaces" / str(workspace_id)

    def _read_file(self) -> dict[str, dict[str, str]]:
        if not self.path.is_file():
            return {}
        with self.path.open("rb") as f:
            return tomllib.load(f)

    def _write_file(self, data: dict[str, dict[str, str]]) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        tmp = self.path.with_suffix(".toml.tmp")
        # Create with 0600 from the start so secrets are never world-readable,
        # then atomically replace the old file.
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as f:
            tomli_w.dump(data, f)
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.path)

    def get(self, field: Field) -> str | None:
        return self.get_with_source(field)[0]

    def get_with_source(self, field: Field) -> tuple[str | None, Source]:
        if env_value := os.environ.get(field.env):
            return env_value, "env"
        file_value = self._read_file().get(field.section, {}).get(field.key)
        if file_value:
            return file_value, "file"
        return None, "unset"

    def set(self, values: dict[Field, str | None]) -> None:
        """Write values to config.toml. None or "" removes the key."""
        data = self._read_file()
        for field, value in values.items():
            section = data.setdefault(field.section, {})
            if value:
                section[field.key] = value
            else:
                section.pop(field.key, None)
                if not section:
                    del data[field.section]
        self._write_file(data)

    def upstream_domains(self) -> list[str]:
        value = self.get(UPSTREAM_DOMAINS)
        if value is None:
            return list(DEFAULT_UPSTREAM_DOMAINS)
        return [d.strip().lower() for d in value.split(",") if d.strip()]

    def get_int(self, field: Field) -> int:
        """An `INT_SETTINGS` value; its default when unset or out of range."""
        default, low, high = INT_SETTINGS[field]
        try:
            value = int(self.get(field) or default)
        except ValueError:
            return default
        return value if low <= value <= high else default

    def copilot(self) -> CopilotSettings:
        return CopilotSettings(
            binary=self.get(COPILOT_BINARY) or DEFAULT_COPILOT_BINARY,
            models={rung: self.get(field) for rung, field in COPILOT_MODELS.items()},
            timeout=self.get_int(COPILOT_TIMEOUT),
            item_cap=self.get_int(REWORK_ITEM_CAP),
            job_cap=self.get_int(REWORK_JOB_CAP),
            build_rounds=self.get_int(REWORK_BUILD_ROUNDS),
        )

    def ensure_lan_token(self) -> str:
        token = self.get(LAN_TOKEN)
        if token is None:
            token = secrets.token_urlsafe(32)
            self.set({LAN_TOKEN: token})
        return token

    def public_view(self) -> dict[str, dict[str, object]]:
        """Settings as the API returns them: secrets reduced to set / not set."""
        view: dict[str, dict[str, object]] = {}
        for field in (JIRA_BASE_URL, JIRA_PAT, GITHUB_PAT):
            value, source = self.get_with_source(field)
            entry: dict[str, object] = {"source": source}
            if field.secret:
                entry["is_set"] = value is not None
            else:
                entry["value"] = value
            view.setdefault(field.section, {})[field.key] = entry
        # The domains in effect: the defaults while unset.
        view["prescan"] = {"upstream_domains": {"source": self.get_with_source(UPSTREAM_DOMAINS)[1], "value": self.upstream_domains()}}
        # Values in effect: defaults while unset; a model None is the CLI's default.
        copilot = self.copilot()
        view["copilot"] = {
            "binary": {"source": self.get_with_source(COPILOT_BINARY)[1], "value": copilot.binary},
            **{f"model_{rung}": {"source": self.get_with_source(field)[1], "value": copilot.models[rung]} for rung, field in COPILOT_MODELS.items()},
            "timeout": {"source": self.get_with_source(COPILOT_TIMEOUT)[1], "value": copilot.timeout},
        }
        view["rework"] = {
            field.key: {"source": self.get_with_source(field)[1], "value": self.get_int(field)} for field in (REWORK_ITEM_CAP, REWORK_JOB_CAP, REWORK_BUILD_ROUNDS)
        }
        view["build"] = {"dependents_cap": {"source": self.get_with_source(BUILD_DEPENDENTS_CAP)[1], "value": self.get_int(BUILD_DEPENDENTS_CAP)}}
        return view
