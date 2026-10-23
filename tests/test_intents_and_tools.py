from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest
from mcp.server.mcpserver.exceptions import ToolError as McpToolError
from sqlalchemy import select
from typer.testing import CliRunner

from patchmig.agents.fake import FakeAdapter
from patchmig.agents.intent import IntentResult, write_intents
from patchmig.agents.runner import AgentRunner
from patchmig.cli import app
from patchmig.config import load_config
from patchmig.config.schema import BudgetConfig
from patchmig.ingest import ingest
from patchmig.jira_client import Ticket
from patchmig.ledger import LedgerStore, Patch
from patchmig.mcp_server.server import build_server
from patchmig.mcp_server.tools import (
    ToolContext,
    ToolError,
    file_history,
    find_files,
    get_patch,
    get_ticket,
    list_patches,
    read_file,
    search_code,
)
from tests.fixtures.scenario import BT_NEW, CAR, FRAMEWORKS, POWER
from tests.test_agents import FAKE_COPILOT
from tests.test_ingest import JIRA, FakeJira, make_config  # noqa: F401  (fixture)


def intent_for(task, attempt):
    return IntentResult(
        problem=f"problem of {task.key}",
        change="change",
        verification="verify",
        components=["X"],
        sources=["commit_message", "diff"],
        confidence=0.8,
    ).model_dump()


@pytest.fixture
def ingested(make_config):  # noqa: F811
    path = make_config([{"type": "git_range"}], jira=JIRA)
    cfg = load_config(path)
    store = LedgerStore.open(cfg.ledger_path)
    run = store.create_run(cfg)
    ingest(
        cfg,
        store,
        run,
        jira=FakeJira({"OEM-101": Ticket("OEM-101", "OEM power mode", "For OEM sleep")}),
    )
    return path, cfg, store, run


def _patches(store, run):
    with store.session() as s:
        return s.scalars(select(Patch).where(Patch.run_id == run.id).order_by(Patch.seq)).all()


def test_intents_written_then_reused_in_next_run(ingested):
    _, cfg, store, run = ingested
    adapter = FakeAdapter(intent_for)
    report = write_intents(cfg, store, run, AgentRunner(adapter, store, run, cfg.llm, cfg.budget))
    assert (report.written, report.reused, report.credits) == (8, 0, 8.0)
    first_brief = adapter.calls[0][0].brief
    assert "OEM power mode" in first_brief and "For OEM sleep" in first_brief
    assert "+    public boolean isOemMode()" in first_brief
    assert '"confidence"' in first_brief  # answer schema included
    p = _patches(store, run)[0]
    assert p.intent.startswith("Problem: problem of patch:")
    assert p.intent_detail["sources"] == ["commit_message", "diff"]

    run2 = store.create_run(cfg)
    ingest(cfg, store, run2)
    adapter2 = FakeAdapter(intent_for)
    report2 = write_intents(
        cfg, store, run2, AgentRunner(adapter2, store, run2, cfg.llm, cfg.budget)
    )
    assert (report2.written, report2.reused) == (0, 8)
    assert adapter2.calls == []
    assert _patches(store, run2)[0].intent_detail["reused_from_patch"] == p.id


def test_intents_limit_budget_and_failures(ingested):
    _, cfg, store, run = ingested
    runner = AgentRunner(FakeAdapter(intent_for), store, run, cfg.llm, cfg.budget)
    assert write_intents(cfg, store, run, runner, limit=3).written == 3
    assert write_intents(cfg, store, run, None).written == 0  # dry run: no agent

    capped = AgentRunner(FakeAdapter(intent_for), store, run, cfg.llm, BudgetConfig(run_credits=4))
    report = write_intents(cfg, store, run, capped)
    assert report.written == 1 and report.skipped == 3
    assert "run budget" in report.stopped_by_budget

    broken = AgentRunner(FakeAdapter(lambda t, a: "nope"), store, run, cfg.llm, cfg.budget)
    report = write_intents(cfg, store, run, broken)
    assert len(report.failed) == 4 and report.written == 0


@pytest.fixture
def ctx(ingested):
    _, cfg, store, run = ingested
    return ToolContext(cfg, store, run.id)


def test_tools_read_the_right_tree_versions(ctx):
    target_hits = search_code(ctx, "target", r"void setMode\(")
    assert target_hits == [
        f"{FRAMEWORKS}/{POWER}:6:    public void setMode(int mode, int reason) {{"
    ]
    source_hits = search_code(ctx, "source", "isOemMode", fixed=True)
    assert {h.split("/")[0] for h in source_hits} == {"frameworks", "packages"}
    assert search_code(ctx, "target", "isOemMode", fixed=True) == []
    assert find_files(ctx, "target", "BtManager") == [f"{FRAMEWORKS}/{BT_NEW}"]

    text = read_file(ctx, "target", FRAMEWORKS, POWER, start=6, end=7)
    assert text.splitlines()[0] == "    6      public void setMode(int mode, int reason) {"
    with pytest.raises(ToolError, match="does not exist"):
        read_file(ctx, "target", CAR, "service/src/com/android/car/LegacyAudio.java")
    with pytest.raises(ToolError, match="unknown project"):
        read_file(ctx, "target", "vendor/x", "a")

    history = file_history(ctx, "target", FRAMEWORKS, POWER, 6, 6)
    assert "record the reason for mode changes" in history


