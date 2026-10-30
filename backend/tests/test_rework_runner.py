import json
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from fake_copilot import FAKE_COPILOT
from fake_jira import FakeJiraServer, FakeJiraStore
from fake_jira.build import write_envsetup
from fake_jira.rework import CORE, PROPS, REGION, RESOLVED, rework_store
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from test_build_runner import item
from test_build_runner import runner as build_runner
from test_import import session
from test_prescan_api import app, client, run_prescan, target, workspace_id  # noqa: F401
from test_tree import snapshot

from aosp_migrator.build.runner import BuildJobConflict
from aosp_migrator.config import Config
from aosp_migrator.copilot.client import CopilotClient
from aosp_migrator.db.models import BuildJob, BuildRun, HistoryEvent, MigrationPatch, ReworkAttempt, ReworkJob, Workspace
from aosp_migrator.prescan.runner import JobConflictError
from aosp_migrator.rework.runner import Options, ReworkJobConflict, ReworkRunner, error_symbols, fail_interrupted_jobs, missing_dependency

LUNCH = "aosp_x86_64-eng"
SETTINGS_STRINGS = "res/values/oem_strings.xml"
BUILD_ERROR = "packages/apps/Settings/res/values/oem_strings.xml:3: error: FAIL_BUILD marker"
# Copilot takes the failure marker out of PROJ-102's new resource file.
FIX_BUILD = {"reply": f"{SETTINGS_STRINGS}: dropped the marker", "edits": [{"path": SETTINGS_STRINGS, "old": "FAIL_BUILD", "new": "Screen brightness"}]}
RESOLVE = {"reply": f"{PROPS}: mode 1, kept level 5", "edits": [{"path": PROPS, "old": REGION, "new": RESOLVED}]}


@pytest.fixture
def fake_jira(fake_jira_server: FakeJiraServer, target: Path) -> FakeJiraStore:
    fake_jira_server.store = rework_store(target)
    return fake_jira_server.store


@pytest.fixture
def workspace(app: FastAPI, client: TestClient, workspace_id: int, target: Path) -> int:
    """Imported, pre-scanned, with the fake envsetup and a lunch target."""
    write_envsetup(target)
    with session(app) as s:
        ws = s.get(Workspace, workspace_id)
        assert ws is not None
        ws.lunch_target = LUNCH
        s.commit()
    assert run_prescan(client, workspace_id)["status"] == "done"
    return workspace_id


@pytest.fixture
def scenarios(tmp_path: Path) -> Path:
    directory = tmp_path / "scenarios"
    directory.mkdir()
    return directory


@pytest.fixture
def log(tmp_path: Path) -> Path:
    return tmp_path / "copilot.jsonl"


def scenario(directory: Path, key: str, *rounds: dict[str, Any]) -> None:
    """The fake Copilot's answer for calls about `key` (one, or one per call)."""
    (directory / f"{key}.json").write_text(json.dumps(rounds[0] if len(rounds) == 1 else {"rounds": list(rounds)}))


