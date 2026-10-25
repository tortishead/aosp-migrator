"""Jira Data Center client: REST API v2, PAT sent as `Authorization: Bearer`. Read-only."""

import hashlib
import os
import time
from collections.abc import Callable, Iterator
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx2

from aosp_migrator.jira.base import (
    AttachmentMeta,
    Comment,
    Issue,
    IssueLink,
    JiraAuthError,
    JiraConnectionError,
    JiraError,
    JiraNotFoundError,
    SearchPage,
)

ISSUE_FIELDS = [
    "summary",
    "description",
    "status",
    "labels",
    "components",
    "fixVersions",
    "issuelinks",
    "attachment",
    "updated",
]

RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
MAX_RETRY_AFTER = 60.0


class DataCenterClient:
    def __init__(
        self,
        base_url: str,
        pat: str,
        *,
        transport: httpx2.BaseTransport | None = None,
        timeout: float = 30,
        max_attempts: int = 4,
        backoff: float = 1.0,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._origin = _origin(self.base_url)
        self._max_attempts = max_attempts
        self._backoff = backoff
        self._sleep = sleep
        self._http = httpx2.Client(
            transport=transport,
            timeout=timeout,
            headers={"Authorization": f"Bearer {pat}", "Accept": "application/json"},
        )

    def __enter__(self) -> "DataCenterClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        self._http.close()

    def myself(self) -> str:
        user = _json(self._request("GET", "/rest/api/2/myself"))
        return user.get("displayName") or user.get("name") or ""

    def search(self, jql: str, *, page_size: int = 50) -> Iterator[SearchPage]:
        start_at = 0
        while True:
            body = {
                "jql": jql,
                "startAt": start_at,
                "maxResults": page_size,
                "fields": ISSUE_FIELDS,
                "expand": ["renderedFields"],
            }
            data = _json(self._request("POST", "/rest/api/2/search", json=body))
            issues = [self._issue(raw) for raw in data.get("issues", [])]
            total = data.get("total", 0)
            yield SearchPage(start_at=start_at, total=total, issues=issues)
            # Jira may cap maxResults below what was asked, so advance by what came back.
            start_at += len(issues)
            if not issues or start_at >= total:
                return

    def get_issue(self, key: str) -> Issue:
        params = {"fields": ",".join(ISSUE_FIELDS), "expand": "renderedFields"}
        return self._issue(_json(self._request("GET", f"/rest/api/2/issue/{key}", params=params)))

    def get_comments(self, key: str) -> list[Comment]:
        comments: list[Comment] = []
        while True:
            params = {"startAt": len(comments), "maxResults": 100, "expand": "renderedBody"}
            data = _json(self._request("GET", f"/rest/api/2/issue/{key}/comment", params=params))
            page = data.get("comments", [])
            comments.extend(
                Comment(
                    jira_id=str(raw["id"]),
                    author=_user_name(raw.get("author")),
                    created=parse_datetime(raw["created"]),
                    body_html=raw.get("renderedBody") or "",
                )
                for raw in page
            )
            if not page or len(comments) >= data.get("total", 0):
                return comments

    def download_attachment(self, url: str, dest: Path) -> str:
        url = self.resolve_url(url)
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(dest.name + ".part")
        digest = hashlib.sha256()
        response = self._request("GET", url, stream=True, headers={"Accept": "*/*"})
        try:
            with tmp.open("wb") as f:
                for chunk in response.iter_bytes():
                    digest.update(chunk)
                    f.write(chunk)
        except httpx2.HTTPError as exc:
            tmp.unlink(missing_ok=True)
            raise JiraConnectionError(f"Download of {url} failed: {exc}") from exc
        finally:
            response.close()
        os.replace(tmp, dest)
        return digest.hexdigest()

    def resolve_url(self, url: str) -> str:
        """Absolute URL for `url` (which may be relative, as in rendered HTML).

        Refuses URLs on other hosts, so the PAT is never sent outside Jira.
        """
        absolute = urljoin(self.base_url + "/", url)
        if _origin(absolute) != self._origin:
            raise JiraError(f"Refusing to fetch {url}: not on {self.base_url}")
        return absolute

    def _request(self, method: str, path_or_url: str, *, stream: bool = False, **kwargs: Any) -> httpx2.Response:
        url = path_or_url if "://" in path_or_url else self.base_url + path_or_url
        attempt = 0
        while True:
            attempt += 1
            last = attempt >= self._max_attempts
            try:
                response = self._http.send(self._http.build_request(method, url, **kwargs), stream=stream)
            except httpx2.TransportError as exc:
                if last:
                    raise JiraConnectionError(f"Cannot reach Jira: {exc}") from exc
                self._sleep(self._delay(attempt, None))
                continue
            if response.status_code in RETRY_STATUSES and not last:
                retry_after = response.headers.get("Retry-After")
                response.close()
                self._sleep(self._delay(attempt, retry_after))
                continue
            if response.is_success:
                return response
            if stream:
                response.read()
            raise _error(response)

    def _delay(self, attempt: int, retry_after: str | None) -> float:
        if retry_after is not None:
            try:
                return min(max(float(retry_after), 0.0), MAX_RETRY_AFTER)
            except ValueError:
                pass  # HTTP-date form; fall back to backoff
        return self._backoff * 2 ** (attempt - 1)

    def _issue(self, raw: dict[str, Any]) -> Issue:
        fields = raw.get("fields") or {}
        rendered = raw.get("renderedFields") or {}
        return Issue(
            jira_id=str(raw["id"]),
            key=raw["key"],
            summary=fields.get("summary") or "",
            description_raw=fields.get("description") or "",
            description_html=rendered.get("description") or "",
            status=(fields.get("status") or {}).get("name", ""),
            labels=list(fields.get("labels") or []),
            components=[c["name"] for c in fields.get("components") or []],
            fix_versions=[v["name"] for v in fields.get("fixVersions") or []],
            issue_links=[_link(link) for link in fields.get("issuelinks") or []],
            attachments=[
                AttachmentMeta(
                    jira_id=str(a["id"]),
                    filename=a["filename"],
                    size=int(a.get("size") or 0),
                    mime=a.get("mimeType") or "application/octet-stream",
                    created=parse_datetime(a["created"]),
                    url=a["content"],
                )
                for a in fields.get("attachment") or []
            ],
            updated=parse_datetime(fields["updated"]),
        )


def parse_datetime(value: str) -> datetime:
    # Jira DC format: 2026-10-01T12:00:00.000+0000
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%f%z")
    except ValueError:
        return datetime.fromisoformat(value)


def _link(raw: dict[str, Any]) -> IssueLink:
    link_type = raw["type"]
    if "outwardIssue" in raw:
        return IssueLink(link_type["name"], "outward", link_type.get("outward", ""), raw["outwardIssue"]["key"])
    return IssueLink(link_type["name"], "inward", link_type.get("inward", ""), raw["inwardIssue"]["key"])


def _user_name(user: dict[str, Any] | None) -> str:
    if not user:
        return ""
    return user.get("displayName") or user.get("name") or ""


def _origin(url: str) -> tuple[str, str]:
    parsed = urlparse(url)
    return parsed.scheme.lower(), parsed.netloc.lower()


def _json(response: httpx2.Response) -> dict[str, Any]:
    try:
        data = response.json()
    except ValueError as exc:
        raise JiraError("Response is not JSON. Is this a Jira base URL?", response.status_code) from exc
    if not isinstance(data, dict):
        raise JiraError("Unexpected JSON response from Jira", response.status_code)
    return data


def _error(response: httpx2.Response) -> JiraError:
    status = response.status_code
    detail = ""
    try:
        data = response.json()
        messages = list(data.get("errorMessages") or []) + list((data.get("errors") or {}).values())
        detail = "; ".join(str(m) for m in messages)
    except (ValueError, AttributeError):
        pass
    if status in (401, 403):
        return JiraAuthError(f"Authentication failed (HTTP {status})", status)
    if status == 404:
        return JiraNotFoundError(detail or "Not found (HTTP 404)", status)
    return JiraError(f"Jira error (HTTP {status}){': ' + detail if detail else ''}", status)
