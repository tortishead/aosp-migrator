"""Minimal Jira REST client: only what the tool needs (reads for ingest, writes for export).

Writes use only standard fields (labels, comments, remote links, new issues): no custom fields.

Jira Server/Data Center and Jira Cloud both serve REST v2 with plain-text bodies, which is
what we use. The one difference that matters: Cloud removed `/rest/api/2/search` (it answers
410 Gone), so on Cloud JQL goes to `/rest/api/3/search/jql`, paged by `nextPageToken`, and
its descriptions come back as Atlassian Document Format, which we flatten to text.
"""

from __future__ import annotations

import base64
import json
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Protocol

from patchmig.config.schema import JiraConfig, resolve_secret


@dataclass
class Ticket:
    key: str
    summary: str
    description: str = ""
    status: str = ""
    issue_links: list[str] = field(default_factory=list)  # keys of linked issues


class JiraClient(Protocol):
    def search(self, jql: str) -> list[Ticket]: ...

    def get(self, key: str) -> Ticket | None: ...


class JiraWriter(Protocol):
    def add_labels(self, key: str, labels: list[str]) -> None: ...

    def comments(self, key: str) -> list[dict]: ...  # [{"id", "body"}]

    def add_comment(self, key: str, body: str) -> str: ...

    def update_comment(self, key: str, comment_id: str, body: str) -> None: ...

    def add_link(self, key: str, url: str, title: str, global_id: str) -> None: ...

    def create_issue(
        self, project: str, issue_type: str, summary: str, description: str, labels: list[str]
    ) -> str: ...


class JiraError(Exception):
    pass


def adf_text(node) -> str:
    """Plain text of an Atlassian Document Format node (Jira Cloud v3 rich text)."""
    if isinstance(node, str):
        return node
    if not isinstance(node, dict):
        return ""
    if node.get("type") == "text":
        return node.get("text", "")
    if node.get("type") == "hardBreak":
        return "\n"
    inner = "".join(adf_text(c) for c in node.get("content") or [])
    block = node.get("type") in ("paragraph", "heading", "codeBlock", "listItem", "blockquote")
    return inner + "\n" if block else inner


def _ticket(issue: dict) -> Ticket:
    f = issue.get("fields", {})
    links = []
    for link in f.get("issuelinks", []) or []:
        other = link.get("outwardIssue") or link.get("inwardIssue")
        if other:
            links.append(other["key"])
    description = f.get("description") or ""
    if not isinstance(description, str):  # Jira Cloud v3 rich text
        description = adf_text(description).strip()
    return Ticket(
        key=issue["key"],
        summary=f.get("summary") or "",
        description=description,
        status=(f.get("status") or {}).get("name", ""),
        issue_links=links,
    )


def _hint(code: int) -> str:
    return {
        401: " (check the token; on Cloud set jira.user to your account email)",
        403: " (the account lacks permission for this)",
        410: " (endpoint removed: set jira.deployment: cloud)",
    }.get(code, "")


def is_cloud(config: JiraConfig) -> bool:
    if config.deployment != "auto":
        return config.deployment == "cloud"
    host = urllib.parse.urlparse(config.url).hostname or ""
    return host.endswith(".atlassian.net")


