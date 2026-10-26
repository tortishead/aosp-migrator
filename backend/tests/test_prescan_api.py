import shutil
import subprocess
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from fake_jira import DEFAULT_PAT, FakeJiraServer, FakeJiraStore
from fake_jira.fixtures import at
from fake_jira.prescan import prescan_store
from fastapi import FastAPI
from fastapi.testclient import TestClient
from repo_tree import RepoTree
from sqlalchemy import select
from test_import import run_import, session

from aosp_migrator.app import create_app
from aosp_migrator.config import JIRA_BASE_URL, JIRA_PAT, Config
from aosp_migrator.db.models import Attachment, DiffScan, Item, JiraIssue
from aosp_migrator.jira.datacenter import DataCenterClient
from aosp_migrator.prescan.runner import PrescanRunner


@pytest.fixture
def target(tmp_path: Path) -> Path:
    return tmp_path / "aosp"


@pytest.fixture
def fake_jira(fake_jira_server: FakeJiraServer, target: Path) -> FakeJiraStore:
    """The pre-scan scenario instead of the default tickets; builds the target tree."""
    fake_jira_server.store = prescan_store(target)
    return fake_jira_server.store


@pytest.fixture
def app(config: Config, jira_url: str) -> FastAPI:
    config.set({JIRA_BASE_URL: jira_url, JIRA_PAT: DEFAULT_PAT})
    return create_app(config, jira_client_factory=lambda: DataCenterClient(jira_url, DEFAULT_PAT, sleep=lambda _: None))


@pytest.fixture
def client(app: FastAPI) -> TestClient:
    return TestClient(app)


@pytest.fixture
def workspace_id(client: TestClient, target: Path, fake_jira: FakeJiraStore) -> int:
    response = client.post("/api/workspaces", json={"name": "A15", "jql": "project = PROJ", "target_tree": str(target)})
    assert response.json()["target_tree_type"] == "repo"
    workspace_id = response.json()["id"]
    assert run_import(client, workspace_id)["status"] == "done"
    return workspace_id


def wait(client: TestClient, job_id: int) -> dict[str, Any]:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        job = client.get(f"/api/prescan-jobs/{job_id}").json()
        if job["status"] != "running":
            return job
        time.sleep(0.02)
    raise AssertionError(f"Pre-scan job {job_id} still running")


def run_prescan(client: TestClient, workspace_id: int, keys: list[str] | None = None) -> dict[str, Any]:
    response = client.post(f"/api/workspaces/{workspace_id}/prescan", json={"keys": keys})
    assert response.status_code == 202, response.text
    return wait(client, response.json()["id"])


def prescan(client: TestClient, workspace_id: int, key: str) -> dict[str, Any]:
    response = client.get(f"/api/workspaces/{workspace_id}/items/{key}/prescan")
    assert response.status_code == 200, response.text
    return response.json()


def test_job_over_fixture_workspace(client: TestClient, workspace_id: int, target: Path) -> None:
    job = run_prescan(client, workspace_id)
    assert (job["status"], job["scope"], job["stage"], job["done"], job["total"], job["errors"]) == ("done", "all", None, 25, 25, [])

    def diffs(key: str) -> list[dict[str, Any]]:
        return prescan(client, workspace_id, key)["diffs"]

    camera = prescan(client, workspace_id, "PROJ-101")
    assert (camera["scanned"], camera["presence"], camera["stale"]) == (True, "not_present", [])
    # Only the current version of the patch is scanned.
    assert [(d["filename"], d["project"], d["mapping_source"]) for d in camera["diffs"]] == [
        ("0001-camera-fix-null-deref.patch", "frameworks/av", "hint")
    ]
    assert camera["diffs"][0]["target_head"] == RepoTree(target).git("frameworks/av", "rev-parse", "HEAD").strip()
    assert (camera["diffs"][0]["apply"], camera["diffs"][0]["apply_detail"]) == (
        "applies",
        {"files": {"services/camera/libcameraservice/CameraService.cpp": "applies", "services/camera/libcameraservice/CameraService.h": "applies"}},
    )

    assert prescan(client, workspace_id, "PROJ-103")["presence"] == "present"
    assert (diffs("PROJ-103")[0]["apply"], diffs("PROJ-103")[0]["apply_detail"]) == (None, None)
    battery_icon = diffs("PROJ-104")[0]
    assert battery_icon["presence"] == "partial"
    assert battery_icon["presence_detail"]["files"] == {
        "packages/SystemUI/res/drawable-xxhdpi/oem_battery.png": "not_present",
        "packages/SystemUI/res/values/dimens.xml": "present",
    }
    # Only the file still missing is apply-checked; the fixture's binary data is not real.
    assert battery_icon["apply"] == "conflicts"
    assert battery_icon["apply_detail"]["files"] == {"packages/SystemUI/res/drawable-xxhdpi/oem_battery.png": "conflicts"}
    hal = diffs("PROJ-107")[0]
    assert (hal["project"], hal["mapping_source"]) == ("hardware/interfaces", "hint")
    upstream = diffs("PROJ-109")[0]
    assert upstream["presence"] == "present"
    assert upstream["presence_detail"]["history"][0]["match"] == "change_id"
    unmapped = diffs("PROJ-110")[0]
    assert (unmapped["project"], unmapped["presence"], unmapped["apply"], unmapped["target_head"]) == (None, None, None, None)
    assert sorted(unmapped["candidates"]) == ["hardware/interfaces", "packages/apps/Settings", "system/core"]
    filler = prescan(client, workspace_id, "PROJ-111")
    assert (filler["scanned"], filler["presence"], filler["diffs"]) == (False, None, [])