def calls(log: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []


def make_runner(app: FastAPI, config: Config, scenarios: Path, log: Path, executor: Callable[[Callable[[], None]], None] = lambda run: run()) -> ReworkRunner:
    env = {"FAKE_COPILOT_SCENARIOS": str(scenarios), "FAKE_COPILOT_LOG": str(log)}
    return ReworkRunner(app.state.sessionmaker, config, build_runner(app, config), lambda _: CopilotClient(str(FAKE_COPILOT), env=env, kill_grace=1), executor)


@pytest.fixture
def rework(app: FastAPI, config: Config, scenarios: Path, log: Path) -> ReworkRunner:
    """Runs jobs in the calling thread; the API uses it too."""
    app.state.rework_runner = make_runner(app, config, scenarios, log)
    return app.state.rework_runner


def start(client: TestClient, workspace: int, keys: list[str], **options: Any) -> dict[str, Any]:
    response = client.post(f"/api/workspaces/{workspace}/rework", json={"keys": keys, **options})
    assert response.status_code == 202, response.text
    return client.get(f"/api/rework-jobs/{response.json()['id']}").json()


def attempts(client: TestClient, workspace: int, key: str) -> list[dict[str, Any]]:
    response = client.get(f"/api/workspaces/{workspace}/items/{key}/rework-attempts")
    assert response.status_code == 200, response.text
    return response.json()


def patches(client: TestClient, workspace: int, key: str) -> list[dict[str, Any]]:
    return client.get(f"/api/workspaces/{workspace}/items/{key}/patches").json()


def patch_text(app: FastAPI, patch_id: int) -> str:
    with session(app) as s:
        patch = s.get(MigrationPatch, patch_id)
        assert patch is not None
        return patch.diff


def rework_dirs(config: Config, workspace: int) -> list[Path]:
    root = config.workspace_dir(workspace) / "rework"
    return sorted(root.iterdir()) if root.is_dir() else []


def test_prerequisite_first_then_the_dependent_on_top(
    app: FastAPI, client: TestClient, rework: ReworkRunner, config: Config, workspace: int, target: Path, scenarios: Path, log: Path
) -> None:
    plan = client.post(f"/api/workspaces/{workspace}/rework/plan", json={"keys": ["PROJ-112"]}).json()
    assert plan["units"] == [["PROJ-111"], ["PROJ-112"]]
    (prerequisite,) = plan["prerequisites"]
    assert (prerequisite["key"], prerequisite["included"], prerequisite["rework"], prerequisite["warning"]) == ("PROJ-111", True, True, None)
    assert (plan["rungs"], plan["caps"]) == (["resolve", "port"], {"item_cap": 5, "job_cap": 100, "build_rounds": 2})
    # PROJ-111 conflicts with nothing to stack it on; PROJ-112 may stack on it.
    assert plan["estimate"] == {"units": 2, "items": 2, "likely_calls": 1, "max_calls": 10}

    scenario(scenarios, "PROJ-111", RESOLVE)
    before = snapshot(target)
    job = start(client, workspace, ["PROJ-112"])
    assert (job["status"], job["done"], job["total"], job["calls"], job["errors"], job["units"]) == ("done", 2, 2, 1, [], [["PROJ-111"], ["PROJ-112"]])
    assert snapshot(target) == before
    assert rework_dirs(config, workspace) == []

    (first,) = attempts(client, workspace, "PROJ-111")
    assert (first["status"], first["rungs"], first["base_patch_ids"], first["notes"]) == ("patched", ["stack", "resolve"], [], [])
    assert [(r["status"]) for r in first["build_runs"]] == ["build_successful"]
    (call,) = first["calls"]
    assert (call["project"], call["rung"], call["round"], call["build_round"], call["status"]) == (CORE, "resolve", 0, 0, "ok")
    base = next(p for p in patches(client, workspace, "PROJ-111") if p["attempt_id"] == first["id"])
    assert (base["source"], base["level"], base["review"], base["base_patch_ids"], base["current"], base["stale"]) == ("ai", "resolve", "proposed", [], True, [])
    assert first["patch_ids"] == [base["id"]]
    assert "+int mode = 1;" in patch_text(app, base["id"])

    (second,) = attempts(client, workspace, "PROJ-112")
    assert (second["status"], second["rungs"], second["base_patch_ids"]) == ("patched", ["stack"], [base["id"]])
    dependent = next(p for p in patches(client, workspace, "PROJ-112") if p["attempt_id"] == second["id"])
    # No Copilot: the dependent applies as is on the prerequisite's patch.
    assert (dependent["source"], dependent["level"], dependent["review"], dependent["base_patch_ids"]) == ("original", "stacked", "proposed", [base["id"]])
    with session(app) as s:
        (run,) = s.scalars(select(BuildRun).where(BuildRun.attempt_id == second["id"]))
        assert (run.job_id, run.status, run.modules) == (None, "build_successful", [CORE])
    # A rework build is not the item's build.
    assert item(app, "PROJ-112").build_status is None
    assert [(i.rework_status, i.rework_reason) for i in (item(app, "PROJ-111"), item(app, "PROJ-112"))] == [("proposed", None), ("proposed", None)]

    summary = client.get(f"/api/workspaces/{workspace}/rework-summary").json()
    assert summary["statuses"] == {"rework_failed": 0, "proposed": 2, "rejected": 0, "accepted": 0}
    found = client.get(f"/api/workspaces/{workspace}/items", params={"rework": "proposed"}).json()["items"]
    assert [(i["key"], i["rework_status"]) for i in found] == [("PROJ-111", "proposed"), ("PROJ-112", "proposed")]
    assert client.get(f"/api/workspaces/{workspace}/items", params={"rework": "bogus"}).status_code == 422

    # Accept is blocked by a proposed base; accepting the base first unblocks it.
    response = client.post(f"/api/workspaces/{workspace}/patches/{dependent['id']}/accept")
    assert (response.status_code, response.json()["detail"]) == (409, f"Base patch of PROJ-111 ({CORE}) is proposed: accept it first")
    assert client.post(f"/api/workspaces/{workspace}/patches/{base['id']}/accept").json()["rework_status"] == "accepted"
    assert client.post(f"/api/workspaces/{workspace}/patches/{dependent['id']}/accept").json() == {
        "id": dependent["id"], "review": "accepted", "review_note": None, "rework_status": "accepted"
    }  # fmt: skip
    assert client.post(f"/api/workspaces/{workspace}/patches/{dependent['id']}/accept").status_code == 409

    # Accepted patches build as usual: the base first.
    build = build_runner(app, config).start(workspace, keys=["PROJ-112"])
    with session(app) as s:
        (run,) = s.scalars(select(BuildRun).where(BuildRun.job_id == build.id))
        assert (run.status, run.patch_ids) == ("build_successful", [base["id"], dependent["id"]])

    # Ripple: a new patch of the prerequisite makes the dependent's stale; nothing is reworked by itself.
    scenario(scenarios, "PROJ-111", {**RESOLVE, "reply": "again"})
    start(client, workspace, ["PROJ-111"])
    assert next(p for p in patches(client, workspace, "PROJ-112") if p["id"] == dependent["id"])["stale"] == ["base PROJ-111 changed"]
    stale = client.get(f"/api/workspaces/{workspace}/items", params={"q": "PROJ-112"}).json()["items"][0]["stale"]
    assert stale == ["base PROJ-111 changed"]
    assert len(attempts(client, workspace, "PROJ-112")) == 1


def test_closed_prerequisite_left_out(app: FastAPI, client: TestClient, rework: ReworkRunner, workspace: int, scenarios: Path) -> None:
    client.post(f"/api/workspaces/{workspace}/items/state", json={"keys": ["PROJ-111"], "state": "closed"})
    plan = client.post(f"/api/workspaces/{workspace}/rework/plan", json={"keys": ["PROJ-112"]}).json()
    (left_out,) = plan["prerequisites"]
    assert (left_out["warning"], left_out["included"], left_out["base"], left_out["reason"]) == ("closed", False, False, "left out: reworked without it")
    assert plan["units"] == [["PROJ-112"]]
    ticked = client.post(f"/api/workspaces/{workspace}/rework/plan", json={"keys": ["PROJ-112"], "include": ["PROJ-111"]}).json()
    assert ticked["units"] == [["PROJ-111"], ["PROJ-112"]]

    # Without it PROJ-112's preimage is missing: `.rej` hunks, and the port rung not selected.
    job = start(client, workspace, ["PROJ-112"], rungs=["resolve"])
    assert (job["done"], job["skipped"]) == (1, [{"key": "PROJ-111", "reason": "left out: reworked without it", "base": False}])
    (failed,) = attempts(client, workspace, "PROJ-112")
    assert failed["notes"] == ["PROJ-111 left out: reworked without it"]
    assert (failed["status"], failed["reason"], failed["calls"]) == ("failed", "init/oem_props.h: preimage blob not in the target history (port not selected)", [])
    assert (item(app, "PROJ-112").rework_status, item(app, "PROJ-112").rework_reason) == ("rework_failed", failed["reason"])

    # Closed, with an accepted patch: still used as base.
    scenario(scenarios, "PROJ-111", RESOLVE)
    start(client, workspace, ["PROJ-111"], include=["PROJ-111"])
    (base,) = [p for p in patches(client, workspace, "PROJ-111") if p["level"] == "resolve"]
    client.post(f"/api/workspaces/{workspace}/patches/{base['id']}/accept")
    job = start(client, workspace, ["PROJ-112"])
    assert job["skipped"] == [{"key": "PROJ-111", "reason": "left out, used as base as it is", "base": True}]
    latest = attempts(client, workspace, "PROJ-112")[0]
    assert (latest["status"], latest["base_patch_ids"], latest["notes"]) == ("patched", [base["id"]], [])


def test_build_fix_round_fixes_a_compile_error(app: FastAPI, client: TestClient, rework: ReworkRunner, workspace: int, target: Path, scenarios: Path, log: Path) -> None:
    scenario(scenarios, "PROJ-102", FIX_BUILD)
    before = snapshot(target)
    job = start(client, workspace, ["PROJ-102"])
    assert (job["status"], job["errors"]) == ("done", [])
    assert snapshot(target) == before
    (attempt,) = attempts(client, workspace, "PROJ-102")
    assert attempt["status"] == "patched"
    assert [(r["status"], r["summary"].splitlines()[0] if r["summary"] else "") for r in attempt["build_runs"]] == [("build_failed", BUILD_ERROR), ("build_successful", "")]
    assert attempt["rungs"] == ["stack", "port"]
    assert [(c["rung"], c["round"], c["build_round"]) for c in attempt["calls"]][0] == ("port", 0, 1)
    prompt = calls(log)[0]["prompt"]
    assert "does not build: fix the build errors listed below" in prompt and f"## Build errors\n```\n{BUILD_ERROR}\n" in prompt
    assert f"packages/apps/Settings: {SETTINGS_STRINGS}" not in prompt and f"- {SETTINGS_STRINGS}\n" in prompt
    (patch,) = [p for p in patches(client, workspace, "PROJ-102") if p["attempt_id"] == attempt["id"]]
    assert (patch["source"], patch["level"], patch["review"]) == ("ai", "port", "proposed")
    text = patch_text(app, patch["id"])
    assert "FAIL_BUILD" not in text and "Screen brightness" in text
    fixed = next(f for f in attempt["files"] if f["path"] == SETTINGS_STRINGS)
    assert (fixed["rung"], fixed["notes"]) == ("port", ["build fix 1"])


def test_build_fix_without_progress_stops(app: FastAPI, client: TestClient, rework: ReworkRunner, workspace: int, scenarios: Path, log: Path) -> None:
    scenario(scenarios, "PROJ-102", {"reply": "looks fine to me"})
    start(client, workspace, ["PROJ-102"])
    (attempt,) = attempts(client, workspace, "PROJ-102")
    assert (attempt["status"], attempt["reason"]) == ("failed", f"build failed: {BUILD_ERROR} (a build-fix round changed nothing)")
    assert (len(attempt["build_runs"]), len(calls(log)), attempt["patch_ids"]) == (1, 1, [])
    assert item(app, "PROJ-102").rework_status == "rework_failed"

    # A round that changes something else: the same errors again, no further round.
    scenario(scenarios, "PROJ-102", {"reply": "renamed", "edits": [{"path": SETTINGS_STRINGS, "old": "<resources>", "new": "<resources>\n<!-- oem -->"}]})
    start(client, workspace, ["PROJ-102"])
    latest = attempts(client, workspace, "PROJ-102")[0]
    assert (latest["status"], latest["reason"], len(latest["build_runs"])) == ("failed", f"build failed: {BUILD_ERROR} (the same errors again)", 2)
    # No build-fix rounds at all: one build.
    start(client, workspace, ["PROJ-102"], build_rounds=0)
    latest = attempts(client, workspace, "PROJ-102")[0]
    assert (latest["reason"], len(latest["build_runs"])) == (f"build failed: {BUILD_ERROR}", 1)


def test_job_cap_leaves_rungs_1_2_for_the_rest(app: FastAPI, client: TestClient, rework: ReworkRunner, workspace: int, scenarios: Path, log: Path) -> None:
    scenario(scenarios, "PROJ-102", FIX_BUILD)
    scenario(scenarios, "PROJ-111", RESOLVE)
    job = start(client, workspace, ["PROJ-111", "PROJ-102"], job_cap=1)
    assert (job["units"], job["calls"], job["options"]["job_cap"]) == ([["PROJ-102"], ["PROJ-111"]], 1, 1)
    assert attempts(client, workspace, "PROJ-102")[0]["status"] == "patched"
    (core,) = attempts(client, workspace, "PROJ-111")
    assert (core["status"], core["reason"], core["calls"]) == ("failed", f"{PROPS}: 3-way merge conflicts", [])
    assert len(calls(log)) == 1


def test_item_cap(app: FastAPI, client: TestClient, rework: ReworkRunner, workspace: int, scenarios: Path, log: Path) -> None:
    scenario(scenarios, "PROJ-111", {"reply": "nothing"})
    start(client, workspace, ["PROJ-111"], item_cap=1)
    (attempt,) = attempts(client, workspace, "PROJ-111")
    assert (attempt["status"], attempt["reason"]) == ("failed", f"Copilot budget reached; {PROPS}: conflict markers left (line 2, 5, 11)")


def test_second_rework_starts_from_the_failure(app: FastAPI, client: TestClient, rework: ReworkRunner, workspace: int, scenarios: Path, log: Path) -> None:
    # Copilot leaves the markers twice, then resolves them.
    scenario(scenarios, "PROJ-111", {"reply": "nothing"}, {"reply": "nothing"}, RESOLVE)
    start(client, workspace, ["PROJ-111"])
    reason = f"{PROPS}: conflict markers left (line 2, 5, 11)"
    assert (item(app, "PROJ-111").rework_status, item(app, "PROJ-111").rework_reason) == ("rework_failed", reason)
    start(client, workspace, ["PROJ-111"])
    assert f"The previous attempt failed: {reason}\n" in calls(log)[2]["prompt"]
    assert attempts(client, workspace, "PROJ-111")[0]["status"] == "patched"

    # Rejected with a note: the next rework gets it.
    (patch,) = [p for p in patches(client, workspace, "PROJ-111") if p["level"] == "resolve"]
    out = client.post(f"/api/workspaces/{workspace}/patches/{patch['id']}/reject", json={"note": " keep level 0 "}).json()
    assert (out["review"], out["review_note"], out["rework_status"]) == ("rejected", "keep level 0", "rejected")
    assert (item(app, "PROJ-111").rework_status, item(app, "PROJ-111").rework_reason) == ("rejected", "keep level 0")
    start(client, workspace, ["PROJ-111"])
    assert "The reviewer rejected the last patch: keep level 0\n" in calls(log)[3]["prompt"]
    assert "previous attempt failed" not in calls(log)[3]["prompt"]


def wait_for(condition: Callable[[], bool], timeout: float = 30) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.05)


