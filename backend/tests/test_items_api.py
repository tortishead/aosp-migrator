from typing import Any

import pytest
from fake_jira import FakeJiraStore
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from test_import import app, client, client_factory, history, jira_config, run_import, session, workspace_id  # noqa: F401

from aosp_migrator.db.models import Attachment, Item, JiraIssue

FILLER = [f"PROJ-{n}" for n in range(105, 126)]


@pytest.fixture
def imported(client: TestClient, workspace_id: int, fake_jira: FakeJiraStore) -> int:
    assert run_import(client, workspace_id)["status"] == "done"
    return workspace_id


def items(client: TestClient, workspace_id: int, **params: Any) -> dict[str, Any]:
    response = client.get(f"/api/workspaces/{workspace_id}/items", params={"limit": 500, **params})
    assert response.status_code == 200, response.text
    return response.json()


def keys(client: TestClient, workspace_id: int, **params: Any) -> list[str]:
    return [i["key"] for i in items(client, workspace_id, **params)["items"]]


def set_state(client: TestClient, workspace_id: int, state: str, *item_keys: str) -> Any:
    return client.post(f"/api/workspaces/{workspace_id}/items/state", json={"keys": list(item_keys), "state": state})


def test_list_fields_and_natural_key_order(client: TestClient, imported: int, jira_url: str) -> None:
    page = items(client, imported)
    assert page["total"] == 25
    # Linked-only PROJ-201 is not a backlog item; PROJ-105 sorts after PROJ-104, not after PROJ-1xx strings.
    assert [i["key"] for i in page["items"]] == ["PROJ-101", "PROJ-102", "PROJ-103", "PROJ-104", *FILLER]
    camera = page["items"][0]
    assert camera | {"id": 0, "jira_updated": ""} == {
        "id": 0,
        "key": "PROJ-101",
        "summary": "Camera HAL crash on resume",
        "status": "In Progress",
        "labels": ["oem-patch", "camera"],
        "components": ["Camera"],
        "fix_versions": ["2026.Q4"],
        "jira_updated": "",
        "local_state": "new",
        "dropped": False,
        "last_import_error": None,
        # Two versions of one patch count once; the screenshot is not a diff.
        "diff_count": 1,
        "diff_subjects": ["[PATCH] camera: fix null deref on resume"],
        "target_projects": [],
        "cluster": None,
        "triage_label": None,
        "triage_reason": None,
        "stale": [],
        "dependencies": [],
        "build_status": None,
        "build_reason": None,
        "rework_status": None,
        "rework_reason": None,
        "browse_url": f"{jira_url}/browse/PROJ-101",
        "rework_built": False,
    }
    assert [i["diff_count"] for i in page["items"][1:4]] == [1, 1, 1]


def test_pagination_and_sort(client: TestClient, imported: int) -> None:
    page = items(client, imported, limit=10, offset=20)
    assert (page["total"], page["offset"], page["limit"]) == (25, 20, 10)
    assert [i["key"] for i in page["items"]] == FILLER[-5:]

    assert keys(client, imported, sort="-key")[:2] == ["PROJ-125", "PROJ-124"]
    # PROJ-104 was updated last (Sep 29), fillers first (Sep 1 + n hours).
    assert keys(client, imported, sort="-updated")[0] == "PROJ-104"
    assert keys(client, imported, sort="updated")[0] == "PROJ-105"
    assert keys(client, imported, sort="status")[0] == "PROJ-101"  # "In Progress" < "Open" < "Resolved"
    assert client.get(f"/api/workspaces/{imported}/items", params={"sort": "nope"}).status_code == 422
    assert client.get(f"/api/workspaces/{imported}/items", params={"limit": 0}).status_code == 422


def test_filters(client: TestClient, imported: int) -> None:
    assert keys(client, imported, status="Resolved") == ["PROJ-104"]
    assert keys(client, imported, status=["Resolved", "In Progress"]) == ["PROJ-101", "PROJ-104"]
    assert keys(client, imported, label="power") == ["PROJ-103"]
    assert keys(client, imported, label=["power", "camera"]) == ["PROJ-101", "PROJ-103"]
    assert keys(client, imported, component="Framework") == ["PROJ-103", *FILLER]
    assert keys(client, imported, fix_version="2026.Q4") == ["PROJ-101", "PROJ-102"]
    assert keys(client, imported, has_diff="true") == ["PROJ-101", "PROJ-102", "PROJ-103", "PROJ-104"]
    assert keys(client, imported, has_diff="false") == FILLER
    assert keys(client, imported, has_error="true") == []
    assert len(keys(client, imported, has_error="false")) == 25
    assert keys(client, imported, dropped="true") == []
    assert len(keys(client, imported, dropped="false")) == 25
    assert keys(client, imported, state="new") == keys(client, imported)
    assert keys(client, imported, state="closed") == []


