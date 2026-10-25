from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from aosp_migrator.app import create_app
from aosp_migrator.config import Config


@pytest.fixture
def client(config: Config, static_dir: Path) -> TestClient:
    return TestClient(create_app(config, static_dir=static_dir))


def test_health(client: TestClient) -> None:
    response = client.get("/api/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_root_serves_index(client: TestClient) -> None:
    response = client.get("/")
    assert response.status_code == 200
    assert "app" in response.text


def test_deep_link_serves_index(client: TestClient) -> None:
    response = client.get("/workspaces/3/items/ABC-1")
    assert response.status_code == 200
    assert "app" in response.text


def test_static_asset_served(client: TestClient) -> None:
    response = client.get("/assets/main.js")
    assert response.text == "console.log(1)"


def test_unknown_api_route_is_404_not_index(client: TestClient) -> None:
    assert client.get("/api/nope").status_code == 404


def test_path_traversal_falls_back_to_index(client: TestClient, static_dir: Path) -> None:
    (static_dir.parent / "secret.txt").write_text("secret")
    response = client.get("/..%2Fsecret.txt")
    assert "secret" not in response.text


def test_missing_bundle_is_404(config: Config, tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    client = TestClient(create_app(config, static_dir=empty))
    assert client.get("/").status_code == 404