class HttpJiraClient:
    """Jira REST v2 on Server/Data Center and Cloud (v3 only for JQL search on Cloud).

    Bearer token, or basic auth when a user is set (Cloud: account email + API token).
    """

    FIELDS = "summary,description,status,issuelinks"

    def __init__(self, config: JiraConfig, timeout: float = 30):
        self.base = config.url.rstrip("/")
        self.cloud = is_cloud(config)
        token = resolve_secret(config.token)
        if config.user:
            creds = base64.b64encode(f"{config.user}:{token}".encode()).decode()
            self.auth = f"Basic {creds}"
        else:
            self.auth = f"Bearer {token}"
        self.timeout = timeout

    def _get(self, path: str, params: dict, version: int = 2) -> dict | None:
        url = f"{self.base}/rest/api/{version}/{path}?{urllib.parse.urlencode(params)}"
        req = urllib.request.Request(
            url, headers={"Authorization": self.auth, "Accept": "application/json"}
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            raise JiraError(f"Jira {path} failed: HTTP {e.code}{_hint(e.code)}") from e
        except urllib.error.URLError as e:
            raise JiraError(f"Jira not reachable at {self.base}: {e.reason}") from e

    def search(self, jql: str) -> list[Ticket]:
        if self.cloud:
            return self._search_cloud(jql)
        tickets: list[Ticket] = []
        start = 0
        while True:
            data = self._get(
                "search", {"jql": jql, "fields": self.FIELDS, "startAt": start, "maxResults": 100}
            )
            issues = (data or {}).get("issues", [])
            tickets += [_ticket(i) for i in issues]
            start += len(issues)
            if not issues or start >= (data or {}).get("total", 0):
                return tickets

    def _search_cloud(self, jql: str) -> list[Ticket]:
        tickets: list[Ticket] = []
        params = {"jql": jql, "fields": self.FIELDS, "maxResults": 100}
        while True:
            data = self._get("search/jql", params, version=3) or {}
            tickets += [_ticket(i) for i in data.get("issues", [])]
            token = data.get("nextPageToken")
            if data.get("isLast", True) or not token:
                return tickets
            params = {**params, "nextPageToken": token}

    def check(self, project: str) -> list[str]:
        """What a connection test can tell: who we are, and that the project is visible."""
        me = self._get("myself", {})
        if me is None:
            raise JiraError("Jira /myself not found: is the URL the Jira base URL?")
        who = me.get("emailAddress") or me.get("name") or me.get("displayName") or "?"
        found = [f"signed in as {who} ({'Cloud' if self.cloud else 'Server/Data Center'})"]
        proj = self._get(f"project/{urllib.parse.quote(project)}", {})
        if proj is None:
            raise JiraError(f"project {project} not found or not visible to this account")
        found.append(f"project {project}: {proj.get('name', '?')}")
        jql = {
            "jql": f"project = {project} ORDER BY key DESC",
            "fields": "summary",
            "maxResults": 1,
        }
        page = self._get("search/jql", jql, version=3) if self.cloud else self._get("search", jql)
        latest = ((page or {}).get("issues") or [{}])[0].get("key", "none")
        found.append(f"JQL search works (latest issue: {latest})")
        return found

    def get(self, key: str) -> Ticket | None:
        data = self._get(f"issue/{urllib.parse.quote(key)}", {"fields": self.FIELDS})
        return _ticket(data) if data else None

    # ---- writes (export) ----------------------------------------------------------------

    def _send(self, method: str, path: str, body: dict) -> dict | None:
        req = urllib.request.Request(
            f"{self.base}/rest/api/2/{path}",
            method=method,
            data=json.dumps(body).encode(),
            headers={
                "Authorization": self.auth,
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read()
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as e:
            raise JiraError(f"Jira {method} {path} failed: HTTP {e.code}{_hint(e.code)}") from e
        except urllib.error.URLError as e:
            raise JiraError(f"Jira not reachable at {self.base}: {e.reason}") from e

    def _issue(self, key: str) -> str:
        return f"issue/{urllib.parse.quote(key)}"

    def add_labels(self, key: str, labels: list[str]) -> None:
        self._send("PUT", self._issue(key), {"update": {"labels": [{"add": x} for x in labels]}})

    def comments(self, key: str) -> list[dict]:
        data = self._get(f"{self._issue(key)}/comment", {"maxResults": 1000}) or {}
        return [{"id": str(c["id"]), "body": c.get("body") or ""} for c in data.get("comments", [])]

    def add_comment(self, key: str, body: str) -> str:
        return str(self._send("POST", f"{self._issue(key)}/comment", {"body": body})["id"])

    def update_comment(self, key: str, comment_id: str, body: str) -> None:
        self._send("PUT", f"{self._issue(key)}/comment/{comment_id}", {"body": body})

    def add_link(self, key: str, url: str, title: str, global_id: str) -> None:
        # Jira updates the existing remote link with the same globalId: idempotent.
        self._send("POST", f"{self._issue(key)}/remotelink",
                   {"globalId": global_id, "object": {"url": url, "title": title}})  # fmt: skip

    def create_issue(self, project, issue_type, summary, description, labels) -> str:
        data = self._send("POST", "issue", {"fields": {
            "project": {"key": project}, "issuetype": {"name": issue_type},
            "summary": summary, "description": description, "labels": labels,
        }})  # fmt: skip
        return data["key"]
