from typing import Any

from fake_jira import FakeJiraStore
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from test_import import app, client, client_factory, history, jira_config, run_import, session, workspace_id  # noqa: F401
from test_items_api import imported, keys, set_state  # noqa: F401

from aosp_migrator.db.models import Cluster, ClusterEvent, ClusterMember, ClusterNote


def create(client: TestClient, workspace_id: int, *item_keys: str, **body: Any) -> Any:
    return client.post(f"/api/workspaces/{workspace_id}/clusters", json={"keys": list(item_keys), **body})


def created(client: TestClient, workspace_id: int, *item_keys: str, **body: Any) -> dict[str, Any]:
    response = create(client, workspace_id, *item_keys, **body)
    assert response.status_code == 201, response.text
    return response.json()


def cluster(client: TestClient, workspace_id: int, cluster_id: int) -> dict[str, Any]:
    response = client.get(f"/api/workspaces/{workspace_id}/clusters/{cluster_id}")
    assert response.status_code == 200, response.text
    return response.json()


def member_keys(detail: dict[str, Any]) -> list[str]:
    return [m["key"] for m in detail["members"]]


def item_events(app: FastAPI, key: str) -> list[tuple[str, dict[str, Any]]]:
    with session(app) as s:
        return [(e.type, e.payload) for e in history(s, key) if e.type.startswith("cluster_")]


def test_create_with_default_name_and_order(app: FastAPI, client: TestClient, imported: int) -> None:
    detail = created(client, imported, "PROJ-105", "PROJ-101", "PROJ-104", "PROJ-103")
    # Oldest current diff first, items without a diff last; the name follows the first member.
    assert member_keys(detail) == ["PROJ-103", "PROJ-101", "PROJ-104", "PROJ-105"]
    assert detail["name"] == f"PROJ-103 {detail['members'][0]['summary']}"
    assert detail["description"] == ""
    camera = detail["members"][1]
    assert camera | {"item_id": 0} == {
        "item_id": 0,
        "key": "PROJ-101",
        "summary": "Camera HAL crash on resume",
        "status": "In Progress",
        "local_state": "new",
        "dropped": False,
        "diff_count": 1,
    }
    assert [(h["type"], h["payload"]) for h in detail["history"]] == [("created", {"keys": member_keys(detail)})]
    assert item_events(app, "PROJ-101") == [("cluster_joined", {"cluster_id": detail["id"], "name": detail["name"]})]

    named = created(client, imported, "PROJ-106", "PROJ-107", name="  Display  ", description=" Brightness bits ")
    assert (named["name"], named["description"]) == ("Display", "Brightness bits")
    listed = client.get(f"/api/workspaces/{imported}/clusters").json()
    assert [c["name"] for c in listed] == ["Display", detail["name"]]
    assert member_keys(listed[0]) == ["PROJ-106", "PROJ-107"]


def test_create_validation(client: TestClient, imported: int) -> None:
    assert create(client, imported, "PROJ-101").status_code == 422
    assert create(client, imported, "PROJ-101", "PROJ-101").status_code == 422
    # Linked-only tickets cannot be members.
    response = create(client, imported, "PROJ-101", "PROJ-201")
    assert (response.status_code, response.json()["detail"]) == (404, "Not in this workspace's backlog: PROJ-201")
    assert create(client, 999, "PROJ-101", "PROJ-102").status_code == 404
    assert client.get(f"/api/workspaces/{imported}/clusters").json() == []


def test_item_is_in_one_cluster_and_moves(app: FastAPI, client: TestClient, imported: int) -> None:
    first = created(client, imported, "PROJ-101", "PROJ-102", "PROJ-103")
    second = created(client, imported, "PROJ-104", "PROJ-101")
    assert member_keys(cluster(client, imported, first["id"])) == ["PROJ-103", "PROJ-102"]
    assert member_keys(second) == ["PROJ-101", "PROJ-104"]
    with session(app) as s:
        assert s.scalar(select(func.count()).select_from(ClusterMember)) == 4
    assert item_events(app, "PROJ-101") == [
        ("cluster_joined", {"cluster_id": first["id"], "name": first["name"]}),
        ("cluster_left", {"cluster_id": first["id"], "name": first["name"], "moved_to": second["name"]}),
        ("cluster_joined", {"cluster_id": second["id"], "name": second["name"]}),
    ]
    removed = [h["payload"] for h in cluster(client, imported, first["id"])["history"] if h["type"] == "member_removed"]
    assert removed == [{"key": "PROJ-101", "moved_to": second["name"]}]

    # Adding members appends them in default order; members already there are ignored.
    url = f"/api/workspaces/{imported}/clusters/{second['id']}/members"
    response = client.post(url, json={"keys": ["PROJ-110", "PROJ-104", "PROJ-109"]})
    assert response.status_code == 200, response.text
    assert member_keys(response.json()) == ["PROJ-101", "PROJ-104", "PROJ-109", "PROJ-110"]
    assert response.json()["history"][-1]["payload"] == {"keys": ["PROJ-109", "PROJ-110"]}
    assert client.post(url, json={"keys": []}).status_code == 422
    assert client.post(url, json={"keys": ["PROJ-201"]}).status_code == 404


