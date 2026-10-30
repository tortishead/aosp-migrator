import json
import subprocess
import threading
import time
from pathlib import Path

import pytest
from fake_copilot import FAKE_COPILOT
from fastapi.testclient import TestClient

from aosp_migrator.app import create_app
from aosp_migrator.config import COPILOT_TIMEOUT, REWORK_ITEM_CAP, Config
from aosp_migrator.copilot.blocks import Block, BlockError, apply_blocks, parse_blocks
from aosp_migrator.copilot.client import EDIT_TOOLS, CopilotClient, command


@pytest.fixture
def log(tmp_path: Path) -> Path:
    return tmp_path / "copilot.jsonl"


@pytest.fixture
def client(log: Path) -> CopilotClient:
    return CopilotClient(str(FAKE_COPILOT), env={"FAKE_COPILOT_LOG": str(log)}, kill_grace=1)


@pytest.fixture
def scratch(tmp_path: Path) -> Path:
    """A checkout to edit, next to a target tree Copilot may only read."""
    work = tmp_path / "work"
    work.mkdir()
    subprocess.run(["git", "init", "-q", str(work)], check=True)
    (work / "a.c").write_text("int a = 1;\n")
    (tmp_path / "target").mkdir()
    (tmp_path / "target" / "t.c").write_text("secret\n")
    return work


def calls(log: Path) -> list[dict]:
    return [json.loads(line) for line in log.read_text().splitlines()]


def test_command_limits_tools_and_writes(tmp_path: Path) -> None:
    args = command("copilot", tmp_path / "work", "gpt-x", [tmp_path / "target"], tmp_path / "usage.json")
    work, target = (tmp_path / "work").resolve(), (tmp_path / "target").resolve()
    assert args[:2] == ["copilot", "-s"]
    for flag in ("--no-auto-update", "--no-custom-instructions", "--no-ask-user", "--disable-builtin-mcps", "--disallow-temp-dir"):
        assert flag in args
    tools = args.index("--available-tools")
    assert args[tools + 1 : tools + 1 + len(EDIT_TOOLS)] == list(EDIT_TOOLS)
    assert args[tools + 1 + len(EDIT_TOOLS)].startswith("--")
    assert "--allow-all-tools" not in args and "--allow-all" not in args and "--yolo" not in args
    assert ["--allow-tool", "write"] == args[args.index("--allow-tool") : args.index("--allow-tool") + 2]
    assert f"write({work}/.git/**)" in args and f"write({target}/**)" in args
    assert ["--add-dir", str(target)] == args[args.index("--add-dir") : args.index("--add-dir") + 2]
    assert args[-2:] == ["--model", "gpt-x"]
    assert "--model" not in command("copilot", tmp_path, None, [], tmp_path / "u.json")


def test_run_edits_in_the_checkout_and_reports_usage(client: CopilotClient, scratch: Path, log: Path) -> None:
    result = client.run(scratch, "Fix it @@fake:edit@@", "gpt-x", 30, read_dirs=[scratch.parent / "target"])
    assert (result.status, result.exit_code, result.output, result.error) == ("ok", 0, "Changed a.c: a is 2 now.", "")
    assert (scratch / "a.c").read_text() == "int a = 2;\n"
    assert (scratch / "sub/new.c").read_text() == "int n;\n"
    assert result.usage is not None and result.usage["modelMetrics"]["gpt-x"]["requests"]["count"] == 1
    (call,) = calls(log)
    # The prompt comes on stdin, the cwd is the checkout.
    assert call["prompt"] == "Fix it @@fake:edit@@" and Path(call["cwd"]) == scratch.resolve()


def test_writes_outside_the_checkout_are_refused(client: CopilotClient, scratch: Path) -> None:
    result = client.run(scratch, "@@fake:escape@@", None, 30, read_dirs=[scratch.parent / "target"])
    assert result.status == "ok"
    assert (scratch.parent / "target/t.c").read_text() == "secret\n"
    assert not (scratch / ".git/hooks/post-checkout").exists()
    assert "refused: ../target/t.c" in result.output and "refused: .git/hooks/post-checkout" in result.output


def test_large_prompt(client: CopilotClient, scratch: Path, log: Path) -> None:
    prompt = "x" * 500_000
    assert client.run(scratch, prompt, None, 30).status == "ok"
    assert calls(log)[0]["prompt"] == prompt


def test_non_zero_exit(client: CopilotClient, scratch: Path) -> None:
    result = client.run(scratch, "@@fake:fail@@", None, 30)
    assert (result.status, result.exit_code, result.error, result.usage) == ("failed", 1, "Error: model not available", None)


