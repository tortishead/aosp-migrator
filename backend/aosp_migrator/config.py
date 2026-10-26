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

FIELDS = (JIRA_BASE_URL, JIRA_PAT, GITHUB_PAT, LAN_TOKEN, UPSTREAM_DOMAINS)
DEFAULT_UPSTREAM_DOMAINS = ("google.com", "android.com")


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
        return view