def test_cancel_kills_copilot_and_cleans_up(
    app: FastAPI, client: TestClient, config: Config, workspace: int, target: Path, scenarios: Path, log: Path
) -> None:
    threads: list[threading.Thread] = []

    def executor(run: Callable[[], None]) -> None:
        threads.append(threading.Thread(target=run))
        threads[-1].start()

    app.state.rework_runner = make_runner(app, config, scenarios, log, executor)
    scenario(scenarios, "PROJ-111", {"sleep": 30, **RESOLVE})
    before = snapshot(target)
    response = client.post(f"/api/workspaces/{workspace}/rework", json={"keys": ["PROJ-111"]})
    job_id = response.json()["id"]
    wait_for(lambda: bool(calls(log)))
    assert (config.rework_dir(workspace, attempts(client, workspace, "PROJ-111")[0]["id"]) / CORE).is_dir()
    # Nothing else runs meanwhile.
    assert client.post(f"/api/workspaces/{workspace}/rework", json={"keys": ["PROJ-102"]}).status_code == 409
    assert client.post(f"/api/workspaces/{workspace}/build", json={"keys": ["PROJ-102"]}).status_code == 409
    assert client.post(f"/api/workspaces/{workspace}/prescan", json={"keys": ["PROJ-102"]}).status_code == 409
    started = time.monotonic()
    assert client.post(f"/api/rework-jobs/{job_id}/cancel").status_code == 202
    threads[0].join(30)
    assert time.monotonic() - started < 15
    job = client.get(f"/api/rework-jobs/{job_id}").json()
    assert (job["status"], job["done"]) == ("canceled", 0)
    (attempt,) = attempts(client, workspace, "PROJ-111")
    assert (attempt["status"], attempt["calls"][0]["status"], attempt["patch_ids"]) == ("canceled", "canceled", [])
    assert rework_dirs(config, workspace) == []
    assert snapshot(target) == before
    # Cancel leaves the item as it was.
    assert item(app, "PROJ-111").rework_status is None
    assert client.post(f"/api/rework-jobs/{job_id}/cancel").status_code == 409


