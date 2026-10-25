import hashlib
import socket
from collections.abc import Iterator
from pathlib import Path

import pytest
from fake_jira import FakeJiraStore
from fake_jira.fixtures import fixture_file

from aosp_migrator.config import JIRA_BASE_URL, JIRA_PAT, Config
from aosp_migrator.jira import JiraNotConfiguredError, client_from_config
from aosp_migrator.jira.base import JiraAuthError, JiraConnectionError, JiraError, JiraNotFoundError
from aosp_migrator.jira.datacenter import DataCenterClient


@pytest.fixture
def store(fake_jira: FakeJiraStore) -> FakeJiraStore:
    return fake_jira


@pytest.fixture
def sleeps() -> list[float]:
    return []


@pytest.fixture
def client(jira_url: str, store: FakeJiraStore, sleeps: list[float]) -> Iterator[DataCenterClient]:
    with DataCenterClient(jira_url, store.pat, sleep=sleeps.append) as client:
        yield client


def search_requests(store: FakeJiraStore) -> list[str]:
    return [r for r in store.requests if r.startswith("POST /rest/api/2/search")]


def test_myself(client: DataCenterClient) -> None:
    assert client.myself() == "Jane Doe"


def test_wrong_pat_is_auth_error(jira_url: str) -> None:
    with DataCenterClient(jira_url, "wrong") as client:
        with pytest.raises(JiraAuthError) as exc_info:
            client.myself()
    assert exc_info.value.status == 401


def test_search_pages_by_returned_count_when_server_caps_page_size(client: DataCenterClient, store: FakeJiraStore) -> None:
    store.max_page_size = 10
    pages = list(client.search("project = PROJ", page_size=50))
    assert [len(p.issues) for p in pages] == [10, 10, 5]
    assert [p.start_at for p in pages] == [0, 10, 20]
    assert all(p.total == 25 for p in pages)
    keys = [issue.key for page in pages for issue in page.issues]
    assert keys == [f"PROJ-{n}" for n in range(101, 126)]
    assert len(search_requests(store)) == 3


def test_search_uses_requested_page_size(client: DataCenterClient, store: FakeJiraStore) -> None:
    pages = list(client.search("project = PROJ", page_size=7))
    assert [len(p.issues) for p in pages] == [7, 7, 7, 4]


def test_search_with_no_results(client: DataCenterClient, store: FakeJiraStore) -> None:
    for issue in store.issues.values():
        issue.in_jql = False
    pages = list(client.search("project = NONE"))
    assert len(pages) == 1
    assert pages[0].total == 0 and pages[0].issues == []


def test_search_returns_rendered_fields_and_metadata(client: DataCenterClient, jira_url: str) -> None:
    issues = {issue.key: issue for page in client.search("project = PROJ") for issue in page.issues}
    camera = issues["PROJ-101"]
    assert camera.summary == "Camera HAL crash on resume"
    assert "||Device||Build||" in camera.description_raw
    assert "<table class='confluenceTable'>" in camera.description_html
    assert "/secure/attachment/" in camera.description_html
    assert camera.status == "In Progress"
    assert camera.labels == ["oem-patch", "camera"]
    assert camera.components == ["Camera"]
    assert camera.fix_versions == ["2026.Q4"]
    assert camera.updated.isoformat() == "2026-09-25T16:00:00+00:00"

    patches = [a for a in camera.attachments if a.filename == "0001-camera-fix-null-deref.patch"]
    assert len(patches) == 2
    assert patches[0].created < patches[1].created
    assert patches[0].url.startswith(f"{jira_url}/secure/attachment/")
    assert patches[0].size == len(fixture_file("camera-fix.v1.patch"))

    links = {(link.link_type, link.direction, link.description, link.key) for link in camera.issue_links}
    assert links == {("Relates", "outward", "relates to", "PROJ-201"), ("Blocks", "outward", "blocks", "PROJ-102")}
    assert [(link.direction, link.description, link.key) for link in issues["PROJ-102"].issue_links] == [
        ("inward", "is blocked by", "PROJ-101")
    ]


def test_get_issue_outside_jql(client: DataCenterClient) -> None:
    issue = client.get_issue("PROJ-201")
    assert issue.summary == "Audio HAL callback after stream release"
    assert issue.description_html == "<p>Upstream fix this camera issue depends on.</p>"
    assert [a.filename for a in issue.attachments] == ["audio-guard.patch"]
    assert issue.issue_links[0].key == "PROJ-101"


def test_get_issue_missing(client: DataCenterClient) -> None:
    with pytest.raises(JiraNotFoundError):
        client.get_issue("PROJ-999")