def test_timeout_kills_it(client: CopilotClient, scratch: Path) -> None:
    started = time.monotonic()
    result = client.run(scratch, "@@fake:slow@@", None, 1)
    assert time.monotonic() - started < 10
    assert result.status == "timeout" and result.error.startswith("timed out after 1 s")
    assert result.exit_code is not None and result.exit_code < 0


def test_cancel_kills_it(client: CopilotClient, scratch: Path) -> None:
    cancel = threading.Event()
    threading.Timer(0.5, cancel.set).start()
    started = time.monotonic()
    result = client.run(scratch, "@@fake:slow@@", None, 60, cancel=cancel.is_set)
    assert time.monotonic() - started < 10
    assert (result.status, result.output) == ("canceled", "")


def test_missing_binary(scratch: Path) -> None:
    result = CopilotClient(str(scratch / "nope")).run(scratch, "hi", None, 30)
    assert result.status == "failed" and result.error.startswith("Copilot CLI did not start:")


def test_blocks_from_an_answer_apply(client: CopilotClient, scratch: Path) -> None:
    result = client.run(scratch, "@@fake:blocks@@", None, 30)
    assert apply_blocks(scratch, parse_blocks(result.output)) == ["a.c"]
    assert (scratch / "a.c").read_text() == "int a = 2;\n"


ANSWER = """Two changes.

a.c
<<<<<<< SEARCH
int a = 1;
=======
int a = 2;
>>>>>>> REPLACE

a.c
```c
<<<<<<< SEARCH
int b = 1;
=======
int b = 3;
>>>>>>> REPLACE
```

new/b.h
<<<<<<< SEARCH
=======
#define B 1
>>>>>>> REPLACE
"""


def test_several_blocks_per_file(tmp_path: Path) -> None:
    (tmp_path / "a.c").write_text("int a = 1;\nint b = 1;\n")
    blocks = parse_blocks(ANSWER)
    assert [b.path for b in blocks] == ["a.c", "a.c", "new/b.h"]
    assert apply_blocks(tmp_path, blocks) == ["a.c", "new/b.h"]
    assert (tmp_path / "a.c").read_text() == "int a = 2;\nint b = 3;\n"
    assert (tmp_path / "new/b.h").read_text() == "#define B 1\n"


@pytest.mark.parametrize(
    ("blocks", "error"),
    [
        ([Block("a.c", "int a = 1;\n", "x\n"), Block("a.c", "int c = 1;\n", "y\n")], "block 2 (a.c): SEARCH not found"),
        ([Block("a.c", "int ", "x")], "block 1 (a.c): SEARCH found 2 times"),
        ([Block("gone.c", "int a", "x")], "block 1 (gone.c): file does not exist"),
        ([Block("a.c", "", "x")], "block 1 (a.c): empty SEARCH for an existing file"),
        ([Block("../t.c", "", "x")], "not a path in the checkout: ../t.c"),
        ([Block(".git/config", "", "x")], "not a path in the checkout: .git/config"),
        ([], "no search/replace blocks in the answer"),
    ],
)
def test_block_that_does_not_match_writes_nothing(tmp_path: Path, blocks: list[Block], error: str) -> None:
    (tmp_path / "a.c").write_text("int a = 1;\nint b = 1;\n")
    with pytest.raises(BlockError, match=error.replace("(", r"\(").replace(")", r"\)")):
        apply_blocks(tmp_path, blocks)
    assert (tmp_path / "a.c").read_text() == "int a = 1;\nint b = 1;\n"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["a.c"]


def test_block_through_a_symlink_out_of_the_checkout_is_refused(tmp_path: Path) -> None:
    (tmp_path / "work").mkdir()
    (tmp_path / "t.c").write_text("secret\n")
    (tmp_path / "work/link.c").symlink_to(tmp_path / "t.c")
    with pytest.raises(BlockError, match="not a path in the checkout: link.c"):
        apply_blocks(tmp_path / "work", [Block("link.c", "secret\n", "x\n")])


@pytest.fixture
def api(config: Config) -> TestClient:
    return TestClient(create_app(config))


