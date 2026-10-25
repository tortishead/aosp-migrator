from pathlib import Path

import pytest

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