def items(client: TestClient, workspace_id: int, **params: Any) -> list[dict[str, Any]]:
    response = client.get(f"/api/workspaces/{workspace_id}/items", params={"limit": 500, **params})
    assert response.status_code == 200, response.text
    return response.json()["items"]


def keys(client: TestClient, workspace_id: int, **params: Any) -> list[str]:
    return [i["key"] for i in items(client, workspace_id, **params)]


def summary(client: TestClient, workspace_id: int) -> dict[str, Any]:
    response = client.get(f"/api/workspaces/{workspace_id}/prescan-summary")
    assert response.status_code == 200, response.text
    return response.json()


def test_triage_labels_counts_and_filters(client: TestClient, workspace_id: int) -> None:
    client.post(f"/api/workspaces/{workspace_id}/items/state", json={"keys": ["PROJ-103"], "state": "not_relevant"})
    assert summary(client, workspace_id)["unlabeled"] == 25
    run_prescan(client, workspace_id)

    labels = {i["key"]: (i["triage_label"], i["triage_reason"]) for i in items(client, workspace_id) if i["triage_label"]}
    assert labels == {
        "PROJ-101": ("applies", "frameworks/av: applies as is"),
        "PROJ-102": ("applies", "packages/apps/Settings: applies as is"),
        "PROJ-103": ("present", "frameworks/base: every file reverse-applies"),
        # Partial, but the file still missing conflicts.
        "PROJ-104": ("conflicts", "frameworks/base: packages/SystemUI/res/drawable-xxhdpi/oem_battery.png conflict"),
        "PROJ-105": ("applies", "system/core: applies as is"),
        "PROJ-106": ("applies", "system/core: applies as is"),
        "PROJ-107": ("applies", "hardware/interfaces: applies as is"),
        "PROJ-108": ("applies", "frameworks/av: applies as is"),
        "PROJ-109": ("present", labels["PROJ-109"][1]),
        "PROJ-110": ("unmapped", "0001-Build-with-OEM-flag.patch: one of packages/apps/Settings, system/core, hardware/interfaces"),
    }
    assert labels["PROJ-109"][1].startswith("frameworks/base: in commit ") and labels["PROJ-109"][1].endswith(" (change_id)")
    assert prescan(client, workspace_id, "PROJ-104")["label"] == "conflicts"
    # The pre-scan never touches the local state.
    assert [i["local_state"] for i in items(client, workspace_id) if i["key"] in ("PROJ-101", "PROJ-103")] == ["new", "not_relevant"]

    counts = summary(client, workspace_id)
    assert list(counts["labels"]) == ["error", "unmapped", "unknown", "conflicts", "partial", "3way", "applies", "likely_present", "present"]
    assert {k: v for k, v in counts["labels"].items() if v} == {"unmapped": 1, "conflicts": 1, "applies": 6, "present": 2}
    assert (counts["unlabeled"], counts["stale"]) == (15, 0)

    assert keys(client, workspace_id, triage="present") == ["PROJ-103", "PROJ-109"]
    assert keys(client, workspace_id, triage=["unmapped", "conflicts"]) == ["PROJ-104", "PROJ-110"]
    assert len(keys(client, workspace_id, triage="none")) == 15
    assert len(keys(client, workspace_id, triage=["none", "present"])) == 17
    assert client.get(f"/api/workspaces/{workspace_id}/items", params={"triage": "bogus"}).status_code == 422
    assert client.get("/api/workspaces/999/prescan-summary").status_code == 404


