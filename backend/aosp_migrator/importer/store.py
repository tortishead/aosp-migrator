"""Fetch one ticket with everything it needs, and write it into the database.

Fetching (network) and storing (one DB transaction) are separate, so a ticket
that fails half way through the download leaves no partial rows behind.
"""

from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from aosp_migrator.db.models import Attachment, Comment, JiraIssue, utcnow
from aosp_migrator.importer.attachments import ProcessedAttachment, process_attachments
from aosp_migrator.importer.html import embedded_image_ids, rewrite_attachment_urls
from aosp_migrator.jira import base as jira
from aosp_migrator.jira.base import AttachmentMeta, Issue, JiraClient

# Jira fields whose changes go into the item history, by column name.
TRACKED_FIELDS = ("summary", "description_raw", "status", "labels", "components", "fix_versions", "issue_links")


@dataclass(frozen=True)
class FetchedIssue:
    issue: Issue
    description_html: str  # attachment URLs already rewritten
    comments: list[jira.Comment]  # bodies already rewritten
    attachments: list[ProcessedAttachment]


def fetch_issue(
    client: JiraClient,
    issue: Issue,
    attachments_dir: Path,
    local_url: Callable[[AttachmentMeta, Path], str],
) -> FetchedIssue:
    """Comments and attachments for `issue`, with Jira attachment URLs in the HTML pointing at local copies."""
    comments = client.get_comments(issue.key)
    images = embedded_image_ids([issue.description_html, *(c.body_html for c in comments)], client.base_url)
    processed = process_attachments(client, issue.attachments, images, attachments_dir)
    local = {p.meta.jira_id: local_url(p.meta, p.local_path) for p in processed if p.local_path is not None}

    def rewrite(body: str) -> str:
        return rewrite_attachment_urls(body, client.base_url, local)

    return FetchedIssue(
        issue=issue,
        description_html=rewrite(issue.description_html),
        comments=[jira.Comment(c.jira_id, c.author, c.created, rewrite(c.body_html)) for c in comments],
        attachments=processed,
    )


def issue_fields(issue: Issue) -> dict[str, Any]:
    """Column values taken straight from the Jira issue."""
    return {
        "jira_id": issue.jira_id,
        "summary": issue.summary,
        "description_raw": issue.description_raw,
        "status": issue.status,
        "labels": issue.labels,
        "components": issue.components,
        "fix_versions": issue.fix_versions,
        "issue_links": [asdict(link) for link in issue.issue_links],
        "jira_updated": issue.updated,
    }


def find_issue(session: Session, workspace_id: int, key: str) -> JiraIssue | None:
    return session.scalar(select(JiraIssue).where(JiraIssue.workspace_id == workspace_id, JiraIssue.key == key))


def store_issue(session: Session, workspace_id: int, fetched: FetchedIssue) -> tuple[JiraIssue, dict[str, Any]]:
    """Upsert the issue with its comments and attachments.

    Returns the row and what changed compared to the previous import (empty
    for a new row or when nothing changed).
    """
    issue = fetched.issue
    row = find_issue(session, workspace_id, issue.key)
    is_new = row is None
    if row is None:
        row = JiraIssue(workspace_id=workspace_id, key=issue.key)
        session.add(row)

    changes: dict[str, Any] = {}
    fields = issue_fields(issue)
    if not is_new:
        field_changes = {
            name: {"old": getattr(row, name), "new": fields[name]} for name in TRACKED_FIELDS if getattr(row, name) != fields[name]
        }
        if field_changes:
            changes["fields"] = field_changes
    for name, value in fields.items():
        setattr(row, name, value)
    row.description_html = fetched.description_html
    row.fetched_at = utcnow()
    session.flush()

    changes |= _store_comments(session, row, fetched.comments)
    changes |= _store_attachments(session, row, fetched.attachments)
    return row, {} if is_new else changes


def _store_comments(session: Session, row: JiraIssue, comments: list[jira.Comment]) -> dict[str, Any]:
    existing = {c.jira_id: c for c in session.scalars(select(Comment).where(Comment.issue_id == row.id))}
    added = edited = 0
    for comment in comments:
        current = existing.pop(comment.jira_id, None)
        if current is None:
            session.add(Comment(issue_id=row.id, jira_id=comment.jira_id, author=comment.author, created=comment.created, body_html=comment.body_html))
            added += 1
        elif current.body_html != comment.body_html:
            current.body_html = comment.body_html
            edited += 1
    for gone in existing.values():
        session.delete(gone)
    counts = {"comments_added": added, "comments_edited": edited, "comments_removed": len(existing)}
    return {name: count for name, count in counts.items() if count}


def _store_attachments(session: Session, row: JiraIssue, attachments: list[ProcessedAttachment]) -> dict[str, Any]:
    existing = {a.jira_id: a for a in session.scalars(select(Attachment).where(Attachment.issue_id == row.id))}
    newest: dict[str, AttachmentMeta] = {}
    for processed in attachments:
        meta = processed.meta
        best = newest.get(meta.filename)
        if best is None or (meta.created, int(meta.jira_id)) > (best.created, int(best.jira_id)):
            newest[meta.filename] = meta

    added: list[str] = []
    for processed in attachments:
        meta = processed.meta
        attachment = existing.pop(meta.jira_id, None)
        if attachment is None:
            attachment = Attachment(issue_id=row.id, jira_id=meta.jira_id)
            session.add(attachment)
            added.append(meta.filename)
        attachment.filename = meta.filename
        attachment.size = meta.size
        attachment.mime = meta.mime
        attachment.kind = processed.kind
        attachment.url = meta.url
        attachment.local_path = str(processed.local_path) if processed.local_path else None
        attachment.sha256 = processed.sha256
        attachment.is_current = newest[meta.filename] is meta
        attachment.created = meta.created
        attachment.parsed = processed.parsed
    removed = [a.filename for a in existing.values()]
    for gone in existing.values():
        session.delete(gone)
    changes: dict[str, Any] = {}
    if added:
        changes["attachments_added"] = added
    if removed:
        changes["attachments_removed"] = removed
    return changes