def test_refused_while_a_build_or_prescan_runs(app: FastAPI, config: Config, rework: ReworkRunner, workspace: int) -> None:
    with session(app) as s:
        s.add(BuildJob(workspace_id=workspace, scope="keys", issue_keys=[], skipped=[], total=0, errors=[]))
        s.commit()
    with pytest.raises(ReworkJobConflict, match="A build is running"):
        rework.start(workspace, ["PROJ-111"], Options())
    with session(app) as s:
        for job in s.scalars(select(BuildJob)):
            job.status = "done"
        s.add(ReworkJob(workspace_id=workspace, issue_keys=[], options={}, units=[], skipped=[], total=0, errors=[]))
        s.commit()
    with pytest.raises(BuildJobConflict, match="A rework is running"):
        build_runner(app, config).start(workspace, keys=["PROJ-102"])
    with pytest.raises(JobConflictError, match="A rework is running"):
        app.state.prescan_runner.start(workspace, ["PROJ-102"])


def test_startup_marks_running_jobs_failed_and_removes_scratch(app: FastAPI, config: Config, workspace: int) -> None:
    with session(app) as s:
        job = ReworkJob(workspace_id=workspace, issue_keys=["PROJ-111"], options={}, units=[["PROJ-111"]], skipped=[], total=1, errors=[])
        s.add(job)
        s.flush()
        attempt = ReworkAttempt(workspace_id=workspace, job_id=job.id, item_ids=[item(app, "PROJ-111").id])
        s.add(attempt)
        s.commit()
        scratch = config.rework_dir(workspace, attempt.id) / CORE
        scratch.mkdir(parents=True)
        fail_interrupted_jobs(s, config)
        assert (job.status, job.error, attempt.status, attempt.reason) == ("failed", "Interrupted by a server restart", "error", "Interrupted by a server restart")
    assert not scratch.parent.parent.exists()