def test_get_comments_rendered_and_paginated(client: DataCenterClient, store: FakeJiraStore) -> None:
    store.max_page_size = 1
    comments = client.get_comments("PROJ-101")
    assert [c.author for c in comments] == ["Raj Patel", "Jane Doe"]
    assert comments[0].body_html == "<p>Reproduced on <b>AP2A</b>. Log attached.</p>"
    assert comments[0].created < comments[1].created
    assert len([r for r in store.requests if "/comment" in r]) == 2


def test_get_comments_none(client: DataCenterClient) -> None:
    assert client.get_comments("PROJ-110") == []


def test_download_attachment(client: DataCenterClient, tmp_path: Path) -> None:
    patch = client.get_issue("PROJ-104").attachments[0]
    dest = tmp_path / "a" / "icons.patch"
    digest = client.download_attachment(patch.url, dest)
    content = fixture_file("icons.patch")
    assert dest.read_bytes() == content
    assert digest == hashlib.sha256(content).hexdigest()
    assert not list(dest.parent.glob("*.part"))


def test_download_relative_url_from_rendered_html(client: DataCenterClient, store: FakeJiraStore, tmp_path: Path) -> None:
    screenshot = next(a for a in store.issues["PROJ-101"].attachments if a.filename == "screenshot.png")
    client.download_attachment(f"/secure/attachment/{screenshot.id}/screenshot.png", tmp_path / "s.png")
    assert (tmp_path / "s.png").read_bytes() == screenshot.content


def test_download_refuses_other_host(client: DataCenterClient, store: FakeJiraStore, tmp_path: Path) -> None:
    with pytest.raises(JiraError, match="Refusing"):
        client.download_attachment("https://evil.example.com/secure/attachment/1/x.patch", tmp_path / "x")
    assert store.requests == []


def test_download_missing_attachment(client: DataCenterClient, jira_url: str, tmp_path: Path) -> None:
    with pytest.raises(JiraNotFoundError):
        client.download_attachment(f"{jira_url}/secure/attachment/1/nope.patch", tmp_path / "nope")
    assert not (tmp_path / "nope").exists()


def test_retries_on_429_and_5xx(client: DataCenterClient, store: FakeJiraStore, sleeps: list[float]) -> None:
    store.fail_next = [503, 429, 502]
    assert client.myself() == "Jane Doe"
    # Backoff 1s, then Retry-After: 0 from the 429, then backoff 4s.
    assert sleeps == [1.0, 0.0, 4.0]
    assert len(store.requests) == 4


def test_retry_mid_pagination(client: DataCenterClient, store: FakeJiraStore) -> None:
    store.max_page_size = 10
    pages = client.search("project = PROJ")
    next(pages)
    store.fail_next = [500]
    keys = [issue.key for page in pages for issue in page.issues]
    assert len(keys) == 15
    assert len(search_requests(store)) == 4


def test_retry_on_download(client: DataCenterClient, store: FakeJiraStore, tmp_path: Path) -> None:
    url = client.get_issue("PROJ-104").attachments[0].url
    store.fail_next = [503]
    client.download_attachment(url, tmp_path / "icons.patch")
    assert (tmp_path / "icons.patch").read_bytes() == fixture_file("icons.patch")


def test_gives_up_after_max_attempts(client: DataCenterClient, store: FakeJiraStore, sleeps: list[float]) -> None:
    store.fail_next = [503] * 10
    with pytest.raises(JiraError) as exc_info:
        client.myself()
    assert exc_info.value.status == 503
    assert len(store.requests) == 4
    assert len(sleeps) == 3


def test_no_retry_on_client_errors(client: DataCenterClient, store: FakeJiraStore, sleeps: list[float]) -> None:
    store.fail_next = [400]
    with pytest.raises(JiraError) as exc_info:
        client.myself()
    assert exc_info.value.status == 400
    assert "Injected failure 400" in str(exc_info.value)
    assert sleeps == []


def test_unreachable_retries_then_connection_error(sleeps: list[float]) -> None:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    with DataCenterClient(f"http://127.0.0.1:{port}", "pat", sleep=sleeps.append) as client:
        with pytest.raises(JiraConnectionError):
            client.myself()
    assert sleeps == [1.0, 2.0, 4.0]


def test_client_from_config(config: Config, jira_url: str, store: FakeJiraStore) -> None:
    with pytest.raises(JiraNotConfiguredError):
        client_from_config(config)
    config.set({JIRA_BASE_URL: jira_url, JIRA_PAT: store.pat})
    client = client_from_config(config)
    try:
        assert client.myself() == "Jane Doe"
    finally:
        client.close()
