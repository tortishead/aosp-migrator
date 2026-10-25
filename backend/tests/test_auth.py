from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from aosp_migrator.app import create_app
from aosp_migrator.config import Config


@pytest.fixture
def lan_client(config: Config, static_dir: Path) -> TestClient:
    return TestClient(create_app(config, lan=True, static_dir=static_dir))


def test_no_token_needed_without_lan(config: Config) -> None:
    client = TestClient(create_app(config))
    assert client.get("/api/settings").status_code == 200
    assert client.get("/api/auth/status").json() == {"required": False, "authenticated": True}


def test_lan_requires_token(lan_client: TestClient) -> None:
    assert lan_client.get("/api/settings").status_code == 401
    assert lan_client.put("/api/settings", json={}).status_code == 401
    assert lan_client.get("/api/auth/status").json() == {"required": True, "authenticated": False}


def test_lan_public_paths_and_frontend_open(lan_client: TestClient) -> None:
    assert lan_client.get("/api/health").status_code == 200
    assert lan_client.get("/").status_code == 200
    assert lan_client.get("/settings").status_code == 200


def test_lan_login_sets_cookie(lan_client: TestClient, config: Config) -> None:
    token = config.ensure_lan_token()
    assert lan_client.post("/api/auth/login", json={"token": "wrong"}).status_code == 401
    assert lan_client.get("/api/settings").status_code == 401

    assert lan_client.post("/api/auth/login", json={"token": token}).status_code == 200
    assert lan_client.get("/api/settings").status_code == 200
    assert lan_client.get("/api/auth/status").json()["authenticated"] is True

    lan_client.post("/api/auth/logout")
    assert lan_client.get("/api/settings").status_code == 401


def test_lan_bearer_header(lan_client: TestClient, config: Config) -> None:
    token = config.ensure_lan_token()
    response = lan_client.get("/api/settings", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200


def test_lan_token_from_env(config: Config, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AOSP_MIGRATOR_LAN_TOKEN", "env-token")
    client = TestClient(create_app(config, lan=True))
    assert client.get("/api/settings", headers={"Authorization": "Bearer env-token"}).status_code == 200
    assert not config.path.exists()