def test_build_error_naming_another_items_symbol(app: FastAPI, workspace: int) -> None:
    summary = "system/core/init/init.cpp:12: error: use of undeclared identifier 'LoadOemBootProps'\nFAILED: out/soong/init.o"
    assert error_symbols(summary) == ["LoadOemBootProps"]
    assert error_symbols("Foo.java:3: error: cannot find symbol\n  symbol:   method setOemMode(int)") == ["setOemMode"]
    assert error_symbols("a.cpp:1: error: ‘android::init::Foo’ was not declared") == ["Foo"]
    with session(app) as s:
        # PROJ-105's diff adds it; not when PROJ-105 is in the unit or its base.
        assert missing_dependency(s, workspace, summary, [item(app, "PROJ-106").id]) == ("LoadOemBootProps", "PROJ-105")
        assert missing_dependency(s, workspace, summary, [item(app, "PROJ-106").id, item(app, "PROJ-105").id]) is None


def test_include_dependents_and_job_results_offer_rework(app: FastAPI, client: TestClient, config: Config, rework: ReworkRunner, workspace: int) -> None:
    plan = client.post(f"/api/workspaces/{workspace}/rework/plan", json={"keys": ["PROJ-111"], "include_dependents": True}).json()
    assert plan["units"] == [["PROJ-111"], ["PROJ-112"]]
    assert [(i["key"], i["selected"], i["dependent"]) for i in plan["items"]] == [("PROJ-111", True, False), ("PROJ-112", True, True)]
    assert client.post(f"/api/workspaces/{workspace}/rework/plan", json={"keys": ["PROJ-999"]}).status_code == 422
    assert client.post(f"/api/workspaces/{workspace}/rework", json={"keys": ["PROJ-111"], "rungs": ["magic"]}).status_code == 422

    build = build_runner(app, config).start(workspace, keys=["PROJ-111", "PROJ-101"])
    assert client.get(f"/api/build-jobs/{build.id}").json()["rework_candidates"] == ["PROJ-111"]
    prescan = client.get(f"/api/workspaces/{workspace}/prescan-jobs").json()[0]
    assert prescan["rework_candidates"] == ["PROJ-104", "PROJ-111", "PROJ-112"]
    with session(app) as s:
        events = [e.type for e in s.scalars(select(HistoryEvent).where(HistoryEvent.item_id == item(app, "PROJ-111").id))]
        assert "rework_started" not in events