def test_filter_import_errors(app: FastAPI, client: TestClient, imported: int) -> None:
    with session(app) as s:
        item = s.scalars(select(Item).join(JiraIssue, Item.issue_id == JiraIssue.id).where(JiraIssue.key == "PROJ-103")).one()
        item.last_import_error = "attachment download failed"
        s.commit()
    assert keys(client, imported, has_error="true") == ["PROJ-103"]
    assert "PROJ-103" not in keys(client, imported, has_error="false")
    assert len(keys(client, imported, has_error="false")) == 24


def test_group_keeps_groups_together_ahead_of_sort(app: FastAPI, client: TestClient, imported: int) -> None:
    with session(app) as s:
        by_key = {key: item for item, key in s.execute(select(Item, JiraIssue.key).join(JiraIssue, Item.issue_id == JiraIssue.id))}
        by_key["PROJ-104"].triage_label = "conflicts"
        by_key["PROJ-102"].triage_label = by_key["PROJ-103"].triage_label = "applies"
        by_key["PROJ-103"].build_status = "build_successful"
        by_key["PROJ-105"].build_status = "build_failed"
        s.commit()
    assert client.post(f"/api/workspaces/{imported}/clusters", json={"keys": ["PROJ-103", "PROJ-110"], "name": "power"}).status_code == 201

    # Labels worst first, then the sort inside each label; unlabeled items last.
    assert keys(client, imported, group="triage", sort="-key")[:4] == ["PROJ-104", "PROJ-103", "PROJ-102", "PROJ-125"]
    assert keys(client, imported, group="build")[:3] == ["PROJ-105", "PROJ-103", "PROJ-101"]
    assert keys(client, imported, group="cluster")[:3] == ["PROJ-103", "PROJ-110", "PROJ-101"]
    assert keys(client, imported, group="status")[0] == "PROJ-101"
    assert client.get(f"/api/workspaces/{imported}/items", params={"group": "nope"}).status_code == 422


def test_filters_combine_with_and(client: TestClient, imported: int) -> None:
    assert keys(client, imported, label="oem-patch", component="Framework") == ["PROJ-103"]
    assert keys(client, imported, label="oem-patch", fix_version="2026.Q4", status="Open") == ["PROJ-102"]
    assert keys(client, imported, component="Framework", has_diff="false", q="Filler 11") == [f"PROJ-{n}" for n in range(110, 120)]
    assert keys(client, imported, label="camera", status="Open") == []


def test_free_text_search(app: FastAPI, client: TestClient, imported: int) -> None:
    assert keys(client, imported, q="PROJ-103") == ["PROJ-103"]
    assert keys(client, imported, q="brightness") == ["PROJ-102"]  # summary, any case
    assert keys(client, imported, q="reopen") == ["PROJ-101"]  # raw description
    # Diff paths, substring: CameraService.cpp is in the patch, not in the text.
    assert keys(client, imported, q="libcameraservice") == ["PROJ-101"]
    assert keys(client, imported, q="BatteryStats.java") == ["PROJ-103"]
    # Rename source path counts too.
    assert keys(client, imported, q="OemDisplayUtils") == ["PROJ-102"]
    # Linked ticket's diff paths do not make the item match.
    assert keys(client, imported, q="StreamHalAidl") == []
    # Terms combine with AND, in any column.
    assert keys(client, imported, q="camera  dimens") == []
    assert keys(client, imported, q="settings brightness") == ["PROJ-102"]
    # Short terms fall back to LIKE; FTS syntax is taken literally.
    assert keys(client, imported, q="Q4 reopen") == [] and keys(client, imported, q="HA") == ["PROJ-101"]
    assert keys(client, imported, q='"camera" OR NOT') == []
    assert keys(client, imported, q="100%") == ["PROJ-102"]
    assert keys(client, imported, q="_") == ["PROJ-102", "PROJ-104"]  # oem_strings.xml, oem_battery.png


