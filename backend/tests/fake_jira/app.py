"""Fake Jira Data Center REST API v2, serving a `FakeJiraStore`.

Covers only what the importer uses: myself, search (GET and POST), issue get,
comments, attachment download. Responses follow the Jira DC JSON shapes.
"""

from datetime import datetime
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel

from fake_jira.store import LINK_TYPES, FakeIssue, FakeJiraStore

ALL_FIELDS = ("summary", "description", "status", "labels", "components", "fixVersions", "issuelinks", "attachment", "updated", "comment")


class SearchBody(BaseModel):
    jql: str = ""
    startAt: int = 0
    maxResults: int = 50
    fields: list[str] | None = None
    expand: list[str] | str | None = None


def create_fake_jira(store: FakeJiraStore) -> FastAPI:
    app = FastAPI(title="Fake Jira DC")
    # Read through app.state on every request so a running server can be given a fresh store.
    app.state.store = store

    def current() -> FakeJiraStore:
        return app.state.store

    @app.middleware("http")
    async def auth_and_faults(request: Request, call_next):  # noqa: ANN001, ANN202
        store = current()
        store.requests.append(f"{request.method} {request.url.path}" + (f"?{request.url.query}" if request.url.query else ""))
        status = store.fail_paths.get(request.url.path)
        if status is None and store.fail_next:
            status = store.fail_next.pop(0)
        if status is not None:
            headers = {"Retry-After": store.retry_after} if status == 429 and store.retry_after is not None else {}
            return JSONResponse({"errorMessages": [f"Injected failure {status}"]}, status_code=status, headers=headers)
        if request.headers.get("authorization") != f"Bearer {store.pat}":
            return JSONResponse({"errorMessages": ["You are not authenticated."], "errors": {}}, status_code=401)
        response = await call_next(request)
        response.headers.update(store.response_headers)
        return response

    @app.get("/rest/api/2/myself")
    def myself() -> dict:
        return {"name": "jdoe", "displayName": current().user, "active": True}

    @app.get("/rest/api/2/search")
    def search_get(
        request: Request,
        jql: str = "",
        startAt: int = 0,
        maxResults: int = 50,
        fields: str | None = None,
        expand: str | None = None,
    ) -> dict:
        return _search(request, startAt, maxResults, _split(fields), _split(expand))

    @app.post("/rest/api/2/search")
    def search_post(request: Request, body: SearchBody) -> dict:
        expand = _split(body.expand) if isinstance(body.expand, str) else body.expand
        return _search(request, body.startAt, body.maxResults, body.fields, expand)

    def _search(request: Request, start_at: int, max_results: int, fields: list[str] | None, expand: list[str] | None) -> dict:
        matching = [issue for issue in current().issues.values() if issue.in_jql]
        page_size = max(0, min(max_results, current().max_page_size))
        page = matching[start_at : start_at + page_size]
        return {
            "expand": "schema,names",
            "startAt": start_at,
            "maxResults": page_size,
            "total": len(matching),
            "issues": [_issue_json(request, issue, fields, expand) for issue in page],
        }

    @app.get("/rest/api/2/issue/{key}", response_model=None)
    def get_issue(request: Request, key: str, fields: str | None = None, expand: str | None = None) -> dict | JSONResponse:
        issue = current().issues.get(key)
        if issue is None:
            return _not_found()
        return _issue_json(request, issue, _split(fields), _split(expand))

    @app.get("/rest/api/2/issue/{key}/comment", response_model=None)
    def get_comments(key: str, startAt: int = 0, maxResults: int = 50, expand: str | None = None) -> dict | JSONResponse:
        issue = current().issues.get(key)
        if issue is None:
            return _not_found()
        page_size = max(0, min(maxResults, current().max_page_size))
        rendered = "renderedBody" in (_split(expand) or [])
        return {
            "startAt": startAt,
            "maxResults": page_size,
            "total": len(issue.comments),
            "comments": [_comment_json(c, rendered) for c in issue.comments[startAt : startAt + page_size]],
        }

    @app.get("/secure/attachment/{attachment_id}/{filename}")
    def download(attachment_id: str, filename: str) -> Response:
        attachment = current().find_attachment(attachment_id)
        if attachment is None or attachment.filename != filename:
            return Response(status_code=404)
        return Response(attachment.content, media_type=attachment.mime)

    def _issue_json(request: Request, issue: FakeIssue, fields: list[str] | None, expand: list[str] | None) -> dict:
        base = str(request.base_url).rstrip("/")
        all_fields: dict[str, Any] = {
            "summary": issue.summary,
            "description": issue.description or None,
            "status": {"name": issue.status, "statusCategory": {"key": "new" if issue.status == "Open" else "indeterminate"}},
            "labels": issue.labels,
            "components": [{"name": name} for name in issue.components],
            "fixVersions": [{"name": name} for name in issue.fix_versions],
            "issuelinks": _links_json(issue.key),
            "attachment": [
                {
                    "id": a.id,
                    "self": f"{base}/rest/api/2/attachment/{a.id}",
                    "filename": a.filename,
                    "author": {"name": a.author, "displayName": a.author},
                    "created": jira_datetime(a.created),
                    "size": len(a.content),
                    "mimeType": a.mime,
                    "content": f"{base}/secure/attachment/{a.id}/{a.filename}",
                }
                for a in issue.attachments
            ],
            "updated": jira_datetime(issue.updated),
            "comment": {
                "startAt": 0,
                "maxResults": len(issue.comments),
                "total": len(issue.comments),
                "comments": [_comment_json(c, rendered=False) for c in issue.comments],
            },
        }
        wanted = fields if fields and not {"*all", "*navigable"} & set(fields) else ALL_FIELDS
        data: dict[str, Any] = {
            "id": issue.id,
            "key": issue.key,
            "self": f"{base}/rest/api/2/issue/{issue.id}",
            "fields": {name: all_fields[name] for name in wanted if name in all_fields},
        }
        if expand and "renderedFields" in expand:
            data["renderedFields"] = {
                "description": issue.description_html or None,
                "comment": {"comments": [_comment_json(c, rendered=True, body_key="body") for c in issue.comments]},
            }
        return data

    def _links_json(key: str) -> list[dict]:
        result = []
        for link in current().links:
            outward, inward = LINK_TYPES[link.link_type]
            link_type = {"name": link.link_type, "outward": outward, "inward": inward}
            if link.outward_key == key:
                result.append({"id": link.id, "type": link_type, "outwardIssue": _link_target(link.inward_key)})
            elif link.inward_key == key:
                result.append({"id": link.id, "type": link_type, "inwardIssue": _link_target(link.outward_key)})
        return result

    def _link_target(key: str) -> dict:
        issue = current().issues.get(key)
        fields = {"summary": issue.summary, "status": {"name": issue.status}} if issue else {}
        return {"id": issue.id if issue else "0", "key": key, "fields": fields}

    return app


def jira_datetime(value: datetime) -> str:
    return value.strftime("%Y-%m-%dT%H:%M:%S.000%z")


def _comment_json(comment: Any, rendered: bool, body_key: str = "renderedBody") -> dict:
    data = {
        "id": comment.id,
        "author": {"name": comment.author.lower().replace(" ", "."), "displayName": comment.author},
        "body": comment.body,
        "created": jira_datetime(comment.created),
        "updated": jira_datetime(comment.created),
    }
    if rendered:
        data[body_key] = comment.body_html
    return data


def _split(value: str | None) -> list[str] | None:
    return [part.strip() for part in value.split(",") if part.strip()] if value else None


def _not_found() -> JSONResponse:
    return JSONResponse({"errorMessages": ["Issue Does Not Exist"], "errors": {}}, status_code=404)
