import httpx2
import pytest
from fastapi.testclient import TestClient

from aosp_migrator.app import create_app
from aosp_migrator.config import JIRA_BASE_URL, JIRA_PAT, Config

SECRET = "pat-super-secret-value"


def jira_transport(seen: list[httpx2.Request]) -> httpx2.MockTransport:
    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        if request.url.path != "/rest/api/2/myself":
            return httpx2.Response(404)
        if request.headers.get("authorization") != f"Bearer {SECRET}":
            return httpx2.Response(401)
        return httpx2.Response(200, json={"name": "jdoe", "displayName": "Jane Doe"})

    return httpx2.MockTransport(handler)


@pytest.fixture
def seen() -> list[httpx2.Request]:
    return []


@pytest.fixture
def client(config: Config, seen: list[httpx2.Request]) -> TestClient:
    return TestClient(create_app(config, http_transport=jira_transport(seen)))


def test_get_settings_initially_unset(client: TestClient) -> None:
    body = client.get("/api/settings").json()
    assert body["jira"]["base_url"] == {"source": "unset", "value": None}
    assert body["jira"]["pat"] == {"source": "unset", "is_set": False}


def test_secret_never_returned(client: TestClient) -> None:
    put = client.put("/api/settings", json={"jira": {"base_url": "https://jira.example.com/", "pat": SECRET}})
    get = client.get("/api/settings")
    for response in (put, get):
        assert SECRET not in response.text
        assert response.json()["jira"]["pat"]["is_set"] is True
    assert get.json()["jira"]["base_url"]["value"] == "https://jira.example.com"


def test_omitted_pat_is_unchanged_and_empty_pat_clears(client: TestClient, config: Config) -> None:
    client.put("/api/settings", json={"jira": {"pat": SECRET}})
    client.put("/api/settings", json={"jira": {"base_url": "https://jira.example.com"}})
    assert config.get(JIRA_PAT) == SECRET
    client.put("/api/settings", json={"jira": {"pat": ""}})
    assert config.get(JIRA_PAT) is None


def test_invalid_base_url_rejected(client: TestClient, config: Config) -> None:
    response = client.put("/api/settings", json={"jira": {"base_url": "jira.example.com"}})
    assert response.status_code == 422
    assert config.get(JIRA_BASE_URL) is None


def test_env_source_reported(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AOSP_MIGRATOR_JIRA_PAT", SECRET)
    body = client.get("/api/settings").json()
    assert body["jira"]["pat"] == {"source": "env", "is_set": True}


def test_jira_test_uses_saved_settings(client: TestClient, config: Config, seen: list) -> None:
    config.set({JIRA_BASE_URL: "https://jira.example.com", JIRA_PAT: SECRET})
    body = client.post("/api/settings/jira/test", json={}).json()
    assert body == {"ok": True, "user": "Jane Doe"}
    assert str(seen[0].url) == "https://jira.example.com/rest/api/2/myself"


def test_jira_test_prefers_unsaved_form_values(client: TestClient, config: Config) -> None:
    config.set({JIRA_BASE_URL: "https://jira.example.com", JIRA_PAT: "wrong"})
    body = client.post("/api/settings/jira/test", json={"pat": SECRET}).json()
    assert body["ok"] is True
    assert config.get(JIRA_PAT) == "wrong"


def test_jira_test_auth_failure(client: TestClient, config: Config) -> None:
    config.set({JIRA_BASE_URL: "https://jira.example.com", JIRA_PAT: "wrong"})
    body = client.post("/api/settings/jira/test", json={}).json()
    assert body["ok"] is False
    assert "401" in body["error"]


def test_jira_test_missing_settings(client: TestClient) -> None:
    body = client.post("/api/settings/jira/test", json={}).json()
    assert body["ok"] is False


def test_jira_test_unreachable(config: Config) -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("connection refused", request=request)

    client = TestClient(create_app(config, http_transport=httpx2.MockTransport(handler)))
    config.set({JIRA_BASE_URL: "https://jira.example.com", JIRA_PAT: SECRET})
    body = client.post("/api/settings/jira/test", json={}).json()
    assert body["ok"] is False
    assert "Cannot reach Jira" in body["error"]
