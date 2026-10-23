"""Patches and their context from Jira: attachments, linked tickets, code-host links, closing."""

from __future__ import annotations

import json
import sqlite3
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from sqlalchemy import select

from patchmig.agents.intent import build_brief
from patchmig.config import load_config
from patchmig.config.schema import JiraCloseConfig
from patchmig.ingest import ingest
from patchmig.ingest.codehosts import find_refs, gitlab_unified, parse_ref
from patchmig.jira_client import HttpJiraClient
from patchmig.jira_export import export, plan
from patchmig.ledger import LedgerStore, Patch, State
from patchmig.ledger.models import Ticket as TicketRow
from tests.fixtures.jira_server import FakeJira
from tests.fixtures.scenario import BT_OLD, FRAMEWORKS
from tests.test_ingest import make_config  # noqa: F401  (fixture)

AUTH = "Bearer t0ken"
# A change to a file that exists only in frameworks/base, as an OEM engineer attaches it.
ATTACHED = f"""diff --git a/{BT_OLD} b/{BT_OLD}
--- a/{BT_OLD}
+++ b/{BT_OLD}
@@ -1,3 +1,4 @@
 package com.android.server;
+// OEM: log reconnects

 public class BtManager {{
"""


class FakeGithub:
    """GET-only GitHub API: one PR whose commit is not in the source tree (squash-merged)."""

    def __init__(self):
        self.requests: list[str] = []
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                fake.requests.append(self.path)
                if self.headers.get("Authorization") != "Bearer gh-t0ken":
                    return self._send(401, b"{}")
                if self.path == "/api/v3/repos/oem/fw/pulls/7":
                    if self.headers["Accept"] == "application/vnd.github.diff":
                        return self._send(200, ATTACHED.replace("log reconnects", "PR 7").encode())
                    return self._send(
                        200,
                        json.dumps(
                            {"title": "Log BT reconnects", "body": "For car head units"}
                        ).encode(),
                    )
                if self.path.startswith("/api/v3/repos/oem/fw/pulls/7/commits"):
                    return self._send(
                        200, json.dumps([{"sha": "f" * 40, "commit": {"message": "wip"}}]).encode()
                    )
                self._send(404, b"{}")

            def _send(self, code, data):
                self.send_response(code)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()


@pytest.fixture
def github():
    gh = FakeGithub()
    yield gh
    gh.server.shutdown()


def test_link_parsing_for_all_hosts():
    hosts = []
    text = """See https://github.com/oem/fw/commit/abcdef1234567 and
    https://gitlab.com/oem/platform/fw/-/merge_requests/12, also
    https://bitbucket.org/oem/fw/pull-requests/3/diff and
    https://git.oem.example/projects/AND/repos/fw/commits/0123456789abcdef. Not a link: abc."""
    refs = find_refs(text, hosts)
    assert [(r.kind, r.repo, r.sha, r.number) for r in refs] == [
        ("github", "oem/fw", "abcdef1234567", None),
        ("gitlab", "oem/platform/fw", None, 12),
        ("bitbucket_cloud", "oem/fw", None, 3),
        ("bitbucket_server", "AND/fw", "0123456789abcdef", None),
    ]
    assert parse_ref("https://example.com/about", hosts) is None


def test_gitlab_json_diff_becomes_unified():
    changed = {"old_path": "a.txt", "new_path": "a.txt", "diff": "@@ -1 +1 @@\n-x\n+y"}
    added = {"old_path": "n.txt", "new_path": "n.txt", "new_file": True,
             "diff": "@@ -0,0 +1 @@\n+n\n"}  # fmt: skip
    diff = gitlab_unified([changed, added])
    assert diff.startswith("diff --git a/a.txt b/a.txt\n--- a/a.txt\n+++ b/a.txt\n@@")
    assert "new file mode 100644\n--- /dev/null\n+++ b/n.txt\n" in diff


