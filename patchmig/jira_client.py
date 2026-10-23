"""Minimal Jira REST client: only what the tool needs (read now; write comes in M8)."""

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


class JiraError(Exception):
    pass


def _ticket(issue: dict) -> Ticket:
    f = issue.get("fields", {})
    links = []
    for link in f.get("issuelinks", []) or []:
        other = link.get("outwardIssue") or link.get("inwardIssue")
        if other:
            links.append(other["key"])
    description = f.get("description") or ""
    if not isinstance(description, str):  # Jira Cloud v3 rich text: keep it readable enough
        description = json.dumps(description)
    return Ticket(
        key=issue["key"],
        summary=f.get("summary") or "",
        description=description,
        status=(f.get("status") or {}).get("name", ""),
        issue_links=links,
    )


class HttpJiraClient:
    """Jira Server/Data Center (REST v2). Bearer token, or basic auth when a user is set."""

    FIELDS = "summary,description,status,issuelinks"

    def __init__(self, config: JiraConfig, timeout: float = 30):
        self.base = config.url.rstrip("/")
        token = resolve_secret(config.token)
        if config.user:
            creds = base64.b64encode(f"{config.user}:{token}".encode()).decode()
            self.auth = f"Basic {creds}"
        else:
            self.auth = f"Bearer {token}"
        self.timeout = timeout

    def _get(self, path: str, params: dict) -> dict | None:
        url = f"{self.base}/rest/api/2/{path}?{urllib.parse.urlencode(params)}"
        req = urllib.request.Request(
            url, headers={"Authorization": self.auth, "Accept": "application/json"}
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            raise JiraError(f"Jira {path} failed: HTTP {e.code}") from e
        except urllib.error.URLError as e:
            raise JiraError(f"Jira not reachable at {self.base}: {e.reason}") from e

    def search(self, jql: str) -> list[Ticket]:
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

    def get(self, key: str) -> Ticket | None:
        data = self._get(f"issue/{urllib.parse.quote(key)}", {"fields": self.FIELDS})
        return _ticket(data) if data else None
