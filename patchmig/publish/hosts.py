"""Where PRs go: GitHub, GitLab (merge requests), Bitbucket Cloud and Bitbucket Server/DC.

Each host's REST API is wrapped to answer in one shape, GitHub's: a PR is a dict with
`number`, `html_url`, `state` ("open" or "closed"), `merged`, `merged_by` {login} and `head`;
a review has `id`, `state` (APPROVED, CHANGES_REQUESTED or other), `user` {login} and `body`.
The publisher and the review sync only see that shape.

Hosts without written reviews report reviewer states instead (GitLab approvals and "request
changes", Bitbucket approved / needs work); a request for changes carries that reviewer's
latest PR comments as its text. Their review id includes the PR's head commit, so a reviewer
who asks for changes again after a rework counts again.

Branches are pushed with the git CLI; the token reaches git in its environment
(`patchmig.githosts.git_env`), never in a command line, remote URL or log.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Protocol

from patchmig.config import ProjectConfig
from patchmig.config.schema import RepoHost, RepoOutput
from patchmig.githosts import api_base, api_headers, git_env, token_of
from patchmig.gitutil import git

PUBLIC = {"github": "https://github.com", "gitlab": "https://gitlab.com",
          "bitbucket_cloud": "https://bitbucket.org"}  # fmt: skip
PR_HOSTS = ("github", "gitlab", "bitbucket_cloud", "bitbucket_server")


class ReviewHostError(Exception):
    pass


GithubError = ReviewHostError


class ReviewApi(Protocol):
    def find_pr(self, repo: str, head: str) -> dict | None: ...

    def create_pr(
        self, repo: str, title: str, body: str, head: str, base: str, draft: bool
    ) -> dict: ...

    def update_pr(self, repo: str, number: int, title: str, body: str) -> dict: ...

    def get_pr(self, repo: str, number: int) -> dict: ...

    def reviews(self, repo: str, number: int) -> list[dict]: ...

    def request_reviewers(self, repo: str, number: int, reviewers: list[str]) -> None: ...

    def add_labels(self, repo: str, number: int, labels: list[str]) -> None: ...


GithubApi = ReviewApi


def output_of(config: ProjectConfig) -> tuple[str, RepoOutput]:
    out = config.output
    if out is None or out.target not in PR_HOSTS:
        raise ReviewHostError("publishing needs output.target github, gitlab, bitbucket_cloud "
                              "or bitbucket_server with its section")  # fmt: skip
    return out.target, getattr(out, out.target)


def host_of(config: ProjectConfig) -> RepoHost:
    """The output host with credentials; unset fields come from the target tree's host."""
    kind, out = output_of(config)
    tree = config.trees.target.host
    same = tree is not None and tree.kind == kind and (
        out.url is None or out.url.rstrip("/") == tree.url.rstrip("/"))  # fmt: skip
    url = out.url or (tree.url if same else None) or PUBLIC.get(kind)
    if url is None:
        raise ReviewHostError(f"output.{kind}.url is needed (or trees.target.host)")
    return RepoHost(
        kind=kind,
        url=url,
        api_url=out.api_url or (tree.api_url if same else None),
        user=out.user or (tree.user if same else None),
        token=out.token or (tree.token if same else None),
    )


def git_url_for(config: ProjectConfig, repo: str) -> str:
    kind, out = output_of(config)
    if out.git_url:
        return out.git_url.format(owner=out.owner, repo=repo)
    base = host_of(config).url.rstrip("/")
    if kind == "bitbucket_server":
        return f"{base}/scm/{out.owner.lower()}/{repo}.git"
    return f"{base}/{out.owner}/{repo}.git"


def push(repo: Path, rev: str, url: str, branch: str, env: dict[str, str] | None) -> None:
    """Force-push `rev` to `branch` at `url` (the branch belongs to the tool)."""
    git(repo, "-c", "credential.helper=", "push", "-q", "--force", url,
        f"{rev}:refs/heads/{branch}", env={"GIT_TERMINAL_PROMPT": "0", **(env or {})})  # fmt: skip


def push_env(config: ProjectConfig) -> dict[str, str] | None:
    try:
        return git_env(host_of(config))
    except (KeyError, OSError):
        return None


