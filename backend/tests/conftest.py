from collections.abc import Iterator
from pathlib import Path

import pytest
from fake_jira import FakeJiraServer, FakeJiraStore, default_store, run_fake_jira

from aosp_migrator.config import FIELDS, DATA_DIR_ENV, Config


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    # Never let the developer's real settings leak into tests.
    monkeypatch.delenv(DATA_DIR_ENV, raising=False)
    for field in FIELDS:
        monkeypatch.delenv(field.env, raising=False)


@pytest.fixture
def config(tmp_path: Path) -> Config:
    return Config(tmp_path / "data")


@pytest.fixture
def static_dir(tmp_path: Path) -> Path:
    static = tmp_path / "static"
    static.mkdir()
    (static / "index.html").write_text("<html>app</html>")
    (static / "assets").mkdir()
    (static / "assets" / "main.js").write_text("console.log(1)")
    return static


@pytest.fixture(scope="session")
def fake_jira_server() -> Iterator[FakeJiraServer]:
    # One server for the whole run; each test gets a fresh store via `fake_jira`.
    with run_fake_jira(default_store()) as server:
        yield server


@pytest.fixture
def fake_jira(fake_jira_server: FakeJiraServer) -> FakeJiraStore:
    """Fresh default fixtures, served at `jira_url`. Mutate it to change what Jira returns."""
    fake_jira_server.store = default_store()
    return fake_jira_server.store


@pytest.fixture
def jira_url(fake_jira_server: FakeJiraServer, fake_jira: FakeJiraStore) -> str:
    return fake_jira_server.url