def test_ledger_tools_are_scoped_to_run(ctx, ingested):
    _, cfg, store, run = ingested
    listed = list_patches(ctx)
    assert len(listed) == 8 and listed[0]["subject"] == "Add OEM power mode"
    assert get_patch(ctx, listed[0]["id"])["jira_keys"] == ["OEM-101"]
    other = ToolContext(cfg, store, run.id + 1)
    with pytest.raises(ToolError, match="not part of run"):
        get_patch(other, listed[0]["id"])
    assert get_ticket(ctx, "OEM-101")["summary"] == "OEM power mode"
    with pytest.raises(ToolError, match="not in the cache"):
        get_ticket(ctx, "OEM-9")


def test_mcp_server_exposes_tools(ctx):
    server = build_server(ctx)

    async def go():
        names = {t.name for t in await server.list_tools()}
        assert names == {"get_patch", "list_patches", "get_ticket", "search_code", "find_files",
                         "read_file", "file_history"}  # fmt: skip
        result = await server.call_tool("find_files", {"tree": "target", "name": "BtManager"})
        assert BT_NEW in str(result)
        with pytest.raises(McpToolError, match="unknown project"):
            await server.call_tool("read_file", {"tree": "source", "project": "x", "path": "y"})

    asyncio.run(go())


def _with_fake_copilot(path: Path, tmp_path: Path) -> Path:
    exe = tmp_path / "copilot"
    exe.write_text(FAKE_COPILOT.format(python=sys.executable))
    exe.chmod(0o755)
    text = path.read_text() + f"llm:\n  executable: {exe}\n  timeout_seconds: 30\n"
    path.write_text(text)
    return path


def test_cli_doctor_and_intents(make_config, tmp_path, monkeypatch):  # noqa: F811
    path = _with_fake_copilot(make_config([{"type": "git_range"}]), tmp_path)
    runner = CliRunner()
    monkeypatch.setenv("FAKE_COPILOT_ANSWER", '{"ok": true, "sum": 42}')
    result = runner.invoke(app, ["doctor", "-c", str(path)])
    assert result.exit_code == 0, result.output
    assert "fake-copilot 1.0" in result.output and "ok   plain JSON answer" in result.output

    monkeypatch.setenv("FAKE_COPILOT_ANSWER", '{"ok": true, "sum": 41}')
    result = runner.invoke(app, ["doctor", "-c", str(path)])
    assert result.exit_code == 1 and "WRONG plain JSON answer" in result.output

    assert runner.invoke(app, ["ingest", "-c", str(path), "--no-jira"]).exit_code == 0
    monkeypatch.setenv(
        "FAKE_COPILOT_ANSWER",
        '{"problem": "p", "change": "c", "verification": "v", "components": [], '
        '"sources": ["diff"], "confidence": 0.5}',
    )
    result = runner.invoke(app, ["intents", "-c", str(path), "--limit", "2"])
    assert result.exit_code == 0, result.output
    assert "2 written, 0 reused" in result.output
    sheet = tmp_path / "review.md"
    result = runner.invoke(app, ["intents", "-c", str(path), "--dry-run", "--export", str(sheet)])
    assert "0 written, 0 reused, 2 already had one" in result.output
    text = sheet.read_text()
    assert "Agent calls: 2 (2 valid), estimated credits 2.0" in text
    assert "## 1. Add OEM power mode" in text and text.count("- [ ] Correct and useful") == 2


def test_mcp_server_over_real_stdio(ingested):
    """The exact entry the adapter hands to Copilot CLI starts a working server."""
    import os

    from mcp.client.session import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client

    from patchmig.mcp_server.server import server_config

    path, _, _, run = ingested
    entry = server_config(str(path), run.id)
    params = StdioServerParameters(
        command=entry["command"], args=entry["args"], env={**os.environ, **entry["env"]}
    )

    async def go():
        async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool("list_patches", {})
            assert "Add OEM power mode" in str(result) and not result.is_error
            result = await session.call_tool(
                "read_file", {"tree": "source", "project": "nope", "path": "x"}
            )
            assert result.is_error and "unknown project" in str(result)

    asyncio.run(asyncio.wait_for(go(), timeout=60))
