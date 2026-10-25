import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fake_jira import DEFAULT_PAT, FakeJiraStore
from fake_jira.fixtures import fixture_file
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from aosp_migrator.app import create_app
from aosp_migrator.config import JIRA_BASE_URL, JIRA_PAT, Config
from aosp_migrator.db.models import Attachment, Comment, HistoryEvent, ImportJob, Item, ItemLink, JiraIssue, Note
from aosp_migrator.jira.base import JiraClient
from aosp_migrator.jira.datacenter import DataCenterClient

JQL_KEYS = [f"PROJ-{n}" for n in range(101, 126)]


@pytest.fixture
def jira_config(config: Config, jira_url: str) -> Config:
    config.set({JIRA_BASE_URL: jira_url, JIRA_PAT: DEFAULT_PAT})
    return config


@pytest.fixture
def client_factory(jira_url: str) -> Callable[[], JiraClient]:
    return lambda: DataCenterClient(jira_url, DEFAULT_PAT, sleep=lambda _: None)


@pytest.fixture
def app(jira_config: Config, client_factory: Callable[[], JiraClient]) -> FastAPI:
    return create_app(jira_config, jira_client_factory=lambda: client_factory())


@pytest.fixture
def client(app: FastAPI) -> TestClient:
    return TestClient(app)


@pytest.fixture
def workspace_id(client: TestClient, tmp_path: Path) -> int:
    tree = tmp_path / "tree"
    tree.mkdir()
    response = client.post("/api/workspaces", json={"name": "A15", "jql": "project = PROJ", "target_tree": str(tree)})
    return response.json()["id"]


def session(app: FastAPI) -> Session:
    return app.state.sessionmaker()


def wait(client: TestClient, job_id: int) -> dict[str, Any]:
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        job = client.get(f"/api/import-jobs/{job_id}").json()
        if job["status"] != "running":
            return job
        time.sleep(0.02)
    raise AssertionError(f"Import job {job_id} still running")


def run_import(client: TestClient, workspace_id: int, keys: list[str] | None = None) -> dict[str, Any]:
    response = client.post(f"/api/workspaces/{workspace_id}/import", json={"keys": keys})
    assert response.status_code == 202, response.text
    assert response.json()["status"] == "running"
    return wait(client, response.json()["id"])


def item(s: Session, key: str) -> Item:
    found = s.scalar(select(Item).join(JiraIssue, Item.issue_id == JiraIssue.id).where(JiraIssue.key == key))
    assert found is not None, key
    return found


def issue(s: Session, key: str) -> JiraIssue:
    found = s.scalar(select(JiraIssue).where(JiraIssue.key == key))
    assert found is not None, key
    return found


def history(s: Session, key: str) -> list[HistoryEvent]:
    return list(s.scalars(select(HistoryEvent).where(HistoryEvent.item_id == item(s, key).id).order_by(HistoryEvent.id)))


def attachments(s: Session, key: str) -> dict[str, list[Attachment]]:
    by_name: dict[str, list[Attachment]] = {}
    for a in s.scalars(select(Attachment).where(Attachment.issue_id == issue(s, key).id).order_by(Attachment.created)):
        by_name.setdefault(a.filename, []).append(a)
    return by_name


