"""A small in-process Jira REST server for tests: Server/Data Center or Cloud behaviour.

Cloud mode answers 410 Gone on /rest/api/2/search (as Jira Cloud does since the old search
was removed), serves /rest/api/3/search/jql paged by nextPageToken, and returns descriptions
as Atlassian Document Format. Server mode serves /rest/api/2/search paged by startAt.
Every request must carry the expected Authorization header. `throttle` answers that many
requests (from then on) with 429 Too Many Requests and a Retry-After, as Jira's rate limit does.
"""

from __future__ import annotations

import json
import re
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def adf(text: str) -> dict:
    return {"type": "doc", "version": 1, "content": [
        {"type": "paragraph", "content": [{"type": "text", "text": line}]}
        for line in text.split("\n")
    ]}  # fmt: skip


class FakeJira:
    def __init__(self, cloud: bool, auth: str, page_size: int = 2):
        self.cloud = cloud
        self.auth = auth
        self.page_size = page_size
        self.issues: dict[str, dict] = {}
        self.requests: list[str] = []
        self.throttle = 0  # the next N requests get 429
        self.retry_after = "0"
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def add(self, key: str, summary: str, description: str = "", links: list[str] = (),
            status: str = "Open"):  # fmt: skip
        self.issues[key] = {"summary": summary, "description": description, "links": list(links),
                            "labels": [], "comments": [], "remotelinks": {}, "attachments": [],
                            "status": status}  # fmt: skip

    def attach(self, key: str, filename: str, content: str) -> None:
        self.issues[key]["attachments"].append((filename, content.encode()))

    def comment(self, key: str, author: str, body: str) -> None:
        c = {"id": str(len(self.issues[key]["comments"]) + 1), "body": body,
             "author": {"displayName": author}}  # fmt: skip
        self.issues[key]["comments"].append(c)

    def __enter__(self) -> FakeJira:
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *exc) -> None:
        self.server.shutdown()

    def _fields(self, key: str) -> dict:
        i = self.issues[key]
        return {
            "summary": i["summary"],
            "description": adf(i["description"]) if self.cloud and i["description"] else
            i["description"],
            "status": {"name": i["status"]},
            "issuelinks": [{"outwardIssue": {"key": k}} for k in i["links"]],
            "comment": {"comments": [
                {**c, "body": adf(c["body"]) if self.cloud else c["body"]}
                for c in i["comments"]]},
            "attachment": [
                {"filename": name, "size": len(data), "mimeType": "text/x-diff",
                 "content": f"{self.url}/secure/attachment/{key}/{n}/{name}"}
                for n, (name, data) in enumerate(i["attachments"])],
        }  # fmt: skip

    def _search(self, jql: str) -> list[str]:
        m = re.search(r"key in \(([^)]*)\)", jql)
        keys = [k.strip() for k in m.group(1).split(",")] if m else sorted(self.issues)
        return [k for k in keys if k in self.issues]

    def _handler(self):
        jira = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _reply(self, code: int, body=None):
                raw = json.dumps(body).encode() if body is not None else b""
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def _route(self, method: str):
                u = urllib.parse.urlparse(self.path)
                q = dict(urllib.parse.parse_qsl(u.query))
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n)) if n else None
                jira.requests.append(f"{method} {u.path}")
                if jira.throttle > 0:
                    jira.throttle -= 1
                    raw = b'{"errorMessages": ["rate limit exceeded"]}'
                    self.send_response(429)
                    self.send_header("Retry-After", jira.retry_after)
                    self.send_header("Content-Length", str(len(raw)))
                    self.end_headers()
                    self.wfile.write(raw)
                    return
                if self.headers.get("Authorization") != jira.auth:
                    return self._reply(401, {"errorMessages": ["unauthorized"]})
                path = urllib.parse.unquote(u.path)
                if m := re.fullmatch(r"/secure/attachment/([A-Z]+-\d+)/(\d+)/.+", path):
                    raw = jira.issues[m.group(1)]["attachments"][int(m.group(2))][1]
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(raw)))
                    self.end_headers()
                    self.wfile.write(raw)
                    return
                if path == "/rest/api/2/search":
                    if jira.cloud:
                        return self._reply(410, {"errorMessages": ["removed"]})
                    keys = jira._search(q["jql"])
                    start = int(q.get("startAt", 0))
                    page = keys[start : start + min(jira.page_size, int(q["maxResults"]))]
                    return self._reply(200, {"total": len(keys), "startAt": start, "issues": [
                        {"key": k, "fields": jira._fields(k)} for k in page]})  # fmt: skip
                if path == "/rest/api/3/search/jql":
                    if not jira.cloud:
                        return self._reply(404, {})
                    keys = jira._search(q["jql"])
                    start = int(q.get("nextPageToken", 0))
                    size = min(jira.page_size, int(q["maxResults"]))
                    page, end = keys[start : start + size], start + size
                    out = {"issues": [{"key": k, "fields": jira._fields(k)} for k in page],
                           "isLast": end >= len(keys)}  # fmt: skip
                    if end < len(keys):
                        out["nextPageToken"] = str(end)
                    return self._reply(200, out)
                if path == "/rest/api/2/myself":
                    return self._reply(200, {"emailAddress": "bot@example.com", "name": "bot"})
                if m := re.fullmatch(r"/rest/api/2/project/(\w+)", path):
                    return self._reply(200, {"key": m.group(1), "name": "OEM platform"})
                if path == "/rest/api/2/issue" and method == "POST":
                    key = f"OEM-{900 + len(jira.issues)}"
                    f = body["fields"]
                    jira.add(key, f["summary"], f["description"])
                    jira.issues[key]["labels"] = list(f["labels"])
                    return self._reply(201, {"key": key})
                m = re.fullmatch(r"/rest/api/2/issue/([A-Z]+-\d+)"
                                 r"(?:/(comment|remotelink|transitions))?(?:/(\d+))?",
                                 path)  # fmt: skip
                if not m or m.group(1) not in jira.issues:
                    return self._reply(404, {})
                key, sub, cid = m.groups()
                issue = jira.issues[key]
                if sub is None and method == "GET":
                    return self._reply(200, {"key": key, "fields": jira._fields(key)})
                if sub is None and method == "PUT":
                    for op in body["update"]["labels"]:
                        if op["add"] not in issue["labels"]:
                            issue["labels"].append(op["add"])
                    return self._reply(204)
                if sub == "comment" and method == "GET":  # paged, as Jira caps maxResults
                    start = int(q.get("startAt", 0))
                    size = min(jira.page_size, int(q.get("maxResults", 50)))
                    return self._reply(
                        200,
                        {
                            "startAt": start,
                            "total": len(issue["comments"]),
                            "comments": issue["comments"][start : start + size],
                        },
                    )
                if sub == "comment" and method == "POST":
                    c = {"id": str(len(issue["comments"]) + 1), "body": body["body"]}
                    issue["comments"].append(c)
                    return self._reply(201, c)
                if sub == "comment" and method == "PUT":
                    for c in issue["comments"]:
                        if c["id"] == cid:
                            c["body"] = body["body"]
                    return self._reply(200, {})
                if sub == "remotelink" and method == "GET":
                    return self._reply(200, [{"object": o} for o in issue["remotelinks"].values()])
                if sub == "transitions" and method == "GET":
                    open_ = issue["status"] != "Closed"
                    return self._reply(200, {"transitions": [{"id": "31", "name": "Close"}]
                                             if open_ else []})  # fmt: skip
                if sub == "transitions" and method == "POST":
                    issue["status"] = "Closed"
                    issue["resolution"] = ((body.get("fields") or {}).get("resolution") or {}).get(
                        "name"
                    )
                    return self._reply(204)
                if sub == "remotelink" and method == "POST":
                    issue["remotelinks"][body["globalId"]] = body["object"]
                    return self._reply(201, {"id": 1})
                return self._reply(405, {})

            def do_GET(self):
                self._route("GET")

            def do_POST(self):
                self._route("POST")

            def do_PUT(self):
                self._route("PUT")

        return Handler
