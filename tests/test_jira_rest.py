from __future__ import annotations

import base64

import pytest
import yaml
from typer.testing import CliRunner

from patchmig.cli import app
from patchmig.config.schema import JiraConfig
from patchmig.jira_client import HttpJiraClient, JiraError, adf_text, is_cloud
from patchmig.jira_export import export
from tests.fixtures.jira_server import FakeJira, adf


def basic(user: str, token: str) -> str:
    return "Basic " + base64.b64encode(f"{user}:{token}".encode()).decode()


@pytest.fixture(params=["server", "cloud"])
def jira(request, monkeypatch):
    monkeypatch.setenv("JIRA_TOKEN", "t0ken")
    cloud = request.param == "cloud"
    auth = basic("bot@example.com", "t0ken") if cloud else "Bearer t0ken"
    with FakeJira(cloud=cloud, auth=auth) as fake:
        for n in range(1, 6):
            fake.add(f"OEM-{n}", f"Ticket {n}", f"Line one of {n}\nLine two", links=["OEM-1"])
        cfg = JiraConfig(url=fake.url, project="OEM", token="env:JIRA_TOKEN",
                         deployment=request.param,
                         user="bot@example.com" if cloud else None)  # fmt: skip
        yield fake, HttpJiraClient(cfg), cfg


def test_deployment_is_detected_from_the_url():
    def cfg(url, deployment="auto"):
        return JiraConfig(url=url, project="OEM", token="env:X", deployment=deployment)

    assert is_cloud(cfg("https://oem.atlassian.net"))
    assert not is_cloud(cfg("https://jira.oem.example"))
    assert is_cloud(cfg("https://jira.oem.example", "cloud"))
    assert not is_cloud(cfg("https://oem.atlassian.net", "server"))


def test_search_pages_through_all_results_on_server_and_cloud(jira):
    fake, client, _ = jira
    tickets = client.search("project = OEM")
    assert [t.key for t in tickets] == [f"OEM-{n}" for n in range(1, 6)]  # 3 pages of 2
    assert tickets[1].description == "Line one of 2\nLine two"  # ADF flattened on Cloud
    assert tickets[1].issue_links == ["OEM-1"]
    endpoint = "/rest/api/3/search/jql" if fake.cloud else "/rest/api/2/search"
    assert fake.requests.count(f"GET {endpoint}") == 3
    assert client.get("OEM-3").summary == "Ticket 3"
    assert client.get("OEM-99") is None


def test_export_writes_through_rest(jira):
    """The writes the Jira export uses, on both deployments."""
    fake, client, _ = jira
    client.add_labels("OEM-2", ["mig-A14-A15-migrated"])
    client.add_labels("OEM-2", ["mig-A14-A15-migrated"])
    cid = client.add_comment("OEM-2", "[migration-agent] mig-A14-A15\nfirst")
    client.update_comment("OEM-2", cid, "[migration-agent] mig-A14-A15\nsecond")
    client.add_link("OEM-2", "https://github.example/pr/1", "PR 1", "patchmig:x#1")
    client.add_link("OEM-2", "https://github.example/pr/1", "PR 1", "patchmig:x#1")
    key = client.create_issue("OEM", "Task", "[mig] power", "tracking", ["mig-A14-A15-migrated"])
    issue = fake.issues["OEM-2"]
    assert issue["labels"] == ["mig-A14-A15-migrated"]
    assert client.comments("OEM-2") == [
        {"id": cid, "body": "[migration-agent] mig-A14-A15\nsecond"}
    ]
    assert list(issue["remotelinks"]) == ["patchmig:x#1"]
    assert fake.issues[key]["labels"] == ["mig-A14-A15-migrated"]


def test_comments_are_read_past_the_first_page(jira):
    """The export finds its marker comment by reading them all: else it would add another."""
    fake, client, _ = jira
    for n in range(5):
        fake.comment("OEM-3", "bot", f"comment {n}")
    assert [c["body"] for c in client.comments("OEM-3")] == [f"comment {n}" for n in range(5)]
    assert fake.requests.count("GET /rest/api/2/issue/OEM-3/comment") == 3  # pages of 2


def test_wrong_endpoint_or_token_says_what_to_fix(jira, monkeypatch):
    fake, _, cfg = jira
    if fake.cloud:  # a Cloud site configured as Server hits the removed search
        wrong = HttpJiraClient(cfg.model_copy(update={"deployment": "server"}))
        with pytest.raises(JiraError, match="HTTP 410.*jira.deployment: cloud"):
            wrong.search("project = OEM")
    monkeypatch.setenv("JIRA_TOKEN", "wrong")
    with pytest.raises(JiraError, match="HTTP 401.*check the token"):
        HttpJiraClient(cfg).search("project = OEM")


def test_jira_check_cli(jira, tmp_path):
    fake, _, cfg = jira
    from tests.test_migrate import Project

    p = Project(tmp_path, jira=cfg.model_dump(mode="json", exclude_none=True))
    res = CliRunner().invoke(app, ["jira-check", "-c", str(p.config_path)])
    assert res.exit_code == 0, res.output
    assert "signed in as bot@example.com" in res.output
    assert ("Cloud" if fake.cloud else "Server/Data Center") in res.output
    assert "project OEM: OEM platform" in res.output and "JQL search works" in res.output

    bad = yaml.safe_load(p.config_path.read_text())
    bad["jira"]["url"] = fake.url + "/wrong"  # not the Jira base URL
    p.config_path.write_text(yaml.safe_dump(bad))
    res = CliRunner().invoke(app, ["jira-check", "-c", str(p.config_path)])
    assert res.exit_code == 1 and "FAIL" in res.output


def test_full_export_against_rest(tmp_path, monkeypatch):
    """migrate jira-export's writes, end to end, through HTTP on Jira Cloud."""
    from tests.test_publish_review import Published

    monkeypatch.setenv("GH_TOKEN", "test-token")
    monkeypatch.setenv("JIRA_TOKEN", "t0ken")
    p = Published(tmp_path)
    p.reviewer().approve(p.cluster_of("P3").id, "xuan")  # reviewed first, then published
    p.publish()
    with FakeJira(cloud=True, auth=basic("bot@example.com", "t0ken")) as fake:
        fake.add("OEM-202", "BT timeout")
        cfg = p.config.model_copy(update={"jira": p.config.jira.model_copy(update={
            "url": fake.url, "deployment": "cloud", "user": "bot@example.com"})})  # fmt: skip
        report = export(cfg, p.store, p.run, HttpJiraClient(cfg.jira))
        assert report.updated == ["OEM-202"] and not report.errors
        export(cfg, p.store, p.run, HttpJiraClient(cfg.jira))
        issue = fake.issues["OEM-202"]
        assert issue["labels"] == ["mig-A14-A15-migrated"]
        assert len(issue["comments"]) == 1 and len(issue["remotelinks"]) == 1


def test_adf_text():
    assert adf_text(adf("a\nb")) == "a\nb\n"
    para = [{"type": "text", "text": "x"}, {"type": "hardBreak"}, {"type": "text", "text": "y"}]
    doc = {"type": "doc", "content": [{"type": "paragraph", "content": para}]}
    assert adf_text(doc) == "x\ny\n"
    assert adf_text(None) == "" and adf_text("plain") == "plain"
