"""The front end does everything the command line does, through the same code (steps.py)."""

from __future__ import annotations

import json
import threading
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from typer.testing import CliRunner

from patchmig import steps
from patchmig.agents.fake import FakeAdapter
from patchmig.cli import app as cli
from patchmig.config import load_config
from patchmig.config.editor import SettingsEditor
from patchmig.ledger import Cluster, LedgerStore, Patch, State
from patchmig.ledger.models import AgentCall
from patchmig.memory import Memory
from patchmig.web.api import create_app
from patchmig.web.jobs import JobManager
from tests.fixtures.scenario import EXPECTED
from tests.test_ingest import make_config  # noqa: F401  (fixture)
from tests.test_intents_and_tools import intent_for

TOKEN = "s3cret"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture
def served(make_config, tmp_path: Path):  # noqa: F811
    path = make_config([{"type": "git_range"}], memory={"path": str(tmp_path / "memory")})
    cfg = load_config(path)
    store = LedgerStore.open(cfg.ledger_path)
    fake = FakeAdapter(intent_for)
    app = create_app(cfg, store, SettingsEditor(path), TOKEN, hooks={"adapter": lambda c: fake})
    return TestClient(app), app.state.jobs, path, cfg, store, fake


def start(client, jobs, step_names, options=None, run_id=None, wait=True):
    res = client.post(
        "/api/jobs",
        headers=AUTH,
        json={"steps": step_names, "by": "xuan", "options": options or {}, "run_id": run_id},
    )
    assert res.status_code == 202, res.text
    if wait:
        jobs.wait(30)
    return client.get(f"/api/jobs/{res.json()['id']}").json()


def text(job) -> list[str]:
    return [line["text"] for line in job["log"]]


def test_pipeline_from_the_browser_logs_what_the_cli_prints(served):
    client, jobs, path, cfg, store, fake = served
    job = start(client, jobs, ["ingest", "analyse", "intents"], {"ingest": {"use_jira": False}})
    assert job["state"] == "done", text(job)
    run = store.latest_run()
    assert job["run_id"] == run.id and job["by"] == "xuan"
    needed = sum(1 for e in EXPECTED if e.verdict == "needed")
    assert f"Run {run.id}: {len(EXPECTED)} patches" in text(job)
    assert "== Analyse" in text(job) and job["log"][0]["level"] == "step"
    # Credits: exactly the agent calls the job made, one per needed patch.
    assert len(fake.calls) == needed and job["agent_calls"] == needed
    with store.session() as s:
        assert job["credits"] == s.scalar(select(func.sum(AgentCall.credits)))
    # The CLI runs the same step function and prints the same lines.
    assert CliRunner().invoke(cli, ["ingest", "--no-jira", "--config", str(path)]).exit_code == 0
    out = CliRunner().invoke(cli, ["analyse", "--config", str(path)]).output
    i, j = text(job).index("== Analyse"), text(job).index("== Write intents")
    in_job = "\n".join(text(job)[i + 1 : j]).replace(f"Run {run.id}:", f"Run {run.id + 1}:")
    assert out.rstrip("\n") == in_job
    # The job is kept next to the config and listed.
    assert json.loads((path.parent / ".patchmig/jobs/00001.json").read_text())["state"] == "done"
    assert [j["id"] for j in client.get("/api/jobs").json()] == [job["id"]]
    assert client.get(f"/api/jobs/{job['id']}?after=2").json()["log"][0]["n"] == 3


def test_actions_need_the_admin_token_and_valid_options(served):
    client, *_ = served
    body = {"steps": ["analyse"], "by": "xuan"}
    assert client.post("/api/jobs", json=body).status_code == 401
    assert (
        client.post("/api/jobs", json=body, headers={"Authorization": "Bearer x"}).status_code
        == 401
    )
    bad = client.post("/api/jobs", headers=AUTH, json={**body, "steps": ["deploy"]})
    assert bad.status_code == 422 and "deploy" in bad.text
    bad = client.post("/api/jobs", headers=AUTH, json={**body, "options": {"analyse": {"x": 1}}})
    assert bad.status_code == 422
    bad = client.post(
        "/api/jobs",
        headers=AUTH,
        json={"steps": ["apply"], "by": "x", "options": {"apply": {"limit": -1}}},
    )
    assert bad.status_code == 422
    assert client.post("/api/jobs", headers=AUTH, json={**body, "run_id": 99}).status_code == 404
    catalogue = client.get("/api/steps").json()
    assert catalogue["actions"] and catalogue["pipeline"][:3] == ["ingest", "history", "analyse"]
    apply = next(s for s in catalogue["steps"] if s["name"] == "apply")
    assert {"clusters", "limit", "no_agent", "stale", "redo"} == {
        o["name"] for o in apply["options"]
    }
    read_only = TestClient(create_app(None, None))
    assert read_only.post("/api/jobs", json=body).status_code == 403
    assert not read_only.get("/api/steps").json()["actions"]


