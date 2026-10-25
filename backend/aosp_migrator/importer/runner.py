"""Import jobs: run in a background thread, one at a time per workspace.

A job imports either every ticket the workspace JQL matches (`all`) or a list
of existing backlog items (`keys`). Each ticket is fetched, then written in
its own transaction. A ticket that fails records an error and the job goes on;
only errors that would hit every ticket (auth, Jira unreachable, search
failing) fail the whole job.
"""

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from aosp_migrator.db.models import HistoryEvent, ImportJob, Item, ItemLink, JiraIssue, Workspace, utcnow
from aosp_migrator.importer.store import FetchedIssue, fetch_issue, find_issue, issue_fields, store_issue
from aosp_migrator.jira.base import AttachmentMeta, Issue, IssueLink, JiraAuthError, JiraClient, JiraConnectionError, JiraError

log = logging.getLogger(__name__)

# Errors that would fail every ticket the same way: stop instead of grinding through them.
FATAL_ERRORS = (JiraAuthError, JiraConnectionError)


def link_is_relevant(link: IssueLink, link_types: list[str]) -> bool:
    """Whether the workspace follows this link: its type name or direction is listed, or nothing is."""
    if not link_types:
        return True
    wanted = {t.casefold() for t in link_types}
    return link.link_type.casefold() in wanted or link.description.casefold() in wanted


class JobConflictError(Exception):
    """An import is already running for the workspace."""


class _Canceled(Exception):
    pass


def local_attachment_url(workspace_id: int, meta: AttachmentMeta, path: Path) -> str:
    """URL the API serves a downloaded attachment under."""
    return f"/api/workspaces/{workspace_id}/attachments/{meta.jira_id}/{quote(path.name)}"


def running_job(session: Session, workspace_id: int) -> ImportJob | None:
    return session.scalar(select(ImportJob).where(ImportJob.workspace_id == workspace_id, ImportJob.status == "running"))


def fail_interrupted_jobs(session: Session) -> None:
    """Jobs still `running` at startup died with the previous process."""
    for job in session.scalars(select(ImportJob).where(ImportJob.status == "running")):
        job.status = "failed"
        job.error = "Interrupted by a server restart"
        job.finished_at = utcnow()
    session.commit()


def _start_thread(target: Callable[[], None]) -> None:
    threading.Thread(target=target, name="import-job", daemon=True).start()


class ImportRunner:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        attachments_dir: Callable[[int], Path],
        client_factory: Callable[[], JiraClient],
        executor: Callable[[Callable[[], None]], None] = _start_thread,
    ) -> None:
        self._sessions = sessions
        self._attachments_dir = attachments_dir
        self._client_factory = client_factory
        self._executor = executor
        self._lock = threading.Lock()

    def start(self, workspace_id: int, keys: list[str] | None = None) -> ImportJob:
        """Create the job and run it in the background. `keys` None imports the whole JQL."""
        with self._lock, self._sessions() as session:
            if running_job(session, workspace_id) is not None:
                raise JobConflictError("An import is already running for this workspace")
            job = ImportJob(
                workspace_id=workspace_id,
                scope="all" if keys is None else "keys",
                issue_keys=keys or [],
                total=None if keys is None else len(keys),
                errors=[],
            )
            session.add(job)
            session.commit()
        self._executor(lambda: self.run(job.id))
        return job

    def run(self, job_id: int) -> None:
        with self._sessions() as session:
            job = session.get(ImportJob, job_id)
            assert job is not None
            try:
                client = self._client_factory()
            except JiraError as exc:
                self._finish(session, job, "failed", str(exc))
                return
            try:
                _Job(session, job, client, self._attachments_dir(job.workspace_id)).run()
                self._finish(session, job, "done")
            except _Canceled:
                self._finish(session, job, "canceled")
            except JiraError as exc:
                self._finish(session, job, "failed", str(exc))
            except Exception as exc:
                log.exception("Import job %s failed", job_id)
                self._finish(session, job, "failed", f"Unexpected error: {exc}")
            finally:
                client.close()

    @staticmethod
    def _finish(session: Session, job: ImportJob, status: str, error: str | None = None) -> None:
        session.rollback()
        job.status = status
        job.error = error
        job.finished_at = utcnow()
        session.commit()


