from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from tests.fixtures.scenario import Scenario, build_scenario


@pytest.fixture(scope="session")
def scenario(tmp_path_factory: pytest.TempPathFactory) -> Scenario:
    return build_scenario(tmp_path_factory.mktemp("scenario"))


MINIMAL_CONFIG = {
    "name": "test-project",
    "migration": {"source_version": "14", "target_version": "15"},
    "trees": {
        "source": {"path": "src", "base_ref": "android-14-base", "head_ref": "oem-14-head"},
        "target": {"path": "dst", "base_ref": "android-15-base"},
    },
    "patch_sources": [{"type": "git_range"}],
}


@pytest.fixture
def write_config(tmp_path: Path):
    """Write a config dict (MINIMAL_CONFIG merged with overrides) and return its path."""

    def _write(overrides: dict | None = None, raw: str | None = None) -> Path:
        path = tmp_path / "project.yaml"
        if raw is not None:
            path.write_text(raw)
        else:
            path.write_text(yaml.safe_dump({**MINIMAL_CONFIG, **(overrides or {})}))
        return path

    return _write