def test_missing_dependency_instead_of_a_round(app: FastAPI, client: TestClient, rework: ReworkRunner, workspace: int, target: Path, scenarios: Path, log: Path) -> None:
    error = "system/core/init/oem_props.cpp:2: error: use of undeclared identifier 'LoadOemBootProps'"
    (target / "build" / "envsetup.sh").write_text(f'lunch() {{ :; }}\nmmm() {{ echo "{error}"; return 1; }}\n')
    scenario(scenarios, "PROJ-111", RESOLVE)
    start(client, workspace, ["PROJ-111"])
    (attempt,) = attempts(client, workspace, "PROJ-111")
    assert (attempt["status"], attempt["reason"]) == ("failed", f"likely missing dependency: `LoadOemBootProps` is added by PROJ-105; build failed: {error}")
    # Only the resolve call: no build-fix round.
    assert [c["rung"] for c in attempt["calls"]] == ["resolve"] and len(attempt["build_runs"]) == 1


def test_item_already_in_the_target(app: FastAPI, client: TestClient, rework: ReworkRunner, workspace: int) -> None:
    start(client, workspace, ["PROJ-103"])
    (attempt,) = attempts(client, workspace, "PROJ-103")
    assert (attempt["status"], attempt["reason"], attempt["patch_ids"]) == ("failed", "nothing to change: already in the target", [])
