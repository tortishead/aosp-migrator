import stat
from pathlib import Path

import pytest

from aosp_migrator.config import (
    DEFAULT_DATA_DIR,
    JIRA_BASE_URL,
    JIRA_PAT,
    LAN_TOKEN,
    Config,
    resolve_data_dir,
)


def test_data_dir_default() -> None:
    assert resolve_data_dir() == DEFAULT_DATA_DIR


def test_data_dir_env_beats_default(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("AOSP_MIGRATOR_DATA_DIR", str(tmp_path / "env"))
    assert resolve_data_dir() == tmp_path / "env"


def test_data_dir_cli_beats_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("AOSP_MIGRATOR_DATA_DIR", str(tmp_path / "env"))
    assert resolve_data_dir(tmp_path / "cli") == tmp_path / "cli"


def test_config_file_is_0600(config: Config) -> None:
    config.set({JIRA_PAT: "secret"})
    assert stat.S_IMODE(config.path.stat().st_mode) == 0o600


def test_config_file_stays_0600_after_rewrite(config: Config) -> None:
    config.set({JIRA_PAT: "one"})
    config.path.chmod(0o644)
    config.set({JIRA_PAT: "two"})
    assert stat.S_IMODE(config.path.stat().st_mode) == 0o600


def test_set_and_get_roundtrip(config: Config) -> None:
    config.set({JIRA_BASE_URL: "https://jira.example.com", JIRA_PAT: "secret"})
    assert Config(config.data_dir).get(JIRA_BASE_URL) == "https://jira.example.com"
    assert config.get_with_source(JIRA_PAT) == ("secret", "file")


def test_set_empty_removes_key(config: Config) -> None:
    config.set({JIRA_PAT: "secret"})
    config.set({JIRA_PAT: ""})
    assert config.get_with_source(JIRA_PAT) == (None, "unset")
    assert "pat" not in config.path.read_text()


def test_env_overrides_file(config: Config, monkeypatch: pytest.MonkeyPatch) -> None:
    config.set({JIRA_PAT: "from-file"})
    monkeypatch.setenv("AOSP_MIGRATOR_JIRA_PAT", "from-env")
    assert config.get_with_source(JIRA_PAT) == ("from-env", "env")


def test_public_view_hides_secrets(config: Config) -> None:
    config.set({JIRA_BASE_URL: "https://jira.example.com", JIRA_PAT: "secret"})
    view = config.public_view()
    assert "secret" not in repr(view)
    assert view["jira"]["pat"] == {"source": "file", "is_set": True}
    assert view["jira"]["base_url"] == {"source": "file", "value": "https://jira.example.com"}
    assert view["github"]["pat"] == {"source": "unset", "is_set": False}
    assert "server" not in view


def test_lan_token_generated_once(config: Config) -> None:
    token = config.ensure_lan_token()
    assert len(token) >= 32
    assert Config(config.data_dir).ensure_lan_token() == token
    assert config.get(LAN_TOKEN) == token