def test_first_import(app: FastAPI, client: TestClient, workspace_id: int, fake_jira: FakeJiraStore) -> None:
    fake_jira.max_page_size = 10
    job = run_import(client, workspace_id)
    assert job["status"] == "done", job
    assert (job["done"], job["total"], job["errors"], job["error"]) == (25, 25, [], None)
    assert job["finished_at"] is not None

    with session(app) as s:
        keys = s.scalars(select(JiraIssue.key).join(Item, Item.issue_id == JiraIssue.id).order_by(JiraIssue.key)).all()
        assert keys == JQL_KEYS
        assert all(i.local_state == "new" and not i.dropped and i.last_import_error is None for i in s.scalars(select(Item)))
        assert [e.type for e in history(s, "PROJ-101")] == ["imported"]

        camera = issue(s, "PROJ-101")
        assert camera.summary == "Camera HAL crash on resume"
        assert (camera.status, camera.labels, camera.components, camera.fix_versions) == ("In Progress", ["oem-patch", "camera"], ["Camera"], ["2026.Q4"])
        assert {"link_type": "Relates", "direction": "outward", "description": "relates to", "key": "PROJ-201"} in camera.issue_links
        assert camera.jira_updated == datetime(2026, 9, 25, 16, tzinfo=UTC)
        assert [c.author for c in s.scalars(select(Comment).where(Comment.issue_id == camera.id).order_by(Comment.created))] == ["Raj Patel", "Jane Doe"]

        # Two versions of the same patch: both kept, newest current, both parsed.
        files = attachments(s, "PROJ-101")
        v1, v2 = files["0001-camera-fix-null-deref.patch"]
        assert (v1.is_current, v2.is_current) == (False, True)
        assert v1.kind == v2.kind == "diff"
        assert v2.parsed is not None and v2.parsed["format"] == "format-patch"
        assert Path(v2.local_path).read_bytes() == fixture_file("camera-fix.v2.patch")

        # Embedded screenshot: downloaded, and the description points at the local copy.
        (screenshot,) = files["screenshot.png"]
        local_url = f"/api/workspaces/{workspace_id}/attachments/{screenshot.jira_id}/screenshot.png"
        assert f'src="{local_url}"' in camera.description_html
        assert "/secure/attachment/" not in camera.description_html

        assert attachments(s, "PROJ-102")["logs.zip"][0].kind == "other"
        assert attachments(s, "PROJ-102")["logs.zip"][0].local_path is None
        assert attachments(s, "PROJ-103")["fix.txt"][0].kind == "diff"
        assert attachments(s, "PROJ-103")["notes.txt"][0].kind == "other"

        # Linked tickets one level deep: PROJ-201 is stored, but is not a backlog item.
        linked = issue(s, "PROJ-201")
        assert s.scalar(select(Item).where(Item.issue_id == linked.id)) is None
        assert attachments(s, "PROJ-201")["audio-guard.patch"][0].kind == "diff"
        links = s.execute(
            select(JiraIssue.key, ItemLink.description).join(ItemLink, ItemLink.issue_id == JiraIssue.id).where(ItemLink.item_id == item(s, "PROJ-101").id)
        ).all()
        assert sorted(links) == [("PROJ-102", "blocks"), ("PROJ-201", "relates to")]
        # A linked ticket that is also a backlog item shares its row.
        assert s.scalar(select(func.count()).select_from(JiraIssue).where(JiraIssue.key == "PROJ-102")) == 1

    response = client.get(local_url)
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
    assert response.headers["content-security-policy"] == "sandbox"
    assert client.get(f"/api/workspaces/{workspace_id}/attachments/999/x.png").status_code == 404


def test_reimport_updates_jira_fields_and_keeps_local_state(app: FastAPI, client: TestClient, workspace_id: int, fake_jira: FakeJiraStore) -> None:
    run_import(client, workspace_id)
    with session(app) as s:
        camera = item(s, "PROJ-101")
        camera.local_state = "not_relevant"
        s.add(Note(item_id=camera.id, text="Already fixed upstream"))
        s.commit()

    fake_jira.issues["PROJ-101"].summary = "Camera HAL crash on resume (v2)"
    fake_jira.issues["PROJ-101"].labels = ["oem-patch"]
    fake_jira.comment("PROJ-101", "Raj Patel", "Verified.", "<p>Verified.</p>", datetime(2026, 9, 26, tzinfo=UTC))
    job = run_import(client, workspace_id)
    assert job["status"] == "done"

    with session(app) as s:
        camera = item(s, "PROJ-101")
        assert issue(s, "PROJ-101").summary == "Camera HAL crash on resume (v2)"
        assert camera.local_state == "not_relevant"
        assert [n.text for n in s.scalars(select(Note).where(Note.item_id == camera.id))] == ["Already fixed upstream"]
        events = history(s, "PROJ-101")
        assert [e.type for e in events] == ["imported", "updated"]
        assert events[1].payload == {
            "job_id": job["id"],
            "fields": {
                "summary": {"old": "Camera HAL crash on resume", "new": "Camera HAL crash on resume (v2)"},
                "labels": {"old": ["oem-patch", "camera"], "new": ["oem-patch"]},
            },
            "comments_added": 1,
        }
        # Unchanged tickets get no history entry.
        assert [e.type for e in history(s, "PROJ-102")] == ["imported"]


def test_ticket_leaving_jql_is_dropped_and_restored_when_back(app: FastAPI, client: TestClient, workspace_id: int, fake_jira: FakeJiraStore) -> None:
    run_import(client, workspace_id)
    fake_jira.issues["PROJ-110"].in_jql = False
    assert run_import(client, workspace_id)["total"] == 24
    with session(app) as s:
        assert item(s, "PROJ-110").dropped
        assert not item(s, "PROJ-111").dropped
        assert [e.type for e in history(s, "PROJ-110")] == ["imported", "dropped"]

    # Reimporting a dropped item by key refreshes it but leaves the flag alone.
    assert run_import(client, workspace_id, ["PROJ-110"])["status"] == "done"
    with session(app) as s:
        assert item(s, "PROJ-110").dropped

    fake_jira.issues["PROJ-110"].in_jql = True
    run_import(client, workspace_id)
    with session(app) as s:
        assert not item(s, "PROJ-110").dropped
        assert [e.type for e in history(s, "PROJ-110")] == ["imported", "dropped", "restored"]


