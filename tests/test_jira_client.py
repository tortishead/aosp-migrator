from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

import pytest

from patchmig.config.schema import JiraConfig
from patchmig.jira_client import HttpJiraClient, JiraError

ISSUES = [
    {
        "key": f"OEM-{i}",
        "fields": {
            "summary": f"Ticket {i}",
            "description": "why",
            "status": {"name": "Done"},
            "issuelinks": [{"outwardIssue": {"key": "OEM-1"}}] if i == 2 else [],
        },
    }
    for i in range(1, 4)
]


class Handler(BaseHTTPRequestHandler):
    seen_auth: list[str] = []

    def log_message(self, *args):  # keep test output quiet
        pass

    def do_GET(self):
        Handler.seen_auth.append(self.headers.get("Authorization", ""))
        url = urlparse(self.path)
        q = parse_qs(url.query)
        if url.path == "/rest/api/2/search":
            start = int(q["startAt"][0])
            body = {"issues": ISSUES[start : start + 2], "total": len(ISSUES)}  # pages of 2
        elif url.path == "/rest/api/2/issue/OEM-2":
            body = ISSUES[1]
        elif url.path == "/rest/api/2/issue/OEM-500":
            self.send_response(500)
            self.end_headers()
            return
        else:
            self.send_response(404)
            self.end_headers()
            return
        data = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(data)


@pytest.fixture
def jira_url():
    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


def test_search_pages_and_get(jira_url, monkeypatch):
    monkeypatch.setenv("PATCHMIG_TEST_JIRA", "tok")
    client = HttpJiraClient(JiraConfig(url=jira_url, project="OEM", token="env:PATCHMIG_TEST_JIRA"))
    tickets = client.search("project = OEM")
    assert [t.key for t in tickets] == ["OEM-1", "OEM-2", "OEM-3"]
    ticket = client.get("OEM-2")
    assert ticket.summary == "Ticket 2" and ticket.status == "Done"
    assert ticket.issue_links == ["OEM-1"]
    assert client.get("OEM-404") is None
    with pytest.raises(JiraError, match="HTTP 500"):
        client.get("OEM-500")
    assert Handler.seen_auth[-1] == "Bearer tok"


def test_basic_auth_when_user_set(jira_url, monkeypatch):
    monkeypatch.setenv("PATCHMIG_TEST_JIRA", "tok")
    cfg = JiraConfig(url=jira_url, project="OEM", user="bot", token="env:PATCHMIG_TEST_JIRA")
    HttpJiraClient(cfg).get("OEM-2")
    assert Handler.seen_auth[-1].startswith("Basic ")


def test_unreachable_jira():
    cfg = JiraConfig(url="http://127.0.0.1:9", project="OEM", token="env:HOME")
    with pytest.raises(JiraError, match="not reachable"):
        HttpJiraClient(cfg, timeout=2).get("OEM-1")
