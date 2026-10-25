from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest
from mcp.server.mcpserver.exceptions import ToolError as McpToolError
from sqlalchemy import select
from typer.testing import CliRunner

from patchmig.agents.analyst import analyse_items
from patchmig.agents.fake import FakeAdapter
from patchmig.agents.runner import AgentRunner
from patchmig.analysis import analyse
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
from tests.fixtures.analyst import analysis_for, analyst
from tests.fixtures.scenario import BT_NEW, CAR, EXPECTED, FRAMEWORKS, POWER
from tests.test_agents import FAKE_COPILOT
from tests.test_ingest import JIRA, FakeJira, make_config  # noqa: F401  (fixture)

N = len(EXPECTED)


# Items Copilot is asked about: those with a patch the checks left needed (P1+P2, P3, P8).
ASKED = ["OEM-101", "OEM-202", "OEM-606"]
intent_for = analysis_for


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


def _runner(store, run, cfg, respond, budget=None):
    return AgentRunner(FakeAdapter(respond), store, run, cfg.llm, budget or cfg.budget)


def test_analyses_written_then_reused_in_next_run(ingested):
    _, cfg, store, run = ingested
    analyse(cfg, store, run)
    adapter = FakeAdapter(analysis_for)
    report = analyse_items(cfg, store, run, AgentRunner(adapter, store, run, cfg.llm, cfg.budget))
    assert (report.analysed, report.reused, report.credits) == (3, 0, 3.0)
    assert report.not_required == 5  # every patch dropped by the checks: not worth credits
    assert [t.key for t, _ in adapter.calls] == [f"item:{k}" for k in ASKED]
    first_brief = adapter.calls[0][0].brief
    assert "OEM power mode" in first_brief and "For OEM sleep" in first_brief
    assert "+    public boolean isOemMode()" in first_brief
    assert '"relevance"' in first_brief  # answer schema included
    assert "- OEM-606: " in first_brief  # P8 uses P1's isOemMode: a related candidate
    p = _patches(store, run)[0]
    assert p.intent.startswith(f"Problem: problem of #{p.id}")
    assert "Solution: cherry pick: solution for OEM-101" in p.intent
    assert p.intent_detail["item"] == "OEM-101" and p.intent_detail["confidence"] == 0.8

    # Analysing again: nothing new, nothing asked.
    again = analyse_items(cfg, store, run, _runner(store, run, cfg, analysis_for))
    assert (again.analysed, again.current) == (0, 3)

    run2 = store.create_run(cfg)
    ingest(cfg, store, run2)
    analyse(cfg, store, run2)
    adapter2 = FakeAdapter(analysis_for)
    report2 = analyse_items(cfg, store, run2, AgentRunner(adapter2, store, run2, cfg.llm,
                                                          cfg.budget))  # fmt: skip
    assert (report2.analysed, report2.reused) == (0, 3) and adapter2.calls == []
    p2 = _patches(store, run2)[0]
    assert p2.intent.startswith(f"Problem: problem of #{p.id}")  # the same understanding
    assert p2.intent_detail["item"] == "OEM-101"


def test_analyses_limit_budget_and_failures(ingested):
    _, cfg, store, run = ingested
    analyse(cfg, store, run)
    assert analyse_items(cfg, store, run, _runner(store, run, cfg, analysis_for),
                         limit=1).analysed == 1  # fmt: skip
    capped = _runner(store, run, cfg, analysis_for, BudgetConfig(run_credits=2))
    report = analyse_items(cfg, store, run, capped)
    assert report.analysed == 1 and report.current == 1
    assert "run budget" in report.stopped_by_budget

    broken = _runner(store, run, cfg, lambda t, a: "nope")
    report = analyse_items(cfg, store, run, broken, keys=["OEM-101"])
    assert len(report.failed) == 1 and report.analysed == 0
    asked: list = []
    report = analyse_items(cfg, store, run, _runner(store, run, cfg, analyst(asked=asked)),
                           keys=["OEM-505"])  # fmt: skip
    assert report.analysed == 1 and [t.key for t in asked] == ["item:OEM-505"]  # asked for


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


def test_tools_read_the_manifest_once_per_tree(ctx, monkeypatch):
    import patchmig.mcp_server.tools as tools

    reads = []
    real = tools.tree_projects
    monkeypatch.setattr(tools, "tree_projects", lambda *a: reads.append(a) or real(*a))
    for _ in range(3):
        search_code(ctx, "target", "class", fixed=True)
        find_files(ctx, "source", "Power")
    assert len(reads) == 2  # source and target, not once per project and call


def test_ledger_tools_are_scoped_to_run(ctx, ingested):
    _, cfg, store, run = ingested
    listed = list_patches(ctx)
    assert len(listed) == N and listed[0]["subject"] == "Add OEM power mode"
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
        assert names == {"get_patch", "list_patches", "get_cluster", "get_ticket", "upstream_fate",
                         "search_code", "find_files", "read_file", "file_history"}  # fmt: skip
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


def test_cli_doctor_and_analyse(make_config, tmp_path, monkeypatch):  # noqa: F811
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
    answer = {"understanding": "u", "relevance": "needed", "relevance_reason": "r",
              "solution": {"approach": "adapt", "summary": "s"}, "sources": ["diff"],
              "confidence": 0.5}  # fmt: skip
    monkeypatch.setenv("FAKE_COPILOT_ANSWER", json.dumps(answer))
    result = runner.invoke(app, ["analyse", "-c", str(path), "--limit", "2"])
    assert result.exit_code == 0, result.output
    assert "Copilot analysis: 2 items analysed" in result.output
    sheet = tmp_path / "review.md"
    result = runner.invoke(app, ["analyse", "-c", str(path), "--no-agent", "--export", str(sheet)])
    assert "Copilot analysis skipped" in result.output
    text = sheet.read_text()
    assert "Agent calls: 2 (2 valid), estimated credits 2.0" in text
    assert "## OEM-101: OEM power mode" not in text  # no Jira: titled by the first patch
    assert "## OEM-101: Add OEM power mode" in text and text.count("- [ ] Correct and useful") == 2
