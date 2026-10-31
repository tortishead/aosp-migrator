from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from aosp_migrator.app import create_app
from aosp_migrator.config import Config
from aosp_migrator.copilot.usage import ai_credits, model_metrics, premium_requests
from aosp_migrator.db.models import Item, JiraIssue, ReworkAttempt, Workspace, utcnow


def usage(model: str, requests: int, tokens_in: int, tokens_out: int, premium: float = 1, nano_aiu: int = 0) -> dict[str, Any]:
    return {
        "totalPremiumRequestCost": premium,
        "totalNanoAiu": nano_aiu,
        "modelMetrics": {model: {"requests": {"count": requests, "cost": premium}, "usage": {"inputTokens": tokens_in, "outputTokens": tokens_out}, "totalNanoAiu": nano_aiu}},
    }


def call(rung: str, status: str = "ok", model: str | None = None, u: dict[str, Any] | None = None, duration: float = 10.0) -> dict[str, Any]:
    return {"project": "core", "rung": rung, "round": 0, "build_round": 0, "model": model, "status": status, "usage": u, "duration": duration}


@pytest.fixture
def app(config: Config, static_dir: Path) -> FastAPI:
    return create_app(config, static_dir=static_dir)


@pytest.fixture
def client(app: FastAPI) -> TestClient:
    return TestClient(app)


def add_workspace(app: FastAPI, name: str, key: str) -> tuple[int, int]:
    with app.state.sessionmaker() as s:
        ws = Workspace(name=name, jql="project = PROJ", target_tree="/tmp/t", target_tree_type="repo")
        s.add(ws)
        s.flush()
        issue = JiraIssue(
            workspace_id=ws.id, key=key, jira_id="1", summary="s", description_raw="", description_html="", status="Open",
            labels=[], components=[], fix_versions=[], issue_links=[], jira_updated=utcnow(),
        )  # fmt: skip
        s.add(issue)
        s.flush()
        item = Item(workspace_id=ws.id, issue_id=issue.id)
        s.add(item)
        s.commit()
        return ws.id, item.id


def add_attempt(app: FastAPI, workspace_id: int, item_id: int, calls: list[dict[str, Any]], age_days: int = 0) -> None:
    with app.state.sessionmaker() as s:
        s.add(ReworkAttempt(workspace_id=workspace_id, item_ids=[item_id], calls=calls, started_at=utcnow() - timedelta(days=age_days)))
        s.commit()


def test_reads_the_cli_usage() -> None:
    u = usage("claude-sonnet-4.5", 3, 1834, 42)
    assert premium_requests(u) == 1
    assert [(m.model, m.requests, m.input_tokens, m.output_tokens) for m in model_metrics(u)] == [("claude-sonnet-4.5", 3, 1834, 42)]
    assert model_metrics(None) == [] and premium_requests({"totalPremiumRequestCost": "x"}) == 0
    assert [(m.requests, m.input_tokens) for m in model_metrics({"modelMetrics": {"x": "nope"}})] == [(0, 0)]


def test_reads_the_ai_credits() -> None:
    assert ai_credits({"totalNanoAiu": 2_500_000_000}) == 2.5
    assert ai_credits({"copilotUsage": {"totalNanoAiu": 1_000_000_000}}) == 1
    # Only per model: summed.
    per_model = {"modelMetrics": {"a": {"totalNanoAiu": 1_000_000_000}, "b": {"copilotUsage": {"totalNanoAiu": 500_000_000}}}}
    assert ai_credits(per_model) == 1.5
    assert [m.ai_credits for m in model_metrics(per_model)] == [1, 0.5]
    # A premium-request CLI reports none.
    assert ai_credits(usage("gpt-5", 1, 1, 1) | {"totalNanoAiu": None, "modelMetrics": {}}) is None
    assert ai_credits(None) is None and ai_credits({"totalNanoAiu": "x"}) is None


def test_empty(client: TestClient) -> None:
    body = client.get("/api/copilot/usage").json()
    assert body["totals"]["calls"] == 0 and body["totals"]["attempts"] == 0
    assert body["by_rung"] == body["by_day"] == body["by_model"] == body["recent"] == []


def test_totals_and_breakdowns(app: FastAPI, client: TestClient) -> None:
    a15, item1 = add_workspace(app, "A15", "PROJ-1")
    a16, item2 = add_workspace(app, "A16", "PROJ-2")
    add_attempt(app, a15, item1, [call("resolve", u=usage("gpt-5", 2, 1000, 100, nano_aiu=1_250_000_000)), call("resolve", "failed", duration=900.0)])
    add_attempt(app, a16, item2, [call("port", model="claude-sonnet-4.5", u=usage("claude-sonnet-4.5", 5, 3000, 300, premium=1.5, nano_aiu=3_000_000_000))])
    # Older than the period.
    add_attempt(app, a15, item1, [call("reimpl", u=usage("gpt-5", 1, 10, 1))], age_days=40)
    # No Copilot call: not counted.
    add_attempt(app, a15, item1, [])

    body = client.get("/api/copilot/usage", params={"days": 30}).json()
    assert body["totals"] == {
        "calls": 3, "ok": 2, "failed": 1, "canceled": 0, "reported": 2, "premium_requests": 2.5, "ai_credits": 4.25,
        "input_tokens": 4000, "output_tokens": 400, "duration": 920.0, "attempts": 2,
    }  # fmt: skip
    assert [(r["rung"], r["calls"], r["premium_requests"]) for r in body["by_rung"]] == [("resolve", 2, 1), ("port", 1, 1.5)]
    assert [(m["model"], m["calls"], m["requests"], m["ai_credits"], m["input_tokens"]) for m in body["by_model"]] == [
        ("claude-sonnet-4.5", 1, 5, 3, 3000),
        ("gpt-5", 1, 2, 1.25, 1000),
    ]
    assert [(m["model"], m["calls"]) for m in body["by_requested_model"]] == [(None, 2), ("claude-sonnet-4.5", 1)]
    assert [(w["name"], w["calls"]) for w in body["by_workspace"]] == [("A15", 2), ("A16", 1)]
    assert [d["calls"] for d in body["by_day"]] == [3]
    newest = body["recent"][0]
    assert (newest["workspace"], newest["keys"], newest["rung"], newest["served_models"], newest["input_tokens"]) == ("A16", ["PROJ-2"], "port", ["claude-sonnet-4.5"], 3000)
    assert newest["ai_credits"] == 3
    unreported = next(r for r in body["recent"] if r["status"] == "failed")
    assert (unreported["premium_requests"], unreported["ai_credits"], unreported["input_tokens"]) == (None, None, None)

    assert client.get("/api/copilot/usage").json()["totals"]["calls"] == 4
    only = client.get("/api/copilot/usage", params={"workspace_id": a16}).json()
    assert (only["totals"]["calls"], only["totals"]["premium_requests"]) == (1, 1.5)
    assert client.get("/api/copilot/usage", params={"workspace_id": 999}).status_code == 404
    assert client.get("/api/copilot/usage", params={"days": 0}).status_code == 422
