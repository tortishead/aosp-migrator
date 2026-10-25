"""Publishing to GitLab and Bitbucket as well as GitHub: one PR shape for every host."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
from sqlalchemy import select

from patchmig.config import load_config
from patchmig.config.schema import OutputConfig, RepoHost, RepoOutput
from patchmig.ledger import Cluster
from patchmig.publish.hosts import (
    HttpBitbucketCloud,
    HttpBitbucketServer,
    HttpGitlab,
    client_for,
    git_url_for,
    host_of,
)
from patchmig.review import sync_reviews
from tests.conftest import MINIMAL_CONFIG
from tests.test_publish_review import REPOS, Published


class Stub:
    """A tiny REST server: answers by (method, path), records every request."""

    def __init__(self):
        self.routes: dict[tuple[str, str], object] = {}
        self.calls: list[dict] = []
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def _answer(self):
                u = urlparse(self.path)
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n)) if n else None
                call = {"method": self.command, "path": u.path, "query": parse_qs(u.query),
                        "body": body, "auth": self.headers.get("Authorization"),
                        "private_token": self.headers.get("PRIVATE-TOKEN")}  # fmt: skip
                stub.calls.append(call)
                answer = stub.routes.get((self.command, u.path))
                if callable(answer):
                    answer = answer(call)
                data = json.dumps(answer).encode()
                self.send_response(404 if answer is None else 200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            do_GET = do_POST = do_PUT = do_PATCH = _answer

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def last(self, method: str, path: str) -> dict:
        return [c for c in self.calls if c["method"] == method and c["path"] == path][-1]


@pytest.fixture
def stub():
    s = Stub()
    yield s
    s.server.shutdown()


def _config(write_config, output: dict, target_host: dict | None = None):
    trees = {**MINIMAL_CONFIG["trees"]}
    if target_host:
        trees["target"] = {**trees["target"], "host": target_host}
    return load_config(write_config({"trees": trees, "output": output}))


def test_output_host_defaults_to_the_target_trees_host(write_config, monkeypatch):
    monkeypatch.setenv("BB", "tok")
    bb = {"kind": "bitbucket_server", "url": "https://bb.oem.com", "token": "env:BB"}
    cfg = _config(write_config, {"target": "bitbucket_server", "bitbucket_server": {
        "owner": "AOSP", "base_branch": "oem-15"}}, bb)  # fmt: skip
    host = host_of(cfg)
    assert (host.kind, host.url, host.token) == ("bitbucket_server", "https://bb.oem.com", "env:BB")
    assert git_url_for(cfg, "platform_frameworks_base") == (
        "https://bb.oem.com/scm/aosp/platform_frameworks_base.git")  # fmt: skip
    assert isinstance(client_for(cfg), HttpBitbucketServer)

    gl = _config(write_config, {"target": "gitlab", "gitlab": {
        "owner": "android/oem", "base_branch": "main", "token": "env:BB"}})  # fmt: skip
    assert host_of(gl).url == "https://gitlab.com"
    assert git_url_for(gl, "car") == "https://gitlab.com/android/oem/car.git"
    # A target host of another kind does not lend its URL or token.
    other = _config(write_config, {"target": "github", "github": {
        "owner": "oem", "base_branch": "main"}}, bb)  # fmt: skip
    assert host_of(other).url == "https://github.com" and host_of(other).token is None
    with pytest.raises(ValueError, match="output.bitbucket_cloud is missing"):
        OutputConfig(target="bitbucket_cloud")


def test_gitlab_merge_requests(stub, monkeypatch):
    monkeypatch.setenv("GL", "gltok")
    owner, repo = "android/oem", "car"
    p = "/api/v4/projects/android%2Foem%2Fcar"
    p_decoded = "/api/v4/projects/android/oem/car"  # http.server decodes %2F in the path
    mr = {"iid": 7, "web_url": "https://gl/mr/7", "state": "opened", "source_branch": "mig/x",
          "sha": "abc", "draft": True, "title": "Draft: t"}  # fmt: skip
    for path in (p, p_decoded):
        stub.routes[("POST", f"{path}/merge_requests")] = mr
        stub.routes[("GET", f"{path}/merge_requests")] = [mr]
        stub.routes[("GET", f"{path}/merge_requests/7")] = mr
        stub.routes[("PUT", f"{path}/merge_requests/7")] = mr
        stub.routes[("GET", f"{path}/merge_requests/7/reviewers")] = [
            {"user": {"username": "lead"}, "state": "requested_changes"},
            {"user": {"username": "dev"}, "state": "unreviewed"}]  # fmt: skip
        stub.routes[("GET", f"{path}/merge_requests/7/notes")] = [
            {"body": "Keep the old timeout", "author": {"username": "lead"}},
            {"body": "added 1 commit", "system": True, "author": {"username": "lead"}},
            {"body": "lgtm", "author": {"username": "boss"}}]  # fmt: skip
        stub.routes[("GET", f"{path}/merge_requests/7/approvals")] = {
            "approved_by": [{"user": {"username": "boss"}}]}  # fmt: skip
    stub.routes[("GET", "/api/v4/users")] = [{"id": 42}]
    api = HttpGitlab(RepoHost(kind="gitlab", url=stub.url, token="env:GL"), owner)
    created = api.create_pr(repo, "t", "body", "mig/x", "main", True)
    assert created == {**created, "number": 7, "state": "open", "merged": False,
                       "html_url": "https://gl/mr/7", "head": "mig/x"}  # fmt: skip
    post = [c for c in stub.calls if c["method"] == "POST"][-1]
    assert post["body"]["title"] == "Draft: t" and post["body"]["target_branch"] == "main"
    assert post["private_token"] == "gltok" and post["auth"] is None
    assert api.find_pr(repo, "mig/x")["number"] == 7
    api.update_pr(repo, 7, "t2", "new body")
    put = [c for c in stub.calls if c["method"] == "PUT"][-1]
    assert put["body"] == {"title": "Draft: t2", "description": "new body"}  # stays a draft
    reviews = api.reviews(repo, 7)
    assert [(r["user"]["login"], r["state"]) for r in reviews] == [
        ("lead", "CHANGES_REQUESTED"),
        ("dev", "COMMENTED"),
        ("boss", "APPROVED"),
    ]
    assert reviews[0]["id"] == "lead:CHANGES_REQUESTED:abc"
    assert reviews[0]["body"] == "Keep the old timeout" and reviews[2]["body"] == ""
    api.request_reviewers(repo, 7, ["lead"])
    assert [c for c in stub.calls if c["method"] == "PUT"][-1]["body"] == {"reviewer_ids": [42]}
    api.add_labels(repo, 7, ["migration", "a15"])
    assert [c for c in stub.calls if c["method"] == "PUT"][-1]["body"] == {
        "add_labels": "migration,a15"}  # fmt: skip
    merged = {**mr, "state": "merged", "merged_by": {"username": "boss"}}
    for path in (p, p_decoded):
        stub.routes[("GET", f"{path}/merge_requests/7")] = merged
    got = api.get_pr(repo, 7)
    assert got["merged"] and got["state"] == "closed" and got["merged_by"]["login"] == "boss"


def test_bitbucket_cloud_pull_requests(stub, monkeypatch):
    monkeypatch.setenv("BBC", "bbtok")
    host = RepoHost(kind="bitbucket_cloud", url="https://bitbucket.org", api_url=stub.url,
                    user="me", token="env:BBC")  # fmt: skip
    api = HttpBitbucketCloud(host, "oem-ws")
    base = "/repositories/oem-ws/car/pullrequests"
    pr = {"id": 3, "state": "OPEN", "title": "t", "links": {"html": {"href": "https://bb/pr/3"}},
          "source": {"branch": {"name": "mig/x"}, "commit": {"hash": "def"}},
          "participants": [
              {"user": {"nickname": "lead", "uuid": "{u1}"}, "state": "changes_requested"},
              {"user": {"nickname": "boss", "uuid": "{u2}"}, "approved": True}]}  # fmt: skip
    stub.routes[("POST", base)] = pr
    stub.routes[("GET", base)] = {"values": [pr]}
    stub.routes[("GET", f"{base}/3")] = pr
    stub.routes[("PUT", f"{base}/3")] = pr
    stub.routes[("GET", f"{base}/3/comments")] = {"values": [
        {"content": {"raw": "Use the new API"}, "user": {"uuid": "{u1}"}},
        {"content": {"raw": "old"}, "user": {"uuid": "{u1}"}, "deleted": True}]}  # fmt: skip
    created = api.create_pr("car", "t", "body", "mig/x", "main", True)
    assert (created["number"], created["html_url"], created["state"]) == (3, "https://bb/pr/3",
                                                                          "open")  # fmt: skip
    post = stub.last("POST", base)
    assert post["body"]["draft"] is True and post["body"]["destination"] == {
        "branch": {"name": "main"}}  # fmt: skip
    assert post["auth"] == "Basic bWU6YmJ0b2s="
    assert api.find_pr("car", "mig/x")["number"] == 3
    q = stub.last("GET", base)["query"]
    assert q["q"] == ['source.branch.name = "mig/x"'] and "DECLINED" in q["state"]
    reviews = api.reviews("car", 3)
    assert [(r["user"]["login"], r["state"]) for r in reviews] == [
        ("lead", "CHANGES_REQUESTED"),
        ("boss", "APPROVED"),
    ]
    assert reviews[0]["body"] == "Use the new API"
    api.request_reviewers("car", 3, ["{u9}", "557058:abc"])
    assert stub.last("PUT", f"{base}/3")["body"]["reviewers"] == [
        {"uuid": "{u9}"}, {"account_id": "557058:abc"}]  # fmt: skip
    api.add_labels("car", 3, ["migration"])  # no labels on Bitbucket: no call
    assert not any(c["path"].endswith("labels") for c in stub.calls)


def test_bitbucket_server_pull_requests(stub, monkeypatch):
    monkeypatch.setenv("BBS", "srvtok")
    api = HttpBitbucketServer(RepoHost(kind="bitbucket_server", url=stub.url, token="env:BBS"),
                              "AOSP")  # fmt: skip
    base = "/rest/api/1.0/projects/AOSP/repos/car/pull-requests"
    pr = {"id": 5, "version": 2, "state": "OPEN", "title": "t",
          "links": {"self": [{"href": "https://bbs/pr/5"}]},
          "fromRef": {"displayId": "mig/x", "latestCommit": "fff"},
          "reviewers": [{"user": {"name": "lead"}, "status": "NEEDS_WORK"}]}  # fmt: skip
    stub.routes[("POST", base)] = pr
    stub.routes[("GET", base)] = {"values": [{**pr, "id": 4, "fromRef": {"displayId": "other"}},
                                             pr]}  # fmt: skip
    stub.routes[("GET", f"{base}/5")] = pr
    stub.routes[("PUT", f"{base}/5")] = pr
    api.create_pr("car", "t", "body", "mig/x", "oem-15", False)
    post = stub.last("POST", base)
    assert post["auth"] == "Bearer srvtok"
    assert post["body"]["toRef"] == {"id": "refs/heads/oem-15", "repository": {
        "slug": "car", "project": {"key": "AOSP"}}}  # fmt: skip
    assert api.find_pr("car", "mig/x")["number"] == 5  # the other branch's PR is skipped
    api.update_pr("car", 5, "t2", "b2")
    put = stub.last("PUT", f"{base}/5")["body"]
    assert put["version"] == 2 and put["reviewers"] == pr["reviewers"]  # kept
    # Without readable comments the review still counts, with no text.
    assert api.reviews("car", 5) == [{"id": "lead:CHANGES_REQUESTED:fff",
                                      "state": "CHANGES_REQUESTED", "user": {"login": "lead"},
                                      "body": ""}]  # fmt: skip
    stub.routes[("GET", f"{base}/5/activities")] = {"values": [
        {"action": "COMMENTED", "user": {"name": "lead"}, "comment": {"text": "Split this"}},
        {"action": "APPROVED", "user": {"name": "boss"}}]}  # fmt: skip
    assert api.reviews("car", 5)[0]["body"] == "Split this"
    api.request_reviewers("car", 5, ["lead", "boss"])
    assert stub.last("PUT", f"{base}/5")["body"]["reviewers"] == [
        {"user": {"name": "lead"}, "status": "NEEDS_WORK"},
        {"user": {"name": "boss"}},
    ]


def test_publish_and_sync_on_gitlab(tmp_path: Path, monkeypatch):
    """The publisher and review sync work the same on any host (fake API, local repos)."""
    monkeypatch.setenv("GH_TOKEN", "test-token")
    p = Published(tmp_path)
    gl = RepoOutput(owner="oem", token="env:GH_TOKEN", repo_map=REPOS, base_branch="main",
                    git_url=str(p.hub / "{repo}.git"), reviewers=["lead"])  # fmt: skip
    p.config = p.config.model_copy(update={"output": OutputConfig(target="gitlab", gitlab=gl)})
    p.approve_verified()
    report = p.publish()
    assert report.outcomes and all(o.status == "published" for o in report.outcomes)
    repo = next(r for r, v in p.gh.prs.items() if v)
    p.gh.review_list[(repo, 1)] = [{"id": "lead:CHANGES_REQUESTED:abc",
                                    "state": "CHANGES_REQUESTED", "user": {"login": "lead"},
                                    "body": ""}]  # fmt: skip
    changes = sync_reviews(p.store, p.run, p.gh, "gitlab")
    assert any("rejected by lead" in c for c in changes)
    with p.store.session() as s:
        rows = s.scalars(select(Cluster).where(Cluster.run_id == p.run.id)).all()
    assert any(f.get("source") == "gitlab" for c in rows for f in c.result.get("feedback", []))