def test_new_attachment_version_on_single_reimport(app: FastAPI, client: TestClient, workspace_id: int, fake_jira: FakeJiraStore) -> None:
    run_import(client, workspace_id)
    v3 = fake_jira.attach("PROJ-101", "0001-camera-fix-null-deref.patch", fixture_file("camera-fix.v2.patch"), "text/x-patch", datetime(2026, 9, 27, tzinfo=UTC))
    requests_before = len(fake_jira.requests)
    job = run_import(client, workspace_id, ["PROJ-101"])
    assert (job["scope"], job["issue_keys"], job["status"], job["done"], job["total"]) == ("keys", ["PROJ-101"], "done", 1, 1)
    assert not any("/search" in r for r in fake_jira.requests[requests_before:])

    with session(app) as s:
        versions = attachments(s, "PROJ-101")["0001-camera-fix-null-deref.patch"]
        assert [(a.jira_id, a.is_current) for a in versions][-1] == (v3.id, True)
        assert [a.is_current for a in versions] == [False, False, True]
        assert history(s, "PROJ-101")[-1].payload["attachments_added"] == ["0001-camera-fix-null-deref.patch"]


def test_failing_ticket_is_recorded_and_job_completes(app: FastAPI, client: TestClient, workspace_id: int, fake_jira: FakeJiraStore) -> None:
    fake_jira.fail_paths["/rest/api/2/issue/PROJ-103/comment"] = 400
    job = run_import(client, workspace_id)
    assert (job["status"], job["done"]) == ("done", 25)
    assert [e["key"] for e in job["errors"]] == ["PROJ-103"]
    assert "HTTP 400" in job["errors"][0]["message"]

    with session(app) as s:
        # A new ticket that fails still shows up in the backlog, with its error.
        failed = item(s, "PROJ-103")
        assert "HTTP 400" in (failed.last_import_error or "")
        assert issue(s, "PROJ-103").summary == "Battery history gap after long screen-off"
        assert attachments(s, "PROJ-103") == {}
        assert [e.type for e in history(s, "PROJ-103")] == ["import_failed"]
        assert item(s, "PROJ-104").last_import_error is None

    del fake_jira.fail_paths["/rest/api/2/issue/PROJ-103/comment"]
    assert run_import(client, workspace_id, ["PROJ-103"])["errors"] == []
    with session(app) as s:
        assert item(s, "PROJ-103").last_import_error is None
        assert set(attachments(s, "PROJ-103")) == {"fix.txt", "notes.txt"}


def test_only_listed_link_types_are_followed(app: FastAPI, client: TestClient, workspace_id: int) -> None:
    workspace = client.get(f"/api/workspaces/{workspace_id}").json()
    client.put(f"/api/workspaces/{workspace_id}", json={**workspace, "link_types": ["is blocked by", "BLOCKS"]})
    assert run_import(client, workspace_id)["status"] == "done"
    with session(app) as s:
        # The "relates to" ticket is neither fetched nor linked; the Jira links stay as reported.
        assert s.scalar(select(JiraIssue).where(JiraIssue.key == "PROJ-201")) is None
        assert len(issue(s, "PROJ-101").issue_links) == 2
        links = s.scalars(select(ItemLink.description).where(ItemLink.item_id == item(s, "PROJ-101").id)).all()
        assert links == ["blocks"]

    client.put(f"/api/workspaces/{workspace_id}", json={**workspace, "link_types": ["relates to"]})
    run_import(client, workspace_id)
    with session(app) as s:
        links = s.execute(
            select(JiraIssue.key, ItemLink.description).join(ItemLink, ItemLink.issue_id == JiraIssue.id).where(ItemLink.item_id == item(s, "PROJ-101").id)
        ).all()
        assert links == [("PROJ-201", "relates to")]


def test_failing_linked_ticket_does_not_fail_the_item(app: FastAPI, client: TestClient, workspace_id: int, fake_jira: FakeJiraStore) -> None:
    fake_jira.fail_paths["/rest/api/2/issue/PROJ-201"] = 404
    job = run_import(client, workspace_id)
    assert job["status"] == "done"
    assert [e["key"] for e in job["errors"]] == ["PROJ-201"]
    assert job["errors"][0]["message"].startswith("Linked from PROJ-101")
    with session(app) as s:
        assert item(s, "PROJ-101").last_import_error is None
        assert s.scalar(select(JiraIssue).where(JiraIssue.key == "PROJ-201")) is None


class BlockingClient:
    """Wraps a real client and pauses inside the import of `key` until released."""

    def __init__(self, inner: JiraClient, key: str) -> None:
        self._inner = inner
        self._key = key
        self.reached = threading.Event()
        self.release = threading.Event()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def get_comments(self, key: str) -> Any:
        if key == self._key:
            self.reached.set()
            assert self.release.wait(10)
        return self._inner.get_comments(key)