def test_one_job_at_a_time_and_stop_between_steps(served, monkeypatch):
    client, jobs, _, _, store, _ = served
    entered, release = threading.Event(), threading.Event()

    def slow(ctx, run_id=None):
        ctx.info("working")
        entered.set()
        release.wait(10)

    monkeypatch.setitem(
        steps.STEPS, "ingest", replace(steps.STEPS["ingest"], fn=slow, run_scoped=True)
    )
    job = start(client, jobs, ["ingest", "analyse"], wait=False)
    assert entered.wait(10)
    busy = client.post("/api/jobs", headers=AUTH, json={"steps": ["analyse"], "by": "x"})
    assert busy.status_code == 409 and f"job {job['id']}" in busy.text
    assert client.get(f"/api/jobs/{job['id']}").json()["state"] == "running"
    assert client.post(f"/api/jobs/{job['id']}/stop", headers=AUTH).status_code == 200
    release.set()
    jobs.wait(10)
    done = client.get(f"/api/jobs/{job['id']}").json()
    assert done["state"] == "stopped" and done["stop_requested"]
    assert "Stopped before analyse: stop requested." in text(done)
    assert "== Analyse" not in text(done)
    assert client.post(f"/api/jobs/{job['id']}/stop", headers=AUTH).status_code == 409


def test_a_failing_step_fails_the_job_with_its_message(served):
    client, jobs, *_ = served
    job = start(client, jobs, ["analyse"])  # no run yet
    assert job["state"] == "failed"
    assert job["error"] == "No runs yet. Start one with: migrate ingest"
    assert job["log"][-1] == {**job["log"][-1], "level": "error", "text": job["error"]}


def test_jobs_survive_a_restart_and_a_running_one_is_interrupted(tmp_path: Path):
    folder = tmp_path / "jobs"
    folder.mkdir()
    (folder / "00004.json").write_text(
        json.dumps(
            {
                "id": 4,
                "steps": ["verify"],
                "options": {},
                "by": "x",
                "run_id": 1,
                "state": "running",
                "started_at": "2026-01-01T00:00:00+00:00",
            }
        )
    )
    manager = JobManager(folder, lambda log: None)
    assert manager.get(4)["state"] == "interrupted"
    assert json.loads((folder / "00004.json").read_text())["state"] == "interrupted"


@pytest.fixture
def analysed(served):
    client, jobs, path, cfg, store, fake = served
    assert (
        start(client, jobs, ["ingest", "analyse"], {"ingest": {"use_jira": False}})["state"]
        == "done"
    )
    run = store.latest_run()
    with store.session() as s:
        clusters = s.scalars(select(Cluster).where(Cluster.run_id == run.id)).all()
    return client, store, run, clusters


def test_review_actions_in_the_browser(analysed):
    client, store, run, clusters = analysed
    dropped = next(c for c in clusters if c.state in (State.UPSTREAM, State.OBSOLETE))
    needed = next(c for c in clusters if c.state == State.NEEDED)
    url = f"/api/runs/{run.id}/clusters/{dropped.id}/review"
    assert client.post(url, json={"action": "approve", "by": "xuan"}).status_code == 401
    ok = client.post(url, headers=AUTH, json={"action": "approve", "by": "xuan", "reason": "fine"})
    assert ok.status_code == 200
    review = ok.json()["review"]
    assert (review["decision"], review["by"], review["source"]) == ("approved", "xuan", "web")
    assert ok.json()["queue"] == "approved"
    # The reviewer's rules hold: a reason is needed, states are checked.
    no_reason = client.post(url, headers=AUTH, json={"action": "reject", "by": "xuan"})
    assert no_reason.status_code == 409 and "reason" in no_reason.text
    retry = client.post(
        f"/api/runs/{run.id}/clusters/{needed.id}/review",
        headers=AUTH,
        json={"action": "retry", "by": "xuan"},
    )
    assert retry.status_code == 409 and "only failed clusters" in retry.text
    rejected = client.post(
        url, headers=AUTH, json={"action": "reject", "by": "xuan", "reason": "still needed"}
    )
    assert rejected.json()["state"] == "needed"
    assert rejected.json()["history"][-1]["actor"] == "human:xuan"
    bad = client.post(url, headers=AUTH, json={"action": "merge", "by": "xuan"})
    assert bad.status_code == 422


