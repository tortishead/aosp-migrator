import re
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import urlparse

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from aosp_migrator.config import (
    BUILD_DEPENDENTS_CAP,
    COPILOT_BINARY,
    COPILOT_MODELS,
    COPILOT_TIMEOUT,
    GITHUB_PAT,
    INT_SETTINGS,
    JIRA_BASE_URL,
    JIRA_PAT,
    REWORK_BUILD_ROUNDS,
    REWORK_ITEM_CAP,
    REWORK_JOB_CAP,
    REWORK_KEEP_SCRATCH,
    UPSTREAM_DOMAINS,
    Config,
    Field,
)
from aosp_migrator.copilot.auth import check_token
from aosp_migrator.copilot.client import CopilotClient
from aosp_migrator.jira.base import JiraAuthError, JiraError
from aosp_migrator.jira.datacenter import DataCenterClient

router = APIRouter(prefix="/settings")


class JiraSettingsUpdate(BaseModel):
    base_url: str | None = None
    # None leaves the PAT unchanged, "" clears it.
    pat: str | None = None


class GithubSettingsUpdate(BaseModel):
    pat: str | None = None


class PrescanSettingsUpdate(BaseModel):
    # Comma-separated author email domains; None leaves them unchanged, "" restores the defaults.
    upstream_domains: str | None = None


class CopilotSettingsUpdate(BaseModel):
    # Each: None leaves it unchanged, "" restores the default (models: the CLI's default).
    binary: str | None = None
    model_resolve: str | None = None
    model_port: str | None = None
    model_reimpl: str | None = None
    # Seconds per Copilot call; 0 restores the default.
    timeout: int | None = None


class ReworkSettingsUpdate(BaseModel):
    # Each: None leaves it unchanged, 0 restores the default (build rounds: -1).
    item_cap: int | None = None
    job_cap: int | None = None
    build_rounds: int | None = None
    keep_scratch: bool | None = None


class BuildSettingsUpdate(BaseModel):
    # None leaves it unchanged, -1 restores the default.
    dependents_cap: int | None = None


class SettingsUpdate(BaseModel):
    jira: JiraSettingsUpdate | None = None
    github: GithubSettingsUpdate | None = None
    prescan: PrescanSettingsUpdate | None = None
    copilot: CopilotSettingsUpdate | None = None
    rework: ReworkSettingsUpdate | None = None
    build: BuildSettingsUpdate | None = None


class CopilotTestRequest(BaseModel):
    # Unsaved values from the form; fall back to the saved ones when omitted.
    binary: str | None = None
    model: str | None = None


# Seconds the test may take: one short prompt.
COPILOT_TEST_TIMEOUT = 180
COPILOT_TEST_PROMPT = "Reply with the single word OK. Do not use any tool."


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


def normalize_domains(value: str) -> str | None:
    """Lower-cased, deduplicated, comma-joined; a leading `@` is dropped. None when empty."""
    domains = list(dict.fromkeys(d.strip().lower().removeprefix("@") for d in value.split(",") if d.strip()))
    if invalid := [d for d in domains if not re.fullmatch(r"[a-z0-9-]+(\.[a-z0-9-]+)+", d)]:
        raise HTTPException(status_code=422, detail=f"Not an email domain: {', '.join(invalid)}")
    return ",".join(domains) or None


def normalize_model(value: str) -> str | None:
    value = value.strip()
    if value and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]*", value):
        raise HTTPException(status_code=422, detail=f"Not a model name: {value}")
    return value or None


def checked_int(field: Field, value: int, reset: int = 0) -> str | None:
    """The value to store; None (the default) for `reset`; 422 outside the field's range."""
    if value == reset:
        return None
    _, low, high = INT_SETTINGS[field]
    if not low <= value <= high:
        raise HTTPException(status_code=422, detail=f"{field.key} must be between {low} and {high}")
    return str(value)


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
    if update.prescan and update.prescan.upstream_domains is not None:
        values[UPSTREAM_DOMAINS] = normalize_domains(update.prescan.upstream_domains)
    if update.copilot:
        if update.copilot.binary is not None:
            values[COPILOT_BINARY] = update.copilot.binary.strip() or None
        for rung, field in COPILOT_MODELS.items():
            model = getattr(update.copilot, f"model_{rung}")
            if model is not None:
                values[field] = normalize_model(model)
        if update.copilot.timeout is not None:
            values[COPILOT_TIMEOUT] = checked_int(COPILOT_TIMEOUT, update.copilot.timeout)
    if update.rework:
        for field, value, reset in (
            (REWORK_ITEM_CAP, update.rework.item_cap, 0),
            (REWORK_JOB_CAP, update.rework.job_cap, 0),
            (REWORK_BUILD_ROUNDS, update.rework.build_rounds, -1),
        ):
            if value is not None:
                values[field] = checked_int(field, value, reset)
        if update.rework.keep_scratch is not None:
            values[REWORK_KEEP_SCRATCH] = "true" if update.rework.keep_scratch else None
    if update.build and update.build.dependents_cap is not None:
        values[BUILD_DEPENDENTS_CAP] = checked_int(BUILD_DEPENDENTS_CAP, update.build.dependents_cap, -1)
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

    # One attempt only: the user is waiting on the button.
    client = DataCenterClient(base_url, pat, transport=request.app.state.http_transport, timeout=15, max_attempts=1, max_rate_limit_attempts=1)
    try:
        with client:
            user = client.myself()
    except JiraAuthError as exc:
        return {"ok": False, "error": str(exc)}
    except JiraError as exc:
        if exc.status is not None and exc.status != 200:
            return {"ok": False, "error": f"Unexpected response (HTTP {exc.status})"}
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "user": user}


@router.post("/copilot/test")
def test_copilot(body: CopilotTestRequest, request: Request) -> dict:
    """Check the token in the environment, then run the CLI as rework would: `--version`, then one short prompt (one premium request) in an empty directory.

    A token GitHub rejects fails the test before the prompt. Without a token the CLI may
    still be logged in (`/login`), so the prompt runs and the result carries the warning.
    Model: the one given, else the saved resolve model, else the CLI's default.
    """
    settings = _config(request).copilot()
    token = check_token(transport=request.app.state.http_transport)
    if token.status == "invalid":
        return {"ok": False, "token": token.to_dict(), "error": token.message}
    client = CopilotClient((body.binary or "").strip() or settings.binary)
    model = normalize_model(body.model or "") or settings.models["resolve"]
    try:
        version = client.version()
    except (OSError, subprocess.SubprocessError) as exc:
        return {"ok": False, "token": token.to_dict(), "error": f"Copilot CLI does not run: {exc}"}
    with tempfile.TemporaryDirectory(prefix="aosp-migrator-copilot-test-") as tmp:
        result = client.run(Path(tmp), COPILOT_TEST_PROMPT, model, COPILOT_TEST_TIMEOUT, tools=("view",))
    if result.status != "ok":
        error = f"Copilot {result.status}: {result.error or f'exit code {result.exit_code}'}"
        if token.status == "missing":
            error = f"{token.message}\n{error}"
        return {"ok": False, "version": version, "token": token.to_dict(), "error": error}
    return {"ok": True, "version": version, "model": model, "reply": result.output, "usage": result.usage, "token": token.to_dict()}
