"""In-memory state of the fake Jira. Tests mutate it to simulate changes in Jira."""

from dataclasses import dataclass, field
from datetime import UTC, datetime

DEFAULT_PAT = "fake-jira-pat"

# name -> (outward description, inward description), as in a stock Jira DC.
LINK_TYPES = {
    "Blocks": ("blocks", "is blocked by"),
    "Cloners": ("clones", "is cloned by"),
    "Duplicate": ("duplicates", "is duplicated by"),
    "Relates": ("relates to", "relates to"),
}


@dataclass
class FakeAttachment:
    id: str
    filename: str
    content: bytes
    mime: str
    created: datetime
    author: str = "jdoe"


@dataclass
class FakeComment:
    id: str
    author: str
    body: str
    body_html: str
    created: datetime


@dataclass
class FakeIssue:
    key: str
    summary: str
    description: str = ""
    description_html: str = ""
    status: str = "Open"
    labels: list[str] = field(default_factory=list)
    components: list[str] = field(default_factory=list)
    fix_versions: list[str] = field(default_factory=list)
    attachments: list[FakeAttachment] = field(default_factory=list)
    comments: list[FakeComment] = field(default_factory=list)
    updated: datetime = field(default_factory=lambda: datetime(2026, 10, 1, 12, 0, tzinfo=UTC))
    # The fake does not parse JQL: every search returns the issues with in_jql=True.
    in_jql: bool = True

    @property
    def id(self) -> str:
        return str(10000 + int(self.key.rsplit("-", 1)[1]))


@dataclass
class FakeLink:
    """`outward_key` <link_type outward description> `inward_key`, e.g. A blocks B."""

    id: str
    link_type: str
    outward_key: str
    inward_key: str


@dataclass
class FakeJiraStore:
    pat: str = DEFAULT_PAT
    user: str = "Jane Doe"
    issues: dict[str, FakeIssue] = field(default_factory=dict)
    links: list[FakeLink] = field(default_factory=list)
    # Jira caps maxResults server side; the client must page by what it gets back.
    max_page_size: int = 50
    # Statuses to answer the next requests with, one per request, before normal handling.
    fail_next: list[int] = field(default_factory=list)
    # "METHOD path?query" of every request, for assertions.
    requests: list[str] = field(default_factory=list)
    _next_id: int = 20000

    def next_id(self) -> str:
        self._next_id += 1
        return str(self._next_id)

    def add(self, issue: FakeIssue) -> FakeIssue:
        self.issues[issue.key] = issue
        return issue

    def link(self, outward_key: str, link_type: str, inward_key: str) -> None:
        self.links.append(FakeLink(self.next_id(), link_type, outward_key, inward_key))

    def attach(self, key: str, filename: str, content: bytes, mime: str, created: datetime) -> FakeAttachment:
        attachment = FakeAttachment(self.next_id(), filename, content, mime, created)
        self.issues[key].attachments.append(attachment)
        return attachment

    def comment(self, key: str, author: str, body: str, body_html: str, created: datetime) -> FakeComment:
        comment = FakeComment(self.next_id(), author, body, body_html, created)
        self.issues[key].comments.append(comment)
        return comment

    def find_attachment(self, attachment_id: str) -> FakeAttachment | None:
        for issue in self.issues.values():
            for attachment in issue.attachments:
                if attachment.id == attachment_id:
                    return attachment
        return None
