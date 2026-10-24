from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest
from pydantic import BaseModel
from sqlalchemy import select

from patchmig.agents.adapter import AgentRuntimeError, AgentTask, ToolPolicy, extract_json
from patchmig.agents.copilot import CopilotCliAdapter
from patchmig.agents.fake import FakeAdapter
from patchmig.agents.runner import AgentFailed, AgentRunner, BudgetExceeded
from patchmig.config import load_config
from patchmig.config.schema import BudgetConfig, LlmConfig
from patchmig.ledger import LedgerStore
from patchmig.ledger.models import AgentCall


class Answer(BaseModel):
    value: int


def test_extract_json_variants():
    assert extract_json('x\n```json\n{"a": 1}\n```\ny\n```json\n{"a": 2}\n```') == {"a": 2}
    assert extract_json('```json\n{"a": 1}\n```\n```json\nnot json\n```') == {"a": 1}
    assert extract_json('The answer is {"a": {"b": 3}} done.') == {"a": {"b": 3}}
    with pytest.raises(ValueError):
        extract_json("no json here [1, 2]")


FAKE_COPILOT = """#!{python}
import json, os, sys, time
log = os.environ.get("FAKE_COPILOT_LOG")
if log:
    with open(log, "a") as f:
        f.write(json.dumps({{"argv": sys.argv[1:], "cwd": os.getcwd(),
                            "auto_update": os.environ.get("COPILOT_AUTO_UPDATE"),
                            "token": os.environ.get("COPILOT_GITHUB_TOKEN"),
                            "gh_host": os.environ.get("GH_HOST")}}) + "\\n")
mode = os.environ.get("FAKE_COPILOT_MODE", "answer")
if "--version" in sys.argv:
    print("fake-copilot 1.0"); sys.exit(0)
if mode == "fail":
    print("boom", file=sys.stderr); sys.exit(3)
if mode == "sleep":
    time.sleep(30)
answer = os.environ.get("FAKE_COPILOT_ANSWER", '{{"value": 7}}')
print("Thinking...\\n```json\\n" + answer + "\\n```")
"""


@pytest.fixture
def fake_copilot(tmp_path: Path, monkeypatch):
    exe = tmp_path / "copilot"
    exe.write_text(FAKE_COPILOT.format(python=sys.executable))
    exe.chmod(0o755)
    log = tmp_path / "calls.jsonl"
    monkeypatch.setenv("FAKE_COPILOT_LOG", str(log))

    def calls() -> list[dict]:
        return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []

    return exe, calls


def _task(tmp_path: Path, brief: str = "do it", **kw) -> AgentTask:
    return AgentTask(agent="intent_writer", key="patch:1", brief=brief, schema=Answer,
                     workdir=tmp_path, **kw)  # fmt: skip


def test_copilot_command_shape(tmp_path: Path):
    cfg = LlmConfig(models={"intent_writer": "small-model"}, extra_args=["--log-level=error"])
    adapter = CopilotCliAdapter(cfg, scratch=tmp_path / "briefs")
    tools = ToolPolicy(allow=["patchmig"], mcp_servers={"patchmig": {"command": "x"}})
    cmd = adapter.command(_task(tmp_path, tools=tools), attempt=1)
    assert cmd[:3] == ["copilot", "-p", "do it"]
    assert "-s" in cmd and "--no-ask-user" in cmd
    assert "--model=small-model" in cmd
    assert "--allow-tool=patchmig" in cmd
    assert "--deny-tool=shell, write" in cmd
    i = cmd.index("--additional-mcp-config")
    assert json.loads(cmd[i + 1]) == {"mcpServers": {"patchmig": {"command": "x"}}}
    assert cmd[-1] == "--log-level=error"


def test_long_brief_goes_to_attachment(tmp_path: Path):
    cfg = LlmConfig(inline_prompt_max_chars=1000)
    adapter = CopilotCliAdapter(cfg, scratch=tmp_path / "briefs")
    cmd = adapter.command(_task(tmp_path, brief="x" * 5000), attempt=2)
    attachment = next(a for a in cmd if a.startswith("--attachment="))
    path = Path(attachment.split("=", 1)[1])
    assert path.read_text() == "x" * 5000 and path.name == "intent_writer-patch-1-2.md"
    assert "x" * 5000 not in cmd


