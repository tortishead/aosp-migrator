from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from aosp_migrator.app import create_app


@pytest.fixture
def static_dir(tmp_path: Path) -> Path:
    (tmp_path / "index.html").write_text("<html>app</html>")
    (tmp_path / "assets").mkdir()
    (tmp_path / "assets" / "main.js").write_text("console.log(1)")
    return tmp_path


@pytest.fixture
def client(static_dir: Path) -> TestClient:
    return TestClient(create_app(static_dir=static_dir))


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


def test_missing_bundle_is_404(tmp_path: Path) -> None:
    client = TestClient(create_app(static_dir=tmp_path))
    assert client.get("/").status_code == 404
