"""Jira client interface and the normalized data it returns.

The importer only sees these types, so a Jira Cloud adapter can be added
next to the Data Center one without touching the importer.
"""

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal, Protocol


class JiraError(Exception):
    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class JiraAuthError(JiraError):
    """401 / 403: PAT missing, wrong, expired or lacking permission."""


class JiraNotFoundError(JiraError):
    pass


class JiraConnectionError(JiraError):
    """Jira could not be reached (DNS, refused, timeout), after retries."""


@dataclass(frozen=True)
class IssueLink:
    link_type: str  # Link type name, e.g. "Blocks"
    direction: Literal["inward", "outward"]
    description: str  # How this issue relates to `key`, e.g. "is blocked by"
    key: str


@dataclass(frozen=True)
class AttachmentMeta:
    jira_id: str
    filename: str
    size: int
    mime: str
    created: datetime
    url: str


@dataclass(frozen=True)
class Comment:
    jira_id: str
    author: str
    created: datetime
    body_html: str


@dataclass(frozen=True)
class Issue:
    jira_id: str
    key: str
    summary: str
    description_raw: str
    description_html: str
    status: str
    labels: list[str]
    components: list[str]
    fix_versions: list[str]
    issue_links: list[IssueLink]
    attachments: list[AttachmentMeta]
    updated: datetime


@dataclass(frozen=True)
class SearchPage:
    start_at: int
    total: int
    issues: list[Issue]


class JiraClient(Protocol):
    def myself(self) -> str:
        """Display name of the authenticated user. Used as a connection check."""
        ...

    def search(self, jql: str, *, page_size: int = 50) -> Iterator[SearchPage]:
        """All issues matching `jql`, one page at a time."""
        ...

    def get_issue(self, key: str) -> Issue: ...

    def get_comments(self, key: str) -> list[Comment]: ...

    def download_attachment(self, url: str, dest: Path) -> str:
        """Write the attachment to `dest` atomically and return its sha256 hex digest."""
        ...

    def close(self) -> None: ...