def test_stale_filter_and_count(client: TestClient, workspace_id: int, target: Path) -> None:
    run_prescan(client, workspace_id)
    assert keys(client, workspace_id, stale=True) == []
    RepoTree(target).commit("system/core", {"init/README": "init\n"}, "Add README")

    # PROJ-110 has no project, so no HEAD to move.
    assert [(i["key"], i["stale"]) for i in items(client, workspace_id, stale=True)] == [
        ("PROJ-105", ["system/core HEAD moved"]),
        ("PROJ-106", ["system/core HEAD moved"]),
    ]
    assert len(keys(client, workspace_id, stale=False)) == 23
    assert summary(client, workspace_id)["stale"] == 2
    # Combines with the label filter.
    assert keys(client, workspace_id, stale=True, triage="present") == []


def test_single_item_and_validation(client: TestClient, workspace_id: int) -> None:
    job = run_prescan(client, workspace_id, ["PROJ-103"])
    assert (job["status"], job["scope"], job["issue_keys"], job["done"], job["total"]) == ("done", "keys", ["PROJ-103"], 1, 1)
    assert prescan(client, workspace_id, "PROJ-103")["scanned"] is True
    assert prescan(client, workspace_id, "PROJ-101")["scanned"] is False

    assert client.post(f"/api/workspaces/{workspace_id}/prescan", json={"keys": ["PROJ-201"]}).status_code == 404
    assert client.post(f"/api/workspaces/{workspace_id}/prescan", json={"keys": [" "]}).status_code == 422
    assert client.post("/api/workspaces/999/prescan", json={}).status_code == 404
    assert client.get(f"/api/workspaces/{workspace_id}/items/PROJ-201/prescan").status_code == 404
    jobs = client.get(f"/api/workspaces/{workspace_id}/prescan-jobs").json()
    assert [j["id"] for j in jobs] == [job["id"]]


def test_missing_project_list_fails_the_job(client: TestClient, workspace_id: int, target: Path) -> None:
    (target / ".repo" / "project.list").unlink()
    job = run_prescan(client, workspace_id)
    assert job["status"] == "failed"
    assert "repo sync" in job["error"]


def test_plain_tree_of_git_projects(client: TestClient, target: Path, fake_jira: FakeJiraStore) -> None:
    """An AOSP checkout without `.repo`: projects are found by their `.git` dirs."""
    shutil.rmtree(target / ".repo")
    response = client.post("/api/workspaces", json={"name": "plain", "jql": "project = PROJ", "target_tree": str(target)})
    assert response.json()["target_tree_type"] == "plain"
    workspace_id = response.json()["id"]
    assert run_import(client, workspace_id)["status"] == "done"

    job = run_prescan(client, workspace_id)
    assert (job["status"], job["errors"]) == ("done", [])
    camera = prescan(client, workspace_id, "PROJ-101")["diffs"][0]
    assert (camera["project"], camera["mapping_source"]) == ("frameworks/av", "hint")
    assert prescan(client, workspace_id, "PROJ-103")["diffs"][0]["project"] == "frameworks/base"
    upstream = prescan(client, workspace_id, "PROJ-109")["diffs"][0]
    assert (upstream["presence"], upstream["presence_detail"]["history"][0]["match"]) == ("present", "change_id")


@pytest.mark.parametrize("tree_type", ["git", "plain"])
def test_git_or_plain_tree_is_one_project(client: TestClient, workspace_id: int, target: Path, tmp_path: Path, tree_type: str) -> None:
    tree = target / "frameworks/base"
    if tree_type == "plain":
        tree = tmp_path / "plain"
        shutil.copytree(target / "frameworks/base", tree, ignore=shutil.ignore_patterns(".git"))
    response = client.post("/api/workspaces", json={"name": tree_type, "jql": "project = PROJ", "target_tree": str(tree)})
    assert response.json()["target_tree_type"] == tree_type
    other = response.json()["id"]
    assert run_import(client, other)["status"] == "done"

    job = run_prescan(client, other)
    assert (job["status"], job["errors"]) == ("done", [])
    present = prescan(client, other, "PROJ-103")
    assert (present["presence"], present["diffs"][0]["project"], present["stale"]) == ("present", ".", [])
    expected_head = RepoTree(target).git("frameworks/base", "rev-parse", "HEAD").strip() if tree_type == "git" else None
    assert present["diffs"][0]["target_head"] == expected_head
    camera = prescan(client, other, "PROJ-101")["diffs"][0]
    assert (camera["project"], camera["candidates"]) == (None, [])
    # Lines moved on: only history can tell, and a plain tree has none.
    upstream = prescan(client, other, "PROJ-109")["diffs"][0]
    if tree_type == "git":
        assert upstream["presence"] == "present"
    else:
        assert (upstream["presence"], upstream["presence_detail"]["reason"]) == ("unknown", "history unavailable")