def test_moving_away_the_second_to_last_member_needs_confirmation(app: FastAPI, client: TestClient, imported: int) -> None:
    pair = created(client, imported, "PROJ-101", "PROJ-102")
    response = create(client, imported, "PROJ-102", "PROJ-103")
    assert response.status_code == 409
    assert pair["name"] in response.json()["detail"]
    assert member_keys(cluster(client, imported, pair["id"])) == ["PROJ-102", "PROJ-101"]
    with session(app) as s:
        assert s.scalar(select(func.count()).select_from(Cluster)) == 1

    moved = created(client, imported, "PROJ-102", "PROJ-103", confirm_dissolve=True)
    assert client.get(f"/api/workspaces/{imported}/clusters/{pair['id']}").status_code == 404
    assert [c["id"] for c in client.get(f"/api/workspaces/{imported}/clusters").json()] == [moved["id"]]
    # The member left behind gets a history entry too.
    assert item_events(app, "PROJ-101")[-1] == ("cluster_left", {"cluster_id": pair["id"], "name": pair["name"], "dissolved": True})


def test_remove_member_and_dissolve(app: FastAPI, client: TestClient, imported: int) -> None:
    detail = created(client, imported, "PROJ-101", "PROJ-102", "PROJ-103")
    url = f"/api/workspaces/{imported}/clusters/{detail['id']}/members"
    assert client.delete(f"{url}/PROJ-102").json() == {"dissolved": False}
    assert client.delete(f"{url}/PROJ-102").status_code == 404
    assert client.delete(f"{url}/PROJ-201").status_code == 404
    assert client.delete(f"{url}/PROJ-101").status_code == 409
    assert member_keys(cluster(client, imported, detail["id"])) == ["PROJ-103", "PROJ-101"]

    assert client.delete(f"{url}/PROJ-101", params={"confirm_dissolve": True}).json() == {"dissolved": True}
    assert client.get(f"/api/workspaces/{imported}/clusters/{detail['id']}").status_code == 404
    assert item_events(app, "PROJ-101")[-1] == ("cluster_left", {"cluster_id": detail["id"], "name": detail["name"]})
    assert item_events(app, "PROJ-103")[-1][1]["dissolved"] is True


def test_dissolve_keeps_items(app: FastAPI, client: TestClient, imported: int) -> None:
    detail = created(client, imported, "PROJ-101", "PROJ-102")
    client.post(f"/api/workspaces/{imported}/clusters/{detail['id']}/notes", json={"text": "Same HAL"})
    assert client.delete(f"/api/workspaces/{imported}/clusters/{detail['id']}").status_code == 204
    assert client.delete(f"/api/workspaces/{imported}/clusters/{detail['id']}").status_code == 404
    with session(app) as s:
        for model in (Cluster, ClusterMember, ClusterNote, ClusterEvent):
            assert s.scalar(select(func.count()).select_from(model)) == 0
    assert len(keys(client, imported)) == 25


def test_rename_and_describe(client: TestClient, imported: int) -> None:
    detail = created(client, imported, "PROJ-101", "PROJ-102")
    url = f"/api/workspaces/{imported}/clusters/{detail['id']}"
    response = client.patch(url, json={"name": " Camera ", "description": "HAL and settings"})
    assert response.status_code == 200, response.text
    assert (response.json()["name"], response.json()["description"]) == ("Camera", "HAL and settings")
    # Unchanged values write no history.
    client.patch(url, json={"name": "Camera"})
    assert [h["type"] for h in cluster(client, imported, detail["id"])["history"]] == ["created", "renamed", "description_changed"]
    assert cluster(client, imported, detail["id"])["history"][1]["payload"] == {"old": detail["name"], "new": "Camera"}
    assert client.patch(url, json={"name": "  "}).status_code == 422
    assert client.patch(url, json={"name": ""}).status_code == 422


def test_reorder(client: TestClient, imported: int) -> None:
    detail = created(client, imported, "PROJ-101", "PROJ-102", "PROJ-103")
    url = f"/api/workspaces/{imported}/clusters/{detail['id']}/order"
    response = client.put(url, json={"keys": ["PROJ-101", "PROJ-103", "PROJ-102"]})
    assert response.status_code == 200, response.text
    assert member_keys(response.json()) == ["PROJ-101", "PROJ-103", "PROJ-102"]
    assert response.json()["history"][-1] == response.json()["history"][-1] | {
        "type": "reordered",
        "payload": {"keys": ["PROJ-101", "PROJ-103", "PROJ-102"]},
    }
    for bad in (["PROJ-101", "PROJ-103"], ["PROJ-101", "PROJ-103", "PROJ-102", "PROJ-102"], ["PROJ-101", "PROJ-103", "PROJ-104"]):
        assert client.put(url, json={"keys": bad}).status_code == 422
    # Same order again: no history entry.
    client.put(url, json={"keys": ["PROJ-101", "PROJ-103", "PROJ-102"]})
    assert [h["type"] for h in cluster(client, imported, detail["id"])["history"]] == ["created", "reordered"]