def test_search_index_follows_reimport(app: FastAPI, client: TestClient, imported: int, fake_jira: FakeJiraStore) -> None:
    fake_jira.issues["PROJ-105"].summary = "Wifi scan throttling"
    fake_jira.attach("PROJ-105", "wifi.patch", fake_jira.issues["PROJ-102"].attachments[0].content, "text/x-patch", fake_jira.issues["PROJ-105"].updated)
    assert run_import(client, imported, ["PROJ-105", "PROJ-102"])["status"] == "done"
    assert keys(client, imported, q="wifi") == ["PROJ-105"]
    assert keys(client, imported, q="Filler ticket 105") == []
    assert keys(client, imported, q="BrightnessLevel") == ["PROJ-102", "PROJ-105"]

    # Attachment rows going away (e.g. removed in Jira) update the paths too.
    with session(app) as s:
        for attachment in s.scalars(select(Attachment).where(Attachment.filename == "wifi.patch")):
            s.delete(attachment)
        s.commit()
    assert keys(client, imported, q="BrightnessLevel") == ["PROJ-102"]


def test_facets(client: TestClient, imported: int) -> None:
    response = client.get(f"/api/workspaces/{imported}/items/facets")
    assert response.status_code == 200
    # PROJ-201 (Closed, Audio) is linked-only and not listed.
    assert response.json() == {
        "statuses": ["In Progress", "Open", "Resolved"],
        "labels": ["camera", "oem-patch", "power", "systemui"],
        "components": ["Camera", "Framework", "Settings", "SystemUI"],
        "fix_versions": ["2026.Q3", "2026.Q4"],
    }


def test_bulk_state_change(app: FastAPI, client: TestClient, imported: int) -> None:
    response = set_state(client, imported, "not_relevant", "PROJ-101", "PROJ-102")
    assert response.status_code == 200
    assert response.json() == {"changed": ["PROJ-101", "PROJ-102"]}
    assert keys(client, imported, state="not_relevant") == ["PROJ-101", "PROJ-102"]
    assert keys(client, imported, state=["not_relevant", "closed"]) == ["PROJ-101", "PROJ-102"]

    # Already in that state: no change, no history event.
    assert set_state(client, imported, "closed", "PROJ-102", "PROJ-103").json() == {"changed": ["PROJ-102", "PROJ-103"]}
    assert set_state(client, imported, "closed", "PROJ-102").json() == {"changed": []}
    # Reversible.
    assert set_state(client, imported, "new", "PROJ-102").json() == {"changed": ["PROJ-102"]}

    with session(app) as s:
        events = [(e.type, e.payload) for e in history(s, "PROJ-102")]
    assert events == [
        ("imported", events[0][1]),
        ("state_changed", {"old": "new", "new": "not_relevant"}),
        ("state_changed", {"old": "not_relevant", "new": "closed"}),
        ("state_changed", {"old": "closed", "new": "new"}),
    ]

    # Reimport keeps the local state.
    assert run_import(client, imported, ["PROJ-103"])["status"] == "done"
    assert keys(client, imported, state="closed") == ["PROJ-103"]


def test_bulk_state_validation(app: FastAPI, client: TestClient, imported: int) -> None:
    assert set_state(client, imported, "done", "PROJ-101").status_code == 422
    assert set_state(client, imported, "closed").status_code == 422
    # Linked-only tickets are not backlog items; nothing changes when one key is unknown.
    response = set_state(client, imported, "closed", "PROJ-101", "PROJ-201")
    assert (response.status_code, response.json()["detail"]) == (404, "Not in this workspace's backlog: PROJ-201")
    with session(app) as s:
        assert {i.local_state for i in s.scalars(select(Item))} == {"new"}
    assert set_state(client, 999, "closed", "PROJ-101").status_code == 404


def test_unknown_workspace(client: TestClient) -> None:
    assert client.get("/api/workspaces/999/items").status_code == 404
    assert client.get("/api/workspaces/999/items/facets").status_code == 404


def test_items_are_per_workspace(client: TestClient, imported: int, tmp_path) -> None:
    other = tmp_path / "other"
    other.mkdir()
    response = client.post("/api/workspaces", json={"name": "Other", "jql": "project = PROJ", "target_tree": str(other)})
    other_id = response.json()["id"]
    assert items(client, other_id)["total"] == 0
    assert client.get(f"/api/workspaces/{other_id}/items/facets").json()["labels"] == []
    assert set_state(client, other_id, "closed", "PROJ-101").status_code == 404