def test_item_error_does_not_stop_the_job(app: FastAPI, client: TestClient, workspace_id: int) -> None:
    with session(app) as s:
        patch = s.scalar(
            select(Attachment).join(JiraIssue, Attachment.issue_id == JiraIssue.id).where(JiraIssue.key == "PROJ-105", Attachment.kind == "diff")
        )
        assert patch is not None and patch.local_path is not None
        Path(patch.local_path).unlink()

    job = run_prescan(client, workspace_id)
    assert (job["status"], job["done"]) == ("done", 25)
    assert [e["key"] for e in job["errors"]] == ["PROJ-105"]
    assert "reimport" in job["errors"][0]["message"]
    assert prescan(client, workspace_id, "PROJ-105")["scanned"] is False
    assert prescan(client, workspace_id, "PROJ-106")["presence"] == "not_present"
    failed = items(client, workspace_id, q="PROJ-105")[0]
    assert failed["triage_label"] == "error"
    assert "reimport" in failed["triage_reason"]


def test_cancel_and_one_job_per_workspace(app: FastAPI, client: TestClient, workspace_id: int) -> None:
    pending: list[Callable[[], None]] = []
    app.state.prescan_runner = PrescanRunner(app.state.sessionmaker, lambda: [], executor=pending.append)

    job = client.post(f"/api/workspaces/{workspace_id}/prescan", json={}).json()
    assert client.post(f"/api/workspaces/{workspace_id}/prescan", json={}).status_code == 409
    assert client.delete(f"/api/workspaces/{workspace_id}").status_code == 409

    canceled = client.post(f"/api/prescan-jobs/{job['id']}/cancel")
    assert (canceled.status_code, canceled.json()["cancel_requested"]) == (202, True)
    pending.pop()()
    job = client.get(f"/api/prescan-jobs/{job['id']}").json()
    assert (job["status"], job["done"]) == ("canceled", 0)
    assert client.post(f"/api/prescan-jobs/{job['id']}/cancel").status_code == 409

    # The workspace is free again.
    client.post(f"/api/workspaces/{workspace_id}/prescan", json={})
    pending.pop()()
    assert client.get(f"/api/workspaces/{workspace_id}/prescan-jobs").json()[0]["status"] == "done"


def test_no_prescan_while_importing(app: FastAPI, client: TestClient, workspace_id: int) -> None:
    pending: list[Callable[[], None]] = []
    app.state.import_runner._executor = pending.append
    client.post(f"/api/workspaces/{workspace_id}/import", json={})
    response = client.post(f"/api/workspaces/{workspace_id}/prescan", json={})
    assert response.status_code == 409
    assert "import" in response.json()["detail"]


def test_stale_after_new_diff_version_and_head_move(client: TestClient, workspace_id: int, fake_jira: FakeJiraStore, target: Path) -> None:
    assert run_prescan(client, workspace_id)["status"] == "done"
    assert prescan(client, workspace_id, "PROJ-105")["stale"] == []

    old = fake_jira.issues["PROJ-105"].attachments[-1]
    fake_jira.attach("PROJ-105", old.filename, old.content.replace(b"OEM boot props", b"OEM boot properties"), old.mime, at(28))
    assert run_import(client, workspace_id, ["PROJ-105"])["status"] == "done"
    result = prescan(client, workspace_id, "PROJ-105")
    assert result["stale"] == ["new diff version"]
    assert result["diffs"][0]["scanned_at"] is None

    RepoTree(target).commit("system/core", {"init/README": "init\n"}, "Add README")
    assert prescan(client, workspace_id, "PROJ-106")["stale"] == ["system/core HEAD moved"]
    assert prescan(client, workspace_id, "PROJ-101")["stale"] == []

    assert run_prescan(client, workspace_id, ["PROJ-105", "PROJ-106"])["status"] == "done"
    assert prescan(client, workspace_id, "PROJ-105")["stale"] == []
    assert prescan(client, workspace_id, "PROJ-106")["stale"] == []


def pick(client: TestClient, workspace_id: int, key: str, attachment_id: int, project: str | None) -> Any:
    return client.put(f"/api/workspaces/{workspace_id}/items/{key}/diffs/{attachment_id}/project", json={"project": project})