def test_copilot_subprocess_success_and_errors(tmp_path: Path, fake_copilot, monkeypatch):
    exe, calls = fake_copilot
    adapter = CopilotCliAdapter(LlmConfig(executable=str(exe), timeout_seconds=10), tmp_path)
    answer = adapter.run(_task(tmp_path), 1)
    assert extract_json(answer.text) == {"value": 7}
    assert calls()[0]["cwd"] == str(tmp_path) and calls()[0]["auto_update"] == "false"

    monkeypatch.setenv("FAKE_COPILOT_MODE", "fail")
    with pytest.raises(AgentRuntimeError, match="exited with 3: boom"):
        adapter.run(_task(tmp_path), 1)

    monkeypatch.setenv("FAKE_COPILOT_MODE", "sleep")
    slow = CopilotCliAdapter(LlmConfig(executable=str(exe), timeout_seconds=10), tmp_path)
    slow.config.timeout_seconds = 1  # below the schema minimum, for test speed
    with pytest.raises(AgentRuntimeError, match="timed out"):
        slow.run(_task(tmp_path), 1)

    missing = CopilotCliAdapter(LlmConfig(executable=str(tmp_path / "nope")), tmp_path)
    with pytest.raises(AgentRuntimeError, match="not found"):
        missing.run(_task(tmp_path), 1)


@pytest.fixture
def runner_env(write_config):
    store = LedgerStore.in_memory()
    run = store.create_run(load_config(write_config()))

    def make(responder, budget: BudgetConfig | None = None, retries: int = 1, model=None):
        llm = LlmConfig(max_retries=retries, credits_per_call={"default": 1.0, "big": 3.0})
        if model:
            llm.models["intent_writer"] = model
        adapter = FakeAdapter(responder)
        return AgentRunner(adapter, store, run, llm, budget or BudgetConfig()), adapter

    return store, run, make


def _calls(store) -> list[AgentCall]:
    with store.session() as s:
        return list(s.scalars(select(AgentCall).order_by(AgentCall.id)))


def test_runner_retries_invalid_answer_with_correction(runner_env, tmp_path):
    store, _, make = runner_env
    runner, adapter = make(lambda t, a: "no json" if a == 1 else {"value": 5})
    outcome = runner.run(_task(tmp_path))
    assert outcome.data.value == 5 and outcome.attempts == 2 and outcome.credits == 2.0
    assert "## Correction" in adapter.calls[1][0].brief
    assert "## Correction" not in adapter.calls[0][0].brief
    rows = _calls(store)
    assert [r.ok for r in rows] == [False, True]
    assert rows[0].error and rows[1].output.endswith("```\n")


def test_runner_gives_up_after_retries(runner_env, tmp_path):
    store, _, make = runner_env
    runner, _ = make(lambda t, a: {"value": "not a number"}, retries=1)
    with pytest.raises(AgentFailed, match="no valid answer"):
        runner.run(_task(tmp_path))
    assert len(_calls(store)) == 2


def test_runtime_error_is_logged_without_credits(runner_env, tmp_path):
    store, _, make = runner_env
    runner, _ = make(lambda t, a: RuntimeError("copilot crashed"))
    with pytest.raises(AgentFailed, match="copilot crashed"):
        runner.run(_task(tmp_path))
    (row,) = _calls(store)
    assert not row.ok and row.credits == 0.0


def test_run_and_cluster_budgets(runner_env, tmp_path):
    _, _, make = runner_env
    runner, _ = make(lambda t, a: {"value": 1}, BudgetConfig(run_credits=2.5), model="big")
    with pytest.raises(BudgetExceeded, match="run budget"):
        runner.run(_task(tmp_path))  # one call costs 3

    runner, _ = make(lambda t, a: {"value": 1}, BudgetConfig(cluster_credits=1.5))
    runner.run(_task(tmp_path, cluster_id=9))
    with pytest.raises(BudgetExceeded, match="cluster 9"):
        runner.run(_task(tmp_path, cluster_id=9))
    runner.run(_task(tmp_path, cluster_id=10))  # other clusters are unaffected
    assert runner.spent() == 2.0 and runner.spent(9) == 1.0


def test_fake_adapter_env_is_not_leaked():
    assert "FAKE_COPILOT_MODE" not in os.environ