class _Http:
    name = "host"
    extra_headers: dict[str, str] = {}

    def comments_by(self, repo: str, number: int, user: str) -> str:
        """The user's latest comments on the PR, newest first ("" if they can't be read)."""
        try:
            return _feedback(self._comments(repo, number, user))
        except ReviewHostError:
            return ""

    def _comments(self, repo: str, number: int, user: str) -> list[str]:
        return []

    def __init__(self, host: RepoHost, owner: str, timeout: float = 30):
        self.host = host
        self.owner = owner
        self.base = api_base(host)
        self.headers = api_headers(host, token_of(host))
        self.timeout = timeout

    def _call(self, method: str, path: str, body: dict | None = None, params=None):
        url = path if path.startswith("http") else f"{self.base}{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params, doseq=True)
        req = urllib.request.Request(
            url,
            method=method,
            data=json.dumps(body).encode() if body is not None else None,
            headers={
                **self.headers,
                "Accept": "application/json",
                "Content-Type": "application/json",
                **self.extra_headers,
            },  # fmt: skip
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read()
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")[:500]
            raise ReviewHostError(f"{self.name} {method} {path}: HTTP {e.code} {detail}") from e
        except urllib.error.URLError as e:
            raise ReviewHostError(f"{self.name} not reachable at {self.base}: {e.reason}") from e


def _feedback(texts: list[str], limit: int = 3) -> str:
    return "\n\n".join(t.strip() for t in texts[:limit] if t and t.strip())


class HttpGithub(_Http):
    name = "GitHub"
    extra_headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}

    def _r(self, repo: str) -> str:
        return f"/repos/{self.owner}/{repo}"

    @staticmethod
    def _pr(pr: dict) -> dict:
        return {**pr, "merged": bool(pr.get("merged") or pr.get("merged_at")),
                "head": (pr.get("head") or {}).get("ref", pr.get("head"))}  # fmt: skip

    def find_pr(self, repo, head):
        prs = self._call("GET", f"{self._r(repo)}/pulls",
                         params={"head": f"{self.owner}:{head}", "state": "all"})  # fmt: skip
        return self._pr(prs[0]) if prs else None

    def create_pr(self, repo, title, body, head, base, draft):
        return self._pr(self._call("POST", f"{self._r(repo)}/pulls", {
            "title": title, "body": body, "head": head, "base": base, "draft": draft}))  # fmt: skip

    def update_pr(self, repo, number, title, body):
        return self._pr(self._call("PATCH", f"{self._r(repo)}/pulls/{number}",
                                   {"title": title, "body": body}))  # fmt: skip

    def get_pr(self, repo, number):
        return self._pr(self._call("GET", f"{self._r(repo)}/pulls/{number}"))

    def reviews(self, repo, number):
        return self._call("GET", f"{self._r(repo)}/pulls/{number}/reviews",
                          params={"per_page": 100})  # fmt: skip

    def request_reviewers(self, repo, number, reviewers):
        if reviewers:
            self._call("POST", f"{self._r(repo)}/pulls/{number}/requested_reviewers",
                       {"reviewers": reviewers})  # fmt: skip

    def add_labels(self, repo, number, labels):
        if labels:
            self._call("POST", f"{self._r(repo)}/issues/{number}/labels", {"labels": labels})


class HttpGitlab(_Http):
    name = "GitLab"

    def _r(self, repo: str) -> str:
        return f"/projects/{urllib.parse.quote(f'{self.owner}/{repo}', safe='')}"

    @staticmethod
    def _pr(mr: dict) -> dict:
        state = mr.get("state")
        return {
            "number": mr["iid"],
            "html_url": mr.get("web_url"),
            "state": "open" if state == "opened" else "closed",
            "merged": state == "merged",
            "merged_by": {"login": (mr.get("merged_by") or {}).get("username", "gitlab")},
            "head": mr.get("source_branch"),
            "sha": mr.get("sha"),
            "draft": mr.get("draft", False),
            "title": mr.get("title"),
        }

    def _user_ids(self, names: list[str]) -> list[int]:
        ids = []
        for name in names:
            found = self._call("GET", "/users", params={"username": name})
            if found:
                ids.append(found[0]["id"])
        return ids

    def find_pr(self, repo, head):
        mrs = self._call("GET", f"{self._r(repo)}/merge_requests",
                         params={"source_branch": head, "state": "all"})  # fmt: skip
        return self._pr(mrs[0]) if mrs else None

    def create_pr(self, repo, title, body, head, base, draft):
        return self._pr(self._call("POST", f"{self._r(repo)}/merge_requests", {
            "title": f"Draft: {title}" if draft else title, "description": body,
            "source_branch": head, "target_branch": base,
            "remove_source_branch": True}))  # fmt: skip

    def update_pr(self, repo, number, title, body):
        current = self.get_pr(repo, number)
        title = f"Draft: {title}" if current["draft"] else title
        return self._pr(self._call("PUT", f"{self._r(repo)}/merge_requests/{number}",
                                   {"title": title, "description": body}))  # fmt: skip

    def get_pr(self, repo, number):
        return self._pr(self._call("GET", f"{self._r(repo)}/merge_requests/{number}"))

    def reviews(self, repo, number):
        sha = self.get_pr(repo, number)["sha"]
        out = []
        for r in self._call("GET", f"{self._r(repo)}/merge_requests/{number}/reviewers") or []:
            user = (r.get("user") or {}).get("username", "gitlab")
            state = {"approved": "APPROVED", "requested_changes": "CHANGES_REQUESTED"}.get(
                r.get("state"), "COMMENTED")  # fmt: skip
            body = self.comments_by(repo, number, user) if state == "CHANGES_REQUESTED" else ""
            out.append({"id": f"{user}:{state}:{sha}", "state": state, "user": {"login": user},
                        "body": body})  # fmt: skip
        approvals = self._call("GET", f"{self._r(repo)}/merge_requests/{number}/approvals") or {}
        for a in approvals.get("approved_by", []):
            user = (a.get("user") or {}).get("username", "gitlab")
            out.append({"id": f"{user}:APPROVED:{sha}", "state": "APPROVED",
                        "user": {"login": user}, "body": ""})  # fmt: skip
        return out

    def _comments(self, repo, number, user):
        notes = self._call(
            "GET",
            f"{self._r(repo)}/merge_requests/{number}/notes",
            params={"sort": "desc", "order_by": "created_at", "per_page": 50},
        )
        return [
            n.get("body", "")
            for n in notes or []
            if not n.get("system") and (n.get("author") or {}).get("username") == user
        ]

    def request_reviewers(self, repo, number, reviewers):
        if ids := self._user_ids(reviewers):
            self._call("PUT", f"{self._r(repo)}/merge_requests/{number}", {"reviewer_ids": ids})

    def add_labels(self, repo, number, labels):
        if labels:
            self._call("PUT", f"{self._r(repo)}/merge_requests/{number}",
                       {"add_labels": ",".join(labels)})  # fmt: skip


