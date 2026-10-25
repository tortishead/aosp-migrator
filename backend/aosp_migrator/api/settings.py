from urllib.parse import urlparse

import httpx2
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from aosp_migrator.config import GITHUB_PAT, JIRA_BASE_URL, JIRA_PAT, Config, Field

router = APIRouter(prefix="/settings")


class JiraSettingsUpdate(BaseModel):
    base_url: str | None = None
    # None leaves the PAT unchanged, "" clears it.
    pat: str | None = None


class GithubSettingsUpdate(BaseModel):
    pat: str | None = None


class SettingsUpdate(BaseModel):
    jira: JiraSettingsUpdate | None = None
    github: GithubSettingsUpdate | None = None


class JiraTestRequest(BaseModel):
    # Unsaved values from the form; fall back to the saved ones when omitted.
    base_url: str | None = None
    pat: str | None = None


def _config(request: Request) -> Config:
    return request.app.state.config


def normalize_base_url(value: str) -> str:
    value = value.strip().rstrip("/")
    parsed = urlparse(value)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise HTTPException(status_code=422, detail="Jira base URL must be an http(s) URL")
    return value


@router.get("")
def get_settings(request: Request) -> dict:
    return _config(request).public_view()


@router.put("")
def update_settings(update: SettingsUpdate, request: Request) -> dict:
    config = _config(request)
    values: dict[Field, str | None] = {}
    if update.jira:
        if update.jira.base_url is not None:
            base_url = update.jira.base_url.strip()
            values[JIRA_BASE_URL] = normalize_base_url(base_url) if base_url else None
        if update.jira.pat is not None:
            values[JIRA_PAT] = update.jira.pat.strip() or None
    if update.github and update.github.pat is not None:
        values[GITHUB_PAT] = update.github.pat.strip() or None
    if values:
        config.set(values)
    return config.public_view()


@router.post("/jira/test")
def test_jira(body: JiraTestRequest, request: Request) -> dict:
    config = _config(request)
    base_url = (body.base_url or "").strip() or config.get(JIRA_BASE_URL)
    pat = (body.pat or "").strip() or config.get(JIRA_PAT)
    if not base_url or not pat:
        return {"ok": False, "error": "Jira base URL and PAT are required"}
    base_url = normalize_base_url(base_url)

    transport = request.app.state.http_transport
    try:
        with httpx2.Client(transport=transport, timeout=15) as client:
            response = client.get(
                f"{base_url}/rest/api/2/myself",
                headers={"Authorization": f"Bearer {pat}", "Accept": "application/json"},
            )
    except httpx2.HTTPError as exc:
        return {"ok": False, "error": f"Cannot reach Jira: {exc}"}

    if response.status_code in (401, 403):
        return {"ok": False, "error": f"Authentication failed (HTTP {response.status_code})"}
    if response.status_code != 200:
        return {"ok": False, "error": f"Unexpected response (HTTP {response.status_code})"}
    try:
        user = response.json()
    except ValueError:
        return {"ok": False, "error": "Response is not JSON. Is this a Jira base URL?"}
    return {"ok": True, "user": user.get("displayName") or user.get("name")}