def test_copilot_settings_defaults_and_update(api: TestClient, config: Config) -> None:
    body = api.get("/api/settings").json()
    assert body["copilot"] == {
        "binary": {"source": "unset", "value": "copilot"},
        "model_resolve": {"source": "unset", "value": None},
        "model_port": {"source": "unset", "value": None},
        "model_reimpl": {"source": "unset", "value": None},
        "timeout": {"source": "unset", "value": 900},
    }
    assert body["rework"] == {
        "item_cap": {"source": "unset", "value": 5},
        "job_cap": {"source": "unset", "value": 100},
        "build_rounds": {"source": "unset", "value": 2},
        "keep_scratch": {"source": "unset", "value": False},
    }
    update = {
        "copilot": {"binary": " /opt/copilot ", "model_resolve": "gpt-5-mini", "model_port": " ", "timeout": 600},
        "rework": {"item_cap": 3, "build_rounds": 0, "keep_scratch": True},
    }
    body = api.put("/api/settings", json=update).json()
    assert body["copilot"]["binary"] == {"source": "file", "value": "/opt/copilot"}
    assert body["copilot"]["model_resolve"] == {"source": "file", "value": "gpt-5-mini"}
    assert body["copilot"]["model_port"] == {"source": "unset", "value": None}
    settings = config.copilot()
    assert (settings.timeout, settings.item_cap, settings.job_cap, settings.build_rounds, settings.keep_scratch) == (600, 3, 100, 0, True)
    # Back to the defaults.
    api.put("/api/settings", json={"copilot": {"binary": "", "timeout": 0}, "rework": {"item_cap": 0, "build_rounds": -1, "keep_scratch": False}})
    settings = config.copilot()
    assert (settings.binary, settings.timeout, settings.item_cap, settings.build_rounds, settings.keep_scratch) == ("copilot", 900, 5, 2, False)


@pytest.mark.parametrize(
    ("update", "detail"),
    [
        ({"copilot": {"model_resolve": "gpt x"}}, "Not a model name: gpt x"),
        ({"copilot": {"timeout": 5}}, "timeout must be between 30 and 7200"),
        ({"rework": {"item_cap": 51}}, "item_cap must be between 1 and 50"),
        ({"rework": {"build_rounds": 11}}, "build_rounds must be between 0 and 10"),
    ],
)
def test_copilot_settings_validation(api: TestClient, config: Config, update: dict, detail: str) -> None:
    response = api.put("/api/settings", json=update)
    assert (response.status_code, response.json()["detail"]) == (422, detail)
    assert config.copilot() == Config(config.data_dir).copilot()
    assert config.get(COPILOT_TIMEOUT) is None and config.get(REWORK_ITEM_CAP) is None


def test_bad_stored_number_falls_back_to_the_default(config: Config) -> None:
    config.set({COPILOT_TIMEOUT: "soon", REWORK_ITEM_CAP: "500"})
    assert (config.copilot().timeout, config.copilot().item_cap) == (900, 5)


def test_test_copilot(api: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    log = tmp_path / "test.jsonl"
    monkeypatch.setenv("FAKE_COPILOT_LOG", str(log))
    api.put("/api/settings", json={"copilot": {"binary": str(FAKE_COPILOT), "model_resolve": "gpt-5-mini"}})
    body = api.post("/api/settings/copilot/test", json={}).json()
    assert {k: body[k] for k in ("ok", "version", "model", "reply")} == {"ok": True, "version": "1.0.105-fake", "model": "gpt-5-mini", "reply": "OK"}
    assert body["usage"]["totalPremiumRequestCost"] == 1
    (call,) = calls(log)
    # No file tools that write.
    tools = call["argv"].index("--available-tools")
    assert call["argv"][tools + 1 : tools + 3] == ["view", "--allow-tool"]

    # Unsaved values from the form.
    body = api.post("/api/settings/copilot/test", json={"binary": str(tmp_path / "nope")}).json()
    assert body["ok"] is False and body["error"].startswith("Copilot CLI does not run:")
    assert api.post("/api/settings/copilot/test", json={"model": "other"}).json()["model"] == "other"


def test_test_copilot_reports_a_failing_prompt(api: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    scenarios = tmp_path / "scenarios"
    scenarios.mkdir()
    monkeypatch.setenv("FAKE_COPILOT_SCENARIOS", str(scenarios))
    # The test prompt carries no marker; a fake that fails every prompt stands in for a missing login.
    fake = tmp_path / "copilot"
    fake.write_text("#!/bin/sh\nif [ \"$1\" = --version ]; then echo 1.0; exit 0; fi\necho 'Error: not logged in' >&2\nexit 1\n")
    fake.chmod(0o755)
    body = api.post("/api/settings/copilot/test", json={"binary": str(fake)}).json()
    assert body == {"ok": False, "version": "1.0", "error": "Copilot failed: Error: not logged in"}
