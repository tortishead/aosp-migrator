"""One server for several migration projects: switch between them, each keeps its own state."""

from __future__ import annotations

import threading
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from patchmig import steps
from patchmig.agents.fake import FakeAdapter
from patchmig.cli import app as cli
from patchmig.web.workspace import create_workspace_app
from tests.fixtures.scenario import EXPECTED
from tests.test_ingest import make_config  # noqa: F401  (fixture)
from tests.test_intents_and_tools import intent_for

TOKEN = "s3cret"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture
def workspace(make_config, tmp_path: Path):  # noqa: F811
    """A workspace folder with one valid project, `pilot`, over the scenario trees."""
    root = tmp_path / "ws"
    (root / "pilot").mkdir(parents=True)
    make_config([{"type": "git_range"}]).rename(root / "pilot" / "project.yaml")
    (root / "notes").mkdir()  # a folder without a project.yaml is not a project
    hooks = {"adapter": lambda c: FakeAdapter(intent_for)}
    return root, hooks


def serve(root: Path, hooks: dict, token: str | None = TOKEN) -> TestClient:
    return TestClient(create_workspace_app(root, token, read_only=token is None, hooks=hooks))


def wait(client: TestClient, folder: str) -> None:
    client.app.state.projects.app(folder).state.jobs.wait(30)


def test_projects_are_listed_and_created_in_the_browser(workspace):
    root, hooks = workspace
    client = serve(root, hooks)
    page = client.get("/")
    assert page.status_code == 200 and 'data-workspace=""' in page.text
    listed = client.get("/api/workspace").json()
    assert listed["actions"] and [p["folder"] for p in listed["projects"]] == ["pilot"]
    pilot = listed["projects"][0]
    assert pilot["name"] == "ingest-test" and pilot["migration"] == "Android 14 → 15"
    assert pilot["valid"] and pilot["runs"] == 0 and pilot["job"] is None

    assert client.post("/api/workspace/projects", json={"folder": "a15"}).status_code == 401
    bad = client.post("/api/workspace/projects", headers=AUTH, json={"folder": "../up"})
    assert bad.status_code == 422
    made = client.post("/api/workspace/projects", headers=AUTH, json={"folder": "a15-to-a16"})
    assert made.status_code == 201 and made.json()["name"] == "a15-to-a16"
    assert (root / "a15-to-a16" / "project.yaml").is_file()
    again = client.post("/api/workspace/projects", headers=AUTH, json={"folder": "pilot"})
    assert again.status_code == 409
    folders = [p["folder"] for p in client.get("/api/workspace").json()["projects"]]
    assert folders == ["a15-to-a16", "pilot"]

    read_only = serve(root, hooks, token=None)
    assert not read_only.get("/api/workspace").json()["actions"]
    assert read_only.post("/api/workspace/projects", json={"folder": "x"}).status_code == 403


def test_without_the_admin_token_the_workspace_needs_none(workspace):
    root, hooks = workspace
    client = TestClient(create_workspace_app(root, None, hooks=hooks, token_required=False))
    assert 'data-token="off"' in client.get("/").text
    assert client.get("/api/workspace").json()["actions"]
    assert client.post("/api/workspace/projects", json={"folder": "open"}).status_code == 201
    assert 'data-token="off"' in client.get("/p/pilot/").text
    assert client.get("/p/pilot/api/settings/auth").status_code == 200
    closed = TestClient(create_workspace_app(root, None, read_only=True, hooks=hooks,
                                             token_required=False))  # fmt: skip
    assert closed.post("/api/workspace/projects", json={"folder": "x"}).status_code == 403


def test_each_project_is_served_under_its_folder(workspace):
    root, hooks = workspace
    client = serve(root, hooks)
    page = client.get("/p/pilot/")
    assert page.status_code == 200 and 'data-workspace="pilot"' in page.text
    assert client.get("/p/pilot/static/app.js").status_code == 200
    assert client.get("/p/pilot", follow_redirects=False).status_code in (307, 308)
    assert client.get("/p/pilot/api/runs").json() == []
    assert client.get("/p/pilot/api/settings").json()["editable"]
    assert client.get("/p/notes/api/runs").status_code == 404
    assert client.get("/p/missing/").status_code == 404


def test_switching_keeps_each_projects_state(workspace):
    root, hooks = workspace
    client = serve(root, hooks)
    client.post("/api/workspace/projects", headers=AUTH, json={"folder": "other"})
    res = client.post("/p/pilot/api/jobs", headers=AUTH, json={"steps": ["ingest"], "by": "xuan"})
    assert res.status_code == 202, res.text
    wait(client, "pilot")
    runs = client.get("/p/pilot/api/runs").json()
    assert len(runs) == 1 and runs[0]["patches"] == len(EXPECTED)
    assert client.get("/p/other/api/runs").json() == []
    assert client.get("/p/other/api/jobs").json() == []

    # Come back later (a new server): the pilot's runs and jobs are still there.
    again = serve(root, hooks)
    pilot = next(
        p for p in again.get("/api/workspace").json()["projects"] if p["folder"] == "pilot"
    )
    assert pilot["runs"] == 1 and pilot["latest_run"]["id"] == runs[0]["id"]
    assert again.get("/p/pilot/api/jobs").json()[0]["state"] == "done"
    assert (root / "pilot" / ".patchmig" / "ledger.db").is_file()


def test_one_job_at_a_time_across_projects(workspace, monkeypatch):
    root, hooks = workspace
    client = serve(root, hooks)
    client.post("/api/workspace/projects", headers=AUTH, json={"folder": "other"})
    entered, release = threading.Event(), threading.Event()

    def slow(ctx, run_id=None):
        entered.set()
        release.wait(10)

    monkeypatch.setitem(
        steps.STEPS, "ingest", replace(steps.STEPS["ingest"], fn=slow, run_scoped=True)
    )
    body = {"steps": ["ingest"], "by": "xuan"}
    assert client.post("/p/pilot/api/jobs", headers=AUTH, json=body).status_code == 202
    assert entered.wait(10)
    pilot = next(
        p for p in client.get("/api/workspace").json()["projects"] if p["folder"] == "pilot"
    )
    assert pilot["job"]["steps"] == ["ingest"]
    busy = client.post("/p/other/api/jobs", headers=AUTH, json=body)
    assert busy.status_code == 409 and "another project" in busy.text
    release.set()
    wait(client, "pilot")
    entered.clear()
    release.set()
    assert client.post("/p/other/api/jobs", headers=AUTH, json=body).status_code == 202
    wait(client, "other")


def test_service_file_for_a_workspace(tmp_path: Path):
    out = CliRunner().invoke(cli, ["service-file", "--workspace", str(tmp_path), "--user", "mig"])
    assert out.exit_code == 0, out.output
    assert f"--workspace {tmp_path.resolve()} --host 127.0.0.1 --port 8765" in out.output
    assert f"EnvironmentFile=-{tmp_path.resolve()}/.patchmig/serve.env" in out.output