def test_picked_project_is_scanned_and_survives_rerun(client: TestClient, workspace_id: int) -> None:
    run_prescan(client, workspace_id, ["PROJ-110"])
    diff = prescan(client, workspace_id, "PROJ-110")["diffs"][0]
    assert diff["project"] is None and "system/core" in diff["candidates"]
    assert "system/core" in client.get(f"/api/workspaces/{workspace_id}/target-projects").json()

    response = pick(client, workspace_id, "PROJ-110", diff["attachment_id"], "system/core")
    assert response.status_code == 200, response.text
    picked = response.json()["diffs"][0]
    # Scanned right away, no job needed.
    assert (picked["project"], picked["mapping_source"], picked["presence"], picked["apply"]) == ("system/core", "manual", "not_present", "applies")
    assert (response.json()["label"], response.json()["label_reason"]) == ("applies", "system/core: applies as is")
    history = client.get(f"/api/workspaces/{workspace_id}/items/PROJ-110").json()["history"]
    assert history[-1]["type"] == "project_picked"
    assert history[-1]["payload"] == {"filename": diff["filename"], "old": None, "new": "system/core"}

    run_prescan(client, workspace_id, ["PROJ-110"])
    again = prescan(client, workspace_id, "PROJ-110")
    assert (again["diffs"][0]["project"], again["diffs"][0]["mapping_source"], again["label"]) == ("system/core", "manual", "applies")

    # Back to the automatic mapping.
    reset = pick(client, workspace_id, "PROJ-110", diff["attachment_id"], None).json()
    assert (reset["diffs"][0]["project"], reset["diffs"][0]["mapping_source"], reset["label"]) == (None, None, "unmapped")


def test_pick_project_validation(app: FastAPI, client: TestClient, workspace_id: int) -> None:
    diff = prescan(client, workspace_id, "PROJ-110")["diffs"][0]
    # Not scanned before: picking creates the scan.
    assert pick(client, workspace_id, "PROJ-110", diff["attachment_id"], "hardware/interfaces").json()["diffs"][0]["presence"] == "not_present"
    unknown = pick(client, workspace_id, "PROJ-110", diff["attachment_id"], "no/such")
    assert (unknown.status_code, unknown.json()["detail"]) == (422, "Not a project of the target tree: no/such")
    other = prescan(client, workspace_id, "PROJ-105")["diffs"][0]
    assert pick(client, workspace_id, "PROJ-110", other["attachment_id"], "system/core").status_code == 404
    assert pick(client, workspace_id, "PROJ-999", diff["attachment_id"], "system/core").status_code == 404

    app.state.prescan_runner = PrescanRunner(app.state.sessionmaker, lambda: [], executor=lambda _: None)
    client.post(f"/api/workspaces/{workspace_id}/prescan", json={})
    assert pick(client, workspace_id, "PROJ-110", diff["attachment_id"], "system/core").status_code == 409


def test_target_projects_without_project_list(client: TestClient, workspace_id: int, target: Path) -> None:
    (target / ".repo" / "project.list").unlink()
    response = client.get(f"/api/workspaces/{workspace_id}/target-projects")
    assert response.status_code == 422
    assert "repo sync" in response.json()["detail"]


def test_jobs_running_at_startup_are_marked_failed(app: FastAPI, client: TestClient, workspace_id: int, config: Config) -> None:
    app.state.prescan_runner = PrescanRunner(app.state.sessionmaker, lambda: [], executor=lambda _: None)
    job = client.post(f"/api/workspaces/{workspace_id}/prescan", json={}).json()
    restarted = TestClient(create_app(config))
    job = restarted.get(f"/api/prescan-jobs/{job['id']}").json()
    assert (job["status"], job["error"]) == ("failed", "Interrupted by a server restart")


def test_plain_tree_with_shallow_project(client: TestClient, target: Path, fake_jira: FakeJiraStore, tmp_path: Path) -> None:
    """The upstream commit is cut off by the shallow clone: unknown, not not_present."""
    shutil.rmtree(target / ".repo")
    full = tmp_path / "full-base"
    shutil.move(target / "frameworks/base", full)
    subprocess.run(["git", "clone", "-q", "--depth", "1", f"file://{full}", str(target / "frameworks/base")], check=True)
    shutil.move(full / "packages/SystemUI", target / "frameworks/base/packages/SystemUI")
    workspace_id = client.post("/api/workspaces", json={"name": "plain", "jql": "project = PROJ", "target_tree": str(target)}).json()["id"]
    assert run_import(client, workspace_id)["status"] == "done"

    assert run_prescan(client, workspace_id)["status"] == "done"
    item = prescan(client, workspace_id, "PROJ-109")
    assert item["diffs"][0]["project"] == "frameworks/base"
    assert (item["presence"], item["diffs"][0]["presence_detail"]["reason"]) == ("unknown", "history unavailable")
