import httpx2
import pytest
from fake_copilot import FAKE_COPILOT
from fastapi.testclient import TestClient

from aosp_migrator.app import create_app
from aosp_migrator.config import Config
from aosp_migrator.copilot.auth import api_url, auth_hint, check_token, find_token

GOOD = "github_pat_good"


def github(requests: list[httpx2.Request]) -> httpx2.MockTransport:
    """GitHub's `GET /user`: accepts GOOD, rejects any other token."""

    def handle(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        if request.headers.get("Authorization") == f"Bearer {GOOD}":
            return httpx2.Response(200, json={"login": "octo"})
        return httpx2.Response(401, json={"message": "Bad credentials"})

    return httpx2.MockTransport(handle)


def test_missing_token() -> None:
    check = check_token({})
    assert (check.status, check.variable) == ("missing", None)
    assert check.message.startswith("COPILOT_GITHUB_TOKEN is not set")
    # Blank counts as unset.
    assert check_token({"COPILOT_GITHUB_TOKEN": "  "}).status == "missing"


def test_valid_and_invalid_token() -> None:
    requests: list[httpx2.Request] = []
    check = check_token({"COPILOT_GITHUB_TOKEN": GOOD}, github(requests))
    assert (check.status, check.variable, check.login) == ("valid", "COPILOT_GITHUB_TOKEN", "octo")
    assert str(requests[0].url) == "https://api.github.com/user"

    check = check_token({"COPILOT_GITHUB_TOKEN": "github_pat_typo"}, github(requests))
    assert (check.status, check.login) == ("invalid", None)
    assert check.message.startswith("COPILOT_GITHUB_TOKEN is not valid")


def test_classic_pat_is_invalid_without_asking_github() -> None:
    requests: list[httpx2.Request] = []
    check = check_token({"GH_TOKEN": "ghp_classic"}, github(requests))
    assert (check.status, check.variable) == ("invalid", "GH_TOKEN")
    assert "classic" in check.message and requests == []


def test_unreachable_or_odd_answer_is_unverified() -> None:
    def down(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("no route", request=request)

    assert check_token({"COPILOT_GITHUB_TOKEN": GOOD}, httpx2.MockTransport(down)).status == "unverified"
    odd = httpx2.MockTransport(lambda request: httpx2.Response(503))
    check = check_token({"COPILOT_GITHUB_TOKEN": GOOD}, odd)
    assert check.status == "unverified" and "HTTP 503" in check.message


def test_variable_order_and_host() -> None:
    assert find_token({"GITHUB_TOKEN": "c", "GH_TOKEN": "b", "COPILOT_GITHUB_TOKEN": "a"}) == ("COPILOT_GITHUB_TOKEN", "a")
    assert find_token({"GITHUB_TOKEN": "c", "GH_TOKEN": "b"}) == ("GH_TOKEN", "b")
    assert api_url({}) == "https://api.github.com"
    assert api_url({"GH_HOST": "github.example.com"}) == "https://github.example.com/api/v3"


def test_auth_hint() -> None:
    error = "Error: No authentication information found. Copilot can be authenticated..."
    assert auth_hint("Error: model not found", {}) is None
    assert (auth_hint(error, {}) or "").startswith("COPILOT_GITHUB_TOKEN is not set")
    assert (auth_hint(error, {"COPILOT_GITHUB_TOKEN": "x"}) or "").startswith("COPILOT_GITHUB_TOKEN is set")


@pytest.fixture
def requests() -> list[httpx2.Request]:
    return []


@pytest.fixture
def api(config: Config, requests: list[httpx2.Request]) -> TestClient:
    client = TestClient(create_app(config, http_transport=github(requests)))
    client.put("/api/settings", json={"copilot": {"binary": str(FAKE_COPILOT)}})
    return client


def test_endpoint_reports_a_valid_token(api: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COPILOT_GITHUB_TOKEN", GOOD)
    body = api.post("/api/settings/copilot/test", json={}).json()
    assert body["ok"] is True
    assert body["token"] == {"status": "valid", "variable": "COPILOT_GITHUB_TOKEN", "login": "octo", "message": "COPILOT_GITHUB_TOKEN is valid for GitHub user octo."}


def test_endpoint_fails_an_invalid_token_before_the_prompt(api: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    log = tmp_path / "calls.jsonl"
    monkeypatch.setenv("FAKE_COPILOT_LOG", str(log))
    monkeypatch.setenv("COPILOT_GITHUB_TOKEN", "github_pat_typo")
    body = api.post("/api/settings/copilot/test", json={}).json()
    assert body["ok"] is False and body["token"]["status"] == "invalid"
    assert body["error"].startswith("COPILOT_GITHUB_TOKEN is not valid")
    # No premium request spent.
    assert not log.exists()


def test_endpoint_warns_without_a_token_when_the_cli_is_logged_in(api: TestClient, requests: list[httpx2.Request]) -> None:
    body = api.post("/api/settings/copilot/test", json={}).json()
    assert body["ok"] is True and body["token"]["status"] == "missing"
    assert requests == []