class HttpBitbucketCloud(_Http):
    name = "Bitbucket Cloud"

    def _r(self, repo: str) -> str:
        return f"/repositories/{self.owner}/{repo}/pullrequests"

    @staticmethod
    def _pr(pr: dict) -> dict:
        state = pr.get("state")
        by = pr.get("closed_by") or {}
        return {
            "number": pr["id"],
            "html_url": ((pr.get("links") or {}).get("html") or {}).get("href"),
            "state": "open" if state == "OPEN" else "closed",
            "merged": state == "MERGED",
            "merged_by": {"login": by.get("nickname") or by.get("display_name") or "bitbucket"},
            "head": ((pr.get("source") or {}).get("branch") or {}).get("name"),
            "sha": ((pr.get("source") or {}).get("commit") or {}).get("hash"),
            "title": pr.get("title"),
            "participants": pr.get("participants", []),
        }

    @staticmethod
    def _who(r: str) -> dict:
        return {"uuid": r} if r.startswith("{") else {"account_id": r}

    def find_pr(self, repo, head):
        q = f'source.branch.name = "{head}"'
        data = self._call("GET", self._r(repo), params={
            "q": q, "state": ["OPEN", "MERGED", "DECLINED", "SUPERSEDED"]})  # fmt: skip
        values = (data or {}).get("values", [])
        return self._pr(values[0]) if values else None

    def create_pr(self, repo, title, body, head, base, draft):
        return self._pr(self._call("POST", self._r(repo), {
            "title": title, "description": body, "draft": draft,
            "source": {"branch": {"name": head}}, "destination": {"branch": {"name": base}},
            "close_source_branch": True}))  # fmt: skip

    def update_pr(self, repo, number, title, body):
        return self._pr(self._call("PUT", f"{self._r(repo)}/{number}",
                                   {"title": title, "description": body}))  # fmt: skip

    def get_pr(self, repo, number):
        return self._pr(self._call("GET", f"{self._r(repo)}/{number}"))

    def reviews(self, repo, number):
        pr = self.get_pr(repo, number)
        out = []
        for p in pr["participants"]:
            raw = p.get("state") or ("approved" if p.get("approved") else "")
            state = {"approved": "APPROVED", "changes_requested": "CHANGES_REQUESTED"}.get(
                raw, "COMMENTED")  # fmt: skip
            user = p.get("user") or {}
            login = user.get("nickname") or user.get("display_name") or "bitbucket"
            body = self.comments_by(repo, number, user.get("uuid", login)) if (
                state == "CHANGES_REQUESTED") else ""  # fmt: skip
            out.append({"id": f"{user.get('uuid', login)}:{state}:{pr['sha']}", "state": state,
                        "user": {"login": login}, "body": body})  # fmt: skip
        return out

    def _comments(self, repo, number, user):
        data = self._call(
            "GET",
            f"{self._r(repo)}/{number}/comments",
            params={"sort": "-created_on", "pagelen": 50},
        )
        return [
            ((c.get("content") or {}).get("raw") or "")
            for c in (data or {}).get("values", [])
            if not c.get("deleted") and (c.get("user") or {}).get("uuid") == user
        ]

    def request_reviewers(self, repo, number, reviewers):
        if reviewers:
            pr = self.get_pr(repo, number)
            self._call("PUT", f"{self._r(repo)}/{number}", {
                "title": pr["title"], "reviewers": [self._who(r) for r in reviewers]})  # fmt: skip

    def add_labels(self, repo, number, labels):
        return None  # Bitbucket has no PR labels


