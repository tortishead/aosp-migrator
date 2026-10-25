from typing import Any

from fake_jira import FakeJiraStore
from fastapi import FastAPI
from fastapi.testclient import TestClient
from test_import import app, client, client_factory, jira_config, run_import, workspace_id  # noqa: F401
from aosp_migrator.config import JIRA_BASE_URL
from test_items_api import imported, set_state  # noqa: F401


def detail(client: TestClient, workspace_id: int, key: str) -> dict[str, Any]:
    response = client.get(f"/api/workspaces/{workspace_id}/items/{key}")
    assert response.status_code == 200, response.text
    return response.json()


def test_issue_comments_and_attachments(client: TestClient, imported: int, jira_url: str) -> None:
    item = detail(client, imported, "PROJ-101")
    assert (item["local_state"], item["dropped"], item["last_import_error"]) == ("new", False, None)
    issue = item["issue"]
    assert issue["summary"] == "Camera HAL crash on resume"
    assert issue["browse_url"] == f"{jira_url}/browse/PROJ-101"
    assert item["jira_base_url"] == jira_url
    assert "<table class='confluenceTable'>" in issue["description_html"]
    # The embedded screenshot points at the local copy.
    screenshot = next(a for a in issue["attachments"] if a["filename"] == "screenshot.png")
    assert f'src="{screenshot["local_url"]}"' in issue["description_html"]
    assert [c["author"] for c in issue["comments"]] == ["Raj Patel", "Jane Doe"]

    patches = [a for a in issue["attachments"] if a["kind"] == "diff"]
    # Oldest first; only the newest version is current.
    assert [a["is_current"] for a in patches] == [False, True]
    assert patches[1]["parsed"]["format"] == "format-patch"
    assert patches[1]["local_url"].startswith(f"/api/workspaces/{imported}/attachments/{patches[1]['jira_id']}/")
    assert client.get(patches[1]["local_url"]).text.startswith("From ")
    assert patches[1]["jira_url"].startswith(jira_url)


def test_not_downloaded_attachment_has_no_local_url(client: TestClient, imported: int) -> None:
    attachments = detail(client, imported, "PROJ-102")["issue"]["attachments"]
    archive = next(a for a in attachments if a["filename"] == "logs.zip")
    assert (archive["kind"], archive["local_url"]) == ("other", None)
    assert archive["size"] > 0


def test_links(client: TestClient, imported: int) -> None:
    links = detail(client, imported, "PROJ-101")["links"]
    by_key = {link["issue"]["key"]: link for link in links}
    assert set(by_key) == {"PROJ-201", "PROJ-102"}
    # Linked-only ticket: full fields and attachments, but no backlog item.
    audio = by_key["PROJ-201"]
    assert (audio["link_type"], audio["item_id"]) == ("Relates", None)
    assert audio["issue"]["status"] == "Closed"
    assert [a["filename"] for a in audio["issue"]["attachments"]] == ["audio-guard.patch"]
    # Linked ticket that is also a backlog item points at it.
    settings = by_key["PROJ-102"]
    assert settings["description"] == "blocks"
    assert settings["item_id"] == detail(client, imported, "PROJ-102")["id"]


def test_unknown_or_linked_only_key_is_404(client: TestClient, imported: int) -> None:
    assert client.get(f"/api/workspaces/{imported}/items/PROJ-999").status_code == 404
    assert client.get(f"/api/workspaces/{imported}/items/PROJ-201").status_code == 404
    assert client.get("/api/workspaces/999/items/PROJ-101").status_code == 404


def test_notes_append_and_show_in_history(client: TestClient, imported: int) -> None:
    url = f"/api/workspaces/{imported}/items/PROJ-102/notes"
    first = client.post(url, json={"text": "  Looks upstreamed already.  "})
    assert first.status_code == 201, first.text
    assert first.json()["text"] == "Looks upstreamed already."
    assert client.post(url, json={"text": "Checked: it is not."}).status_code == 201
    assert client.post(url, json={"text": "   "}).status_code == 422
    assert client.post(url, json={"text": ""}).status_code == 422
    assert client.post(f"/api/workspaces/{imported}/items/PROJ-999/notes", json={"text": "x"}).status_code == 404

    item = detail(client, imported, "PROJ-102")
    assert [n["text"] for n in item["notes"]] == ["Looks upstreamed already.", "Checked: it is not."]
    notes = [h for h in item["history"] if h["type"] == "note_added"]
    assert [h["payload"]["text"] for h in notes] == ["Looks upstreamed already.", "Checked: it is not."]
    assert notes[0]["payload"]["note_id"] == item["notes"][0]["id"]


def test_history_in_order(client: TestClient, imported: int, fake_jira: FakeJiraStore) -> None:
    set_state(client, imported, "not_relevant", "PROJ-103")
    fake_jira.issues["PROJ-103"].summary = "Battery history gap"
    assert run_import(client, imported, ["PROJ-103"])["status"] == "done"
    item = detail(client, imported, "PROJ-103")
    assert [h["type"] for h in item["history"]] == ["imported", "state_changed", "updated"]
    assert item["history"][2]["payload"]["fields"]["summary"]["new"] == "Battery history gap"
    # Reimport keeps local state.
    assert item["local_state"] == "not_relevant"


def test_no_browse_url_without_jira_settings(app: FastAPI, client: TestClient, imported: int) -> None:
    app.state.config.set({JIRA_BASE_URL: None})
    assert detail(client, imported, "PROJ-104")["issue"]["browse_url"] is None


def test_diff_with_hunks(client: TestClient, imported: int) -> None:
    attachments = detail(client, imported, "PROJ-102")["issue"]["attachments"]
    diff = next(a for a in attachments if a["filename"] == "settings.diff")
    response = client.get(f"/api/workspaces/{imported}/diffs/{diff['jira_id']}")
    assert response.status_code == 200, response.text
    files = response.json()["files"]
    assert [f["status"] for f in files] == ["modified", "added", "renamed", "modified"]
    assert files[0]["hunks"][0].startswith("@@ -88,6 +88,7 @@")

    archive = next(a for a in attachments if a["filename"] == "logs.zip")
    assert client.get(f"/api/workspaces/{imported}/diffs/{archive['jira_id']}").status_code == 404
    assert client.get(f"/api/workspaces/{imported}/diffs/999999").status_code == 404