def test_jira_query_reads_attachments_links_and_linked_tickets(
    scenario,
    make_config,  # noqa: F811
    github,
    monkeypatch,
):
    monkeypatch.setenv("JIRA_TOKEN", "t0ken")
    monkeypatch.setenv("GH_TOKEN", "gh-t0ken")
    p3 = scenario.commits["P3"]
    with FakeJira(cloud=False, auth=AUTH) as jira:
        # Migration ticket: an attached diff, and a link to its implementation ticket.
        jira.add("MIG-1", "Migrate BT logging", "Track the BT patches", links=["IMP-1"])
        jira.attach("MIG-1", "bt-logging.diff", ATTACHED)
        jira.attach("MIG-1", "screenshot.png", "not a diff")
        # Implementation ticket on Android 14: a commit link (in the source tree) in its
        # text, and a PR link (squash-merged elsewhere) in a comment.
        jira.add("IMP-1", "BT reconnect timeout", f"Done in https://github.com/oem/fw/commit/{p3}")
        jira.comment("IMP-1", "Dev", f"Follow-up: {github.url}/oem/fw/pull/7")
        cfg = load_config(make_config(
            [{"type": "jira_query", "jql": "project = MIG"}],
            jira={"url": jira.url, "project": "MIG", "token": "env:JIRA_TOKEN"},
            code_hosts=[{"url": github.url, "kind": "github", "token": "env:GH_TOKEN",
                         "repo_map": {"oem/fw": FRAMEWORKS}}],
        ))  # fmt: skip
        store = LedgerStore.in_memory()
        run = store.create_run(cfg)
        report = ingest(cfg, store, run, jira=HttpJiraClient(cfg.jira))

    with store.session() as s:
        rows = s.scalars(select(Patch).where(Patch.run_id == run.id).order_by(Patch.seq)).all()
        imp = s.get(TicketRow, "IMP-1")
    assert report.total == 3, report.warnings
    commit = next(r for r in rows if r.commit_sha == p3)
    assert set(commit.jira_keys) >= {"MIG-1", "IMP-1"}
    attached = next(r for r in rows if r.diff_path and r.diff_path.endswith("bt-logging.diff"))
    assert attached.project == FRAMEWORKS and attached.subject == "Migrate BT logging"
    pr = next(r for r in rows if r.subject == "Log BT reconnects")
    assert pr.project == FRAMEWORKS and "For car head units" in pr.message
    assert "PR 7" in Path(pr.diff_path).read_text()
    assert any("/pulls/7/commits" in r for r in github.requests)
    assert imp.comments == [f"Dev: Follow-up: {github.url}/oem/fw/pull/7"]


def test_intent_brief_has_comments_and_linked_tickets(make_config):  # noqa: F811
    cfg = load_config(make_config([{"type": "git_range"}]))
    t = TicketRow(key="OEM-1", summary="Power mode", description="Need OEM sleep", status="Done",
                  issue_links=["OEM-2"], comments=[f"a: c{i}" for i in range(8)])  # fmt: skip
    linked = TicketRow(key="OEM-2", summary="Car power spec", description="Spec text",
                       status="Open", issue_links=[], comments=[])  # fmt: skip
    patch = Patch(project=FRAMEWORKS, subject="Add OEM power mode", message="m", jira_keys=[])
    brief = build_brief(cfg, patch, [t], {"OEM-2": linked})
    assert "Latest comments:\n- a: c3" in brief and "a: c2" not in brief  # last five
    assert "Linked OEM-2: Car power spec (Open)\nSpec text" in brief


def test_old_ledger_gets_new_columns(tmp_path):
    path = tmp_path / "ledger.db"
    LedgerStore.open(path)
    with sqlite3.connect(path) as db:  # as written by a version before ticket comments
        db.execute("ALTER TABLE tickets DROP COLUMN comments")
    store = LedgerStore.open(path)
    with store.session() as s, s.begin():
        s.add(TicketRow(key="OEM-1", summary="x", comments=["a: b"]))
    with store.session() as s:
        assert s.get(TicketRow, "OEM-1").comments == ["a: b"]


def test_export_closes_tickets_only_when_every_patch_is_confirmed_not_needed(tmp_path, monkeypatch):
    from tests.test_publish_review import Published

    monkeypatch.setenv("GH_TOKEN", "test-token")
    monkeypatch.setenv("JIRA_TOKEN", "t0ken")
    p = Published(tmp_path)
    r = p.reviewer()
    for key in ("P1", "P5", "P7"):  # power (migrated), revert pair, brightness (upstream)
        r.approve(p.cluster_of(key).id, "xuan")
    with FakeJira(cloud=False, auth=AUTH) as jira:
        for key in ("OEM-101", "OEM-404", "OEM-505", "OEM-606", "OEM-808"):
            jira.add(key, key)
        jira.add("OEM-202", "BT", status="Closed")
        close = JiraCloseConfig(transition="Close", resolution="Won't Do")
        cfg = p.config.model_copy(update={"jira": p.config.jira.model_copy(update={
            "url": jira.url, "close_not_needed": close})})  # fmt: skip
        planned = {t.key: t.close for t in plan(cfg, p.store, p.run)}
        assert planned == {"OEM-101": False, "OEM-404": True, "OEM-505": True,
                           "OEM-606": False, "OEM-808": True}  # fmt: skip
        report = export(cfg, p.store, p.run, HttpJiraClient(cfg.jira))
        assert sorted(report.closed) == ["OEM-404", "OEM-505", "OEM-808"]
        assert jira.issues["OEM-505"]["resolution"] == "Won't Do"
        assert jira.issues["OEM-101"]["status"] == "Open"
        again = export(cfg, p.store, p.run, HttpJiraClient(cfg.jira))
        assert again.closed == [] and any("already closed" in s for s in again.skipped)
    assert p.patch("P5").state == State.REVERTED