def test_notes_from_the_browser_and_what_they_make_stale(analysed):
    client, store, run, _ = analysed
    with store.session() as s:
        patch = s.scalars(
            select(Patch).where(Patch.run_id == run.id, Patch.state == State.NEEDED)
        ).first()
    body = {
        "run_id": run.id,
        "text": "Keep the old API",
        "by": "xuan",
        "scope": "patch",
        "target": patch.id,
        "stages": ["analyse"],
    }
    assert client.post("/api/notes", json=body).status_code == 401
    note = client.post("/api/notes", headers=AUTH, json=body).json()
    assert note["scope"] == "patch" and note["label"].startswith(f"#{patch.id} ")
    assert note["stages"] == ["analyse"] and note["author"] == "xuan"
    bad = client.post("/api/notes", headers=AUTH, json={**body, "target": 9999})
    assert bad.status_code == 422 and "not in run" in bad.text
    edited = client.put(
        f"/api/notes/{note['id']}",
        headers=AUTH,
        json={"by": "lin", "text": "Keep the old API (infotainment)"},
    )
    assert edited.json()["edits"] == 1 and edited.json()["author"] == "lin"
    assert client.get("/api/notes").json()[0]["text"] == "Keep the old API (infotainment)"
    assert client.get(f"/api/runs/{run.id}/stale").json() == {"intents": [], "clusters": []}
    gone = client.post(f"/api/notes/{note['id']}/remove", headers=AUTH, json={"by": "xuan"})
    assert gone.status_code == 200 and client.get("/api/notes").json() == []
    assert client.get("/api/notes?removed=true").json()[0]["removed_at"]
    assert (
        client.put(
            f"/api/notes/{note['id']}", headers=AUTH, json={"by": "x", "text": "y"}
        ).status_code
        == 404
    )
    md = client.get(f"/api/runs/{run.id}/report").json()["markdown"]
    assert md.startswith("#")


def test_memory_lessons_in_the_browser(served):
    client, _, _, cfg, *_ = served
    assert client.get("/api/memory").json()["error"]  # not created yet
    mem = Memory.open(cfg, create=True)
    lesson = mem.propose("Power HAL moved", "Look in vendor/oem/power.", ["vendor/**"], {})
    view = client.get("/api/memory").json()
    assert [x["name"] for x in view["proposed"]] == [lesson.name] and view["approved"] == []
    url = f"/api/memory/lessons/{lesson.name}/approve"
    assert client.post(url, json={"by": "xuan"}).status_code == 401
    after = client.post(url, headers=AUTH, json={"by": "xuan"}).json()
    assert after["proposed"] == [] and after["approved"][0]["meta"]["approved_by"] == "xuan"
    assert client.post(url, headers=AUTH, json={"by": "xuan"}).status_code == 404
    assert (
        client.post(
            f"/api/memory/lessons/{lesson.name}/merge", headers=AUTH, json={"by": "x"}
        ).status_code
        == 404
    )


def test_pipeline_order_deepens_history_before_and_after_ingest(served, monkeypatch):
    client, jobs, *_ = served
    seen = []

    def fake(name):
        def fn(ctx, run_id=None, **options):
            seen.append((name, run_id))
            return 7 if name == "ingest" else None

        return fn

    for name in ("history", "ingest", "analyse"):
        monkeypatch.setitem(steps.STEPS, name, replace(steps.STEPS[name], fn=fake(name)))
    job = start(client, jobs, ["analyse", "history", "ingest"])
    assert job["state"] == "done" and job["run_id"] == 7
    # git_range source: history before ingest too; later steps work on the new run.
    assert seen == [("history", None), ("ingest", None), ("history", 7), ("analyse", 7)]


def test_service_file_starts_the_server_with_this_config(served):
    path = served[2]
    out = CliRunner().invoke(
        cli, ["service-file", "--config", str(path), "--port", "9000", "--user", "mig"]
    )
    assert out.exit_code == 0, out.output
    assert f"--config {path.resolve()} --host 127.0.0.1 --port 9000" in out.output
    assert (
        "User=mig" in out.output
        and f"EnvironmentFile=-{path.parent}/.patchmig/serve.env" in out.output
    )
    assert "Restart=on-failure" in out.output