@dataclass
class _Job:
    session: Session
    job: ImportJob
    client: JiraClient
    attachments_dir: Path
    # Keys stored during this job, so a ticket linked from several items is fetched once.
    stored: set[str] = field(default_factory=set)
    link_types: list[str] = field(default_factory=list)

    @property
    def workspace_id(self) -> int:
        return self.job.workspace_id

    def run(self) -> None:
        workspace = self.session.get(Workspace, self.workspace_id)
        assert workspace is not None
        self.link_types = list(workspace.link_types)
        if self.job.scope == "keys":
            for key in self.job.issue_keys:
                self._check_cancel()
                self._import(key, None, in_jql=None)
            return

        seen: set[str] = set()
        for page in self.client.search(workspace.jql):
            self.job.total = page.total
            self.session.commit()
            for issue in page.issues:
                self._check_cancel()
                seen.add(issue.key)
                self._import(issue.key, issue, in_jql=True)
        self._mark_dropped(seen)

    def _check_cancel(self) -> None:
        self.session.refresh(self.job, ["cancel_requested"])
        if self.job.cancel_requested:
            raise _Canceled

    def _import(self, key: str, issue: Issue | None, in_jql: bool | None) -> None:
        """Import one backlog ticket. `in_jql` True restores a dropped item; None leaves the flag alone."""
        try:
            fetched = self._fetch(issue or self.client.get_issue(key))
            linked = self._fetch_linked(fetched.issue)
        except FATAL_ERRORS:
            raise
        except Exception as exc:
            if not isinstance(exc, JiraError):
                log.exception("Import of %s failed", key)
            self._record_failure(key, issue, in_jql, str(exc))
            return
        try:
            self._store(fetched, linked, in_jql)
        except Exception as exc:
            log.exception("Storing %s failed", key)
            self._record_failure(key, issue, in_jql, f"Storing failed: {exc}")
            return
        self.job.done += 1
        self.session.commit()

    def _fetch(self, issue: Issue) -> FetchedIssue:
        return fetch_issue(
            self.client,
            issue,
            self.attachments_dir,
            lambda meta, path: local_attachment_url(self.workspace_id, meta, path),
        )

    def _fetch_linked(self, issue: Issue) -> dict[str, FetchedIssue]:
        """Linked tickets not yet stored by this job. One that fails is reported but keeps its last import."""
        linked: dict[str, FetchedIssue] = {}
        for link in self._relevant_links(issue):
            if link.key in self.stored or link.key in linked or link.key == issue.key:
                continue
            try:
                linked[link.key] = self._fetch(self.client.get_issue(link.key))
            except FATAL_ERRORS:
                raise
            except JiraError as exc:
                self._add_error(link.key, f"Linked from {issue.key}: {exc}")
        return linked

    def _relevant_links(self, issue: Issue) -> list[IssueLink]:
        return [link for link in issue.issue_links if link_is_relevant(link, self.link_types)]

    def _store(self, fetched: FetchedIssue, linked: dict[str, FetchedIssue], in_jql: bool | None) -> None:
        session = self.session
        row, changes = store_issue(session, self.workspace_id, fetched)
        for linked_issue in linked.values():
            linked_row, linked_changes = store_issue(session, self.workspace_id, linked_issue)
            # A linked ticket that is also a backlog item: its own import later in this job sees no changes.
            linked_item = session.scalar(select(Item).where(Item.issue_id == linked_row.id))
            if linked_item is not None and linked_changes:
                self._history(linked_item, "updated", linked_changes)

        item = session.scalar(select(Item).where(Item.issue_id == row.id))
        if item is None:
            item = self._new_item(row)
            self._history(item, "imported")
        elif changes:
            self._history(item, "updated", changes)
        item.last_import_error = None
        if in_jql:
            self._restore(item)

        for link in session.scalars(select(ItemLink).where(ItemLink.item_id == item.id)):
            session.delete(link)
        for link in self._relevant_links(fetched.issue):
            target = find_issue(session, self.workspace_id, link.key)
            if target is not None and target.id != row.id:
                session.add(ItemLink(item_id=item.id, issue_id=target.id, link_type=link.link_type, direction=link.direction, description=link.description))
        session.flush()
        self.stored.update([fetched.issue.key, *linked])

    def _record_failure(self, key: str, issue: Issue | None, in_jql: bool | None, message: str) -> None:
        """Keep what is already stored; a new ticket gets a bare backlog item so the error is visible."""
        session = self.session
        session.rollback()
        row = find_issue(session, self.workspace_id, key)
        if row is None and issue is not None:
            row = JiraIssue(workspace_id=self.workspace_id, key=key, description_html=issue.description_html, **issue_fields(issue))
            session.add(row)
            session.flush()
        if row is not None:
            item = session.scalar(select(Item).where(Item.issue_id == row.id)) or self._new_item(row)
            item.last_import_error = message
            self._history(item, "import_failed", {"error": message})
            if in_jql:
                self._restore(item)
        self._add_error(key, message)
        self.job.done += 1
        session.commit()

    def _mark_dropped(self, seen: set[str]) -> None:
        rows = self.session.execute(
            select(Item, JiraIssue.key).join(JiraIssue, Item.issue_id == JiraIssue.id).where(Item.workspace_id == self.workspace_id, Item.dropped.is_(False))
        )
        for item, key in rows:
            if key not in seen:
                item.dropped = True
                self._history(item, "dropped")
        self.session.commit()

    def _restore(self, item: Item) -> None:
        if item.dropped:
            item.dropped = False
            self._history(item, "restored")

    def _new_item(self, row: JiraIssue) -> Item:
        item = Item(workspace_id=self.workspace_id, issue_id=row.id)
        self.session.add(item)
        self.session.flush()
        return item

    def _history(self, item: Item, event_type: str, payload: dict[str, Any] | None = None) -> None:
        self.session.add(HistoryEvent(item_id=item.id, type=event_type, payload={"job_id": self.job.id, **(payload or {})}))

    def _add_error(self, key: str, message: str) -> None:
        # Reassign: in-place changes to a JSON column are not tracked.
        self.job.errors = [*self.job.errors, {"key": key, "message": message}]