class HttpBitbucketServer(_Http):
    name = "Bitbucket Server"

    def _r(self, repo: str) -> str:
        return f"/projects/{self.owner}/repos/{repo}/pull-requests"

    @staticmethod
    def _pr(pr: dict) -> dict:
        state = pr.get("state")
        links = (pr.get("links") or {}).get("self") or [{}]
        return {
            "number": pr["id"],
            "html_url": links[0].get("href"),
            "state": "open" if state == "OPEN" else "closed",
            "merged": state == "MERGED",
            "merged_by": {"login": "bitbucket"},
            "head": (pr.get("fromRef") or {}).get("displayId"),
            "sha": (pr.get("fromRef") or {}).get("latestCommit"),
            "title": pr.get("title"),
            "version": pr.get("version", 0),
            "reviewers": pr.get("reviewers", []),
        }

    def _ref(self, repo: str, branch: str) -> dict:
        return {"id": f"refs/heads/{branch}",
                "repository": {"slug": repo, "project": {"key": self.owner}}}  # fmt: skip

    def find_pr(self, repo, head):
        data = self._call("GET", self._r(repo), params={
            "state": "ALL", "direction": "OUTGOING", "at": f"refs/heads/{head}"})  # fmt: skip
        hits = [p for p in (data or {}).get("values", [])
                if (p.get("fromRef") or {}).get("displayId") == head]  # fmt: skip
        return self._pr(hits[0]) if hits else None

    def create_pr(self, repo, title, body, head, base, draft):
        return self._pr(self._call("POST", self._r(repo), {
            "title": title, "description": body, "draft": draft,
            "fromRef": self._ref(repo, head), "toRef": self._ref(repo, base)}))  # fmt: skip

    def update_pr(self, repo, number, title, body):
        pr = self.get_pr(repo, number)
        return self._pr(self._call("PUT", f"{self._r(repo)}/{number}", {
            "version": pr["version"], "title": title, "description": body,
            "reviewers": pr["reviewers"]}))  # fmt: skip

    def get_pr(self, repo, number):
        return self._pr(self._call("GET", f"{self._r(repo)}/{number}"))

    def reviews(self, repo, number):
        pr = self.get_pr(repo, number)
        out = []
        for r in pr["reviewers"]:
            state = {"APPROVED": "APPROVED", "NEEDS_WORK": "CHANGES_REQUESTED"}.get(
                r.get("status"), "COMMENTED")  # fmt: skip
            login = (r.get("user") or {}).get("name", "bitbucket")
            body = self.comments_by(repo, number, login) if state == "CHANGES_REQUESTED" else ""
            out.append({"id": f"{login}:{state}:{pr['sha']}", "state": state,
                        "user": {"login": login}, "body": body})  # fmt: skip
        return out

    def _comments(self, repo, number, user):
        data = self._call("GET", f"{self._r(repo)}/{number}/activities", params={"limit": 100})
        return [
            ((a.get("comment") or {}).get("text") or "")
            for a in (data or {}).get("values", [])
            if a.get("action") == "COMMENTED" and (a.get("user") or {}).get("name") == user
        ]

    def request_reviewers(self, repo, number, reviewers):
        if reviewers:
            pr = self.get_pr(repo, number)
            have = {(r.get("user") or {}).get("name") for r in pr["reviewers"]}
            wanted = pr["reviewers"] + [{"user": {"name": n}} for n in reviewers if n not in have]
            self._call("PUT", f"{self._r(repo)}/{number}", {
                "version": pr["version"], "title": pr["title"], "reviewers": wanted})  # fmt: skip

    def add_labels(self, repo, number, labels):
        return None  # Bitbucket Server has no PR labels


CLIENTS = {"github": HttpGithub, "gitlab": HttpGitlab, "bitbucket_cloud": HttpBitbucketCloud,
           "bitbucket_server": HttpBitbucketServer}  # fmt: skip


def client_for(config: ProjectConfig) -> ReviewApi:
    """The REST client for output.target. Raises KeyError/OSError if the token is missing."""
    kind, out = output_of(config)
    return CLIENTS[kind](host_of(config), out.owner)