def test_notes(client: TestClient, imported: int) -> None:
    detail = created(client, imported, "PROJ-101", "PROJ-102")
    url = f"/api/workspaces/{imported}/clusters/{detail['id']}/notes"
    response = client.post(url, json={"text": "  Migrate 101 first.  "})
    assert response.status_code == 201
    assert response.json()["text"] == "Migrate 101 first."
    assert client.post(url, json={"text": "  "}).status_code == 422
    after = cluster(client, imported, detail["id"])
    assert [n["text"] for n in after["notes"]] == ["Migrate 101 first."]
    assert after["history"][-1]["payload"] == {"note_id": after["notes"][0]["id"], "text": "Migrate 101 first."}


def test_combined_files_and_reimport_keeps_clusters(client: TestClient, imported: int, fake_jira: FakeJiraStore) -> None:
    # PROJ-105 gets the same patch as PROJ-102, so they touch the same files.
    patch = fake_jira.issues["PROJ-102"].attachments[0].content
    fake_jira.attach("PROJ-105", "settings-port.diff", patch, "text/x-diff", fake_jira.issues["PROJ-105"].updated)
    detail = created(client, imported, "PROJ-102", "PROJ-105", "PROJ-101")
    assert run_import(client, imported, ["PROJ-105", "PROJ-102", "PROJ-101"])["status"] == "done"

    after = cluster(client, imported, detail["id"])
    assert member_keys(after) == member_keys(detail)
    touched = {f["path"]: f["touched_by"] for f in after["files"]}
    assert touched["services/camera/libcameraservice/CameraService.cpp"] == ["PROJ-101"]
    shared = [path for path, by in touched.items() if len(by) > 1]
    assert shared and all(touched[path] == ["PROJ-102", "PROJ-105"] for path in shared)
    # Rename sources count as touched.
    assert any("OemDisplayUtils" in path for path in shared)
    assert [f["path"] for f in after["files"]] == sorted(touched)


def test_members_keep_their_state(client: TestClient, imported: int) -> None:
    detail = created(client, imported, "PROJ-101", "PROJ-102")
    set_state(client, imported, "not_relevant", "PROJ-101")
    states = {m["key"]: m["local_state"] for m in cluster(client, imported, detail["id"])["members"]}
    assert states == {"PROJ-101": "not_relevant", "PROJ-102": "new"}


def test_items_api_shows_and_filters_clusters(client: TestClient, imported: int) -> None:
    detail = created(client, imported, "PROJ-101", "PROJ-102", name="Camera")
    other = created(client, imported, "PROJ-103", "PROJ-104", name="Power")
    page = client.get(f"/api/workspaces/{imported}/items", params={"limit": 500}).json()["items"]
    by_key = {i["key"]: i["cluster"] for i in page}
    assert by_key["PROJ-101"] == {"id": detail["id"], "name": "Camera"}
    assert by_key["PROJ-105"] is None

    assert keys(client, imported, cluster="any") == ["PROJ-101", "PROJ-102", "PROJ-103", "PROJ-104"]
    assert len(keys(client, imported, cluster="none")) == 21
    assert keys(client, imported, cluster=str(other["id"])) == ["PROJ-103", "PROJ-104"]
    assert client.get(f"/api/workspaces/{imported}/items", params={"cluster": "nope"}).status_code == 422


def test_item_detail_shows_cluster_and_linked_clusters(client: TestClient, imported: int) -> None:
    detail = created(client, imported, "PROJ-102", "PROJ-103", name="Settings")
    item = client.get(f"/api/workspaces/{imported}/items/PROJ-101").json()
    assert item["cluster"] is None
    links = {link["issue"]["key"]: link["cluster"] for link in item["links"]}
    assert links == {"PROJ-201": None, "PROJ-102": {"id": detail["id"], "name": "Settings"}}

    item = client.get(f"/api/workspaces/{imported}/items/PROJ-102").json()
    assert item["cluster"] | {"members": []} == {"id": detail["id"], "name": "Settings", "members": []}
    assert [m["key"] for m in item["cluster"]["members"]] == ["PROJ-103", "PROJ-102"]


def test_clusters_are_per_workspace_and_go_with_it(app: FastAPI, client: TestClient, imported: int, tmp_path) -> None:
    detail = created(client, imported, "PROJ-101", "PROJ-102")
    other = tmp_path / "other"
    other.mkdir()
    other_id = client.post("/api/workspaces", json={"name": "Other", "jql": "x", "target_tree": str(other)}).json()["id"]
    assert client.get(f"/api/workspaces/{other_id}/clusters/{detail['id']}").status_code == 404
    assert client.get(f"/api/workspaces/{other_id}/clusters").json() == []

    assert client.delete(f"/api/workspaces/{imported}").status_code == 204
    with session(app) as s:
        assert s.scalar(select(func.count()).select_from(Cluster)) == 0
        assert s.scalar(select(func.count()).select_from(ClusterMember)) == 0
