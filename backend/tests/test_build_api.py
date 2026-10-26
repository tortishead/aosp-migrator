import time
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient
from test_build_runner import fake_jira, wait_for, workspace  # noqa: F401
from test_import import session
from test_prescan_api import app, client, target, workspace_id  # noqa: F401
from test_tree import snapshot

from aosp_migrator.api.build import log_tail
from aosp_migrator.db.models import Workspace


def wait(client: TestClient, job_id: int) -> dict[str, Any]:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        job = client.get(f"/api/build-jobs/{job_id}").json()
        if job["status"] != "running":
            return job
        time.sleep(0.02)
    raise AssertionError(f"Build job {job_id} still running")


def start(client: TestClient, workspace: int, **body: Any) -> dict[str, Any]:
    response = client.post(f"/api/workspaces/{workspace}/build", json=body)
    assert response.status_code == 202, response.text
    return response.json()


def keys(client: TestClient, workspace: int, query: str) -> list[str]:
    return [item["key"] for item in client.get(f"/api/workspaces/{workspace}/items?{query}").json()["items"]]


def test_build_selected_runs_summary_and_filter(client: TestClient, workspace: int, target: Path) -> None:
    before = snapshot(target)
    job = start(client, workspace, keys=["PROJ-105", "PROJ-102", "PROJ-103"])
    assert job["skipped"] == [{"key": "PROJ-103", "reason": "pre-scan: present"}]
    job = wait(client, job["id"])
    assert (job["status"], job["done"], job["total"], job["unit_keys"]) == ("done", 2, 2, ["PROJ-102"])
    assert "FAILED: out/target/common/obj/APPS/Settings_intermediates/package-res.apk" in job["log_tail"]
    assert snapshot(target) == before
    assert [j["id"] for j in client.get(f"/api/workspaces/{workspace}/build-jobs?limit=1").json()] == [job["id"]]

    summary = client.get(f"/api/workspaces/{workspace}/build-summary").json()
    assert summary["statuses"] == {"apply_failed": 0, "build_failed": 1, "build_successful": 1}
    assert summary["unbuilt"] > 0

    items = {i["key"]: i for i in client.get(f"/api/workspaces/{workspace}/items?q=PROJ-102").json()["items"]}
    assert items["PROJ-102"]["build_status"] == "build_failed"
    assert "error: FAIL_BUILD marker" in items["PROJ-102"]["build_reason"]
    assert keys(client, workspace, "build=build_successful") == ["PROJ-105"]
    assert keys(client, workspace, "build=build_failed&build=build_successful") == ["PROJ-102", "PROJ-105"]
    assert "PROJ-105" not in keys(client, workspace, "build=none&limit=500")
    assert client.get(f"/api/workspaces/{workspace}/items?build=bogus").status_code == 422

    (run,) = client.get(f"/api/workspaces/{workspace}/items/PROJ-105/builds").json()
    assert (run["job_id"], run["keys"], run["status"], run["tool"], run["modules"], run["summary"]) == (
        job["id"],
        ["PROJ-105"],
        "build_successful",
        "mmm",
        ["system/core"],
        "",
    )
    log = client.get(run["log_url"])
    assert log.headers["content-type"].startswith("text/plain")
    assert "mmm system/core\n#### build completed successfully" in log.text
    assert client.get(f"/api/workspaces/{workspace}/items/PROJ-103/builds").json() == []
    assert client.get(f"/api/workspaces/{workspace}/build-runs/9999/log").status_code == 404


def test_build_a_cluster(client: TestClient, workspace: int) -> None:
    cluster = client.post(f"/api/workspaces/{workspace}/clusters", json={"keys": ["PROJ-105", "PROJ-102"]}).json()
    job = wait(client, start(client, workspace, cluster_id=cluster["id"])["id"])
    assert (job["scope"], job["cluster_id"], job["total"], job["unit_keys"]) == ("cluster", cluster["id"], 1, ["PROJ-105", "PROJ-102"])
    (run,) = client.get(f"/api/workspaces/{workspace}/items/PROJ-102/builds").json()
    assert (run["keys"], run["cluster_id"]) == (["PROJ-105", "PROJ-102"], cluster["id"])


def test_start_errors(app: FastAPI, client: TestClient, workspace: int) -> None:
    url = f"/api/workspaces/{workspace}/build"
    assert client.post(url, json={}).status_code == 422
    assert client.post(url, json={"keys": ["PROJ-105"], "cluster_id": 1}).status_code == 422
    assert client.post(url, json={"keys": [" "]}).status_code == 422
    assert client.post(url, json={"cluster_id": 999}).status_code == 404
    assert client.post("/api/workspaces/999/build", json={"keys": ["PROJ-105"]}).status_code == 404
    unknown = client.post(url, json={"keys": ["PROJ-999"]})
    assert (unknown.status_code, unknown.json()["detail"]) == (422, "Not in the backlog: PROJ-999")
    with session(app) as s:
        ws = s.get(Workspace, workspace)
        assert ws is not None
        ws.lunch_target = None
        s.commit()
    no_lunch = client.post(url, json={"keys": ["PROJ-105"]})
    assert (no_lunch.status_code, no_lunch.json()["detail"]) == (422, "Set a lunch target in the workspace settings to build")


def test_cancel_and_conflict(client: TestClient, workspace: int, target: Path) -> None:
    (target / "SLOW_BUILD").write_text("SLOW_BUILD\n")
    before = snapshot(target)
    job = start(client, workspace, keys=["PROJ-102", "PROJ-105"])
    wait_for(lambda: "mmm packages/apps/Settings" in (client.get(f"/api/build-jobs/{job['id']}").json()["log_tail"] or ""))
    conflict = client.post(f"/api/workspaces/{workspace}/build", json={"keys": ["PROJ-105"]})
    assert conflict.status_code == 409
    canceled = client.post(f"/api/build-jobs/{job['id']}/cancel")
    assert (canceled.status_code, canceled.json()["cancel_requested"]) == (202, True)
    job = wait(client, job["id"])
    assert (job["status"], job["done"]) == ("canceled", 0)
    assert snapshot(target) == before
    assert client.post(f"/api/build-jobs/{job['id']}/cancel").status_code == 409
    assert client.get("/api/build-jobs/999").status_code == 404


def test_log_tail_keeps_the_last_lines(tmp_path: Path) -> None:
    log = tmp_path / "run.log"
    log.write_text("".join(f"line {i}\n" for i in range(100_000)))
    assert log_tail(log, 3) == "line 99997\nline 99998\nline 99999"
    log.write_text("only\n")
    assert log_tail(log) == "only"