def test_cancel_mid_job_and_one_job_per_workspace(jira_config: Config, client_factory: Callable[[], JiraClient], tmp_path: Path) -> None:
    blocking: list[BlockingClient] = []
    block_on = ["PROJ-105"]

    def factory() -> JiraClient:
        blocking.append(BlockingClient(client_factory(), block_on[0]))
        return blocking[-1]  # type: ignore[return-value]

    app = create_app(jira_config, jira_client_factory=factory)
    client = TestClient(app)
    tree = tmp_path / "tree"
    tree.mkdir()
    workspace_id = client.post("/api/workspaces", json={"name": "A15", "jql": "x", "target_tree": str(tree)}).json()["id"]

    job = client.post(f"/api/workspaces/{workspace_id}/import", json={}).json()
    assert blocking[0].reached.wait(10)

    second = client.post(f"/api/workspaces/{workspace_id}/import", json={})
    assert second.status_code == 409
    assert client.delete(f"/api/workspaces/{workspace_id}").status_code == 409
    assert client.get(f"/api/workspaces/{workspace_id}/import-jobs").json()[0]["status"] == "running"

    canceled = client.post(f"/api/import-jobs/{job['id']}/cancel")
    assert canceled.status_code == 202
    assert canceled.json()["cancel_requested"] is True
    blocking[0].release.set()

    # The ticket in flight finishes, then the job stops.
    job = wait(client, job["id"])
    assert (job["status"], job["done"], job["total"]) == ("canceled", 5, 25)
    assert client.post(f"/api/import-jobs/{job['id']}/cancel").status_code == 409
    with session(app) as s:
        assert s.scalar(select(func.count()).select_from(Item)) == 5
        # A canceled job saw only part of the JQL: nothing may be flagged dropped.
        assert s.scalar(select(func.count()).select_from(Item).where(Item.dropped.is_(True))) == 0

    # The workspace is free again.
    block_on[0] = ""
    assert run_import(client, workspace_id)["status"] == "done"


def test_start_validation(client: TestClient, workspace_id: int, jira_config: Config) -> None:
    assert client.post("/api/workspaces/99/import", json={}).status_code == 404
    response = client.post(f"/api/workspaces/{workspace_id}/import", json={"keys": ["PROJ-101"]})
    assert response.status_code == 404
    assert response.json()["detail"] == "Not in this workspace's backlog: PROJ-101"
    assert client.post(f"/api/workspaces/{workspace_id}/import", json={"keys": [" "]}).status_code == 422
    assert client.get("/api/import-jobs/99").status_code == 404

    jira_config.set({JIRA_PAT: None})
    response = client.post(f"/api/workspaces/{workspace_id}/import", json={})
    assert response.status_code == 422
    assert client.get(f"/api/workspaces/{workspace_id}/import-jobs").json() == []


def test_auth_failure_fails_the_job(client: TestClient, workspace_id: int, fake_jira: FakeJiraStore) -> None:
    fake_jira.pat = "rotated"
    job = run_import(client, workspace_id)
    assert job["status"] == "failed"
    assert job["error"] == "Authentication failed (HTTP 401)"


def test_auth_failure_mid_job_stops_instead_of_failing_every_ticket(client: TestClient, workspace_id: int, fake_jira: FakeJiraStore) -> None:
    fake_jira.fail_paths["/rest/api/2/issue/PROJ-104/comment"] = 401
    job = run_import(client, workspace_id)
    assert (job["status"], job["error"], job["done"]) == ("failed", "Authentication failed (HTTP 401)", 3)


def test_delete_workspace_removes_imported_data(app: FastAPI, client: TestClient, workspace_id: int, jira_config: Config) -> None:
    run_import(client, workspace_id)
    assert jira_config.attachments_dir(workspace_id).is_dir()
    assert client.delete(f"/api/workspaces/{workspace_id}").status_code == 204
    with session(app) as s:
        for model in (JiraIssue, Item, ItemLink, Comment, Attachment, HistoryEvent, ImportJob):
            assert s.scalar(select(func.count()).select_from(model)) == 0, model
    assert not jira_config.workspace_dir(workspace_id).exists()


def test_jobs_running_at_startup_are_marked_failed(app: FastAPI, workspace_id: int, jira_config: Config) -> None:
    with session(app) as s:
        s.add(ImportJob(workspace_id=workspace_id, scope="all", issue_keys=[], errors=[]))
        s.commit()
    restarted = create_app(jira_config)
    (job,) = TestClient(restarted).get(f"/api/workspaces/{workspace_id}/import-jobs").json()
    assert (job["status"], job["error"]) == ("failed", "Interrupted by a server restart")
