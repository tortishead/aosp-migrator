"""Commit and pull-request links on GitHub, GitLab and Bitbucket (Cloud and Server/DC).

Implementation tickets often point at the patch as a link instead of carrying it: a commit
URL, or a PR / merge request URL. A commit link needs no API: its SHA is looked up in the
source tree. A PR link needs the host's REST API to list its commits (looked up in the source
tree too) and, when they are not there (squashed or rebased on merge), to fetch the PR diff.

Hosts are configured under `code_hosts` (URL, kind, token, repo_map) and as the trees' hosts;
github.com, gitlab.com and bitbucket.org work without configuration for public repositories.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

from patchmig.config.schema import RepoHost
from patchmig.githosts import api_base, api_headers, token_of

_URL = re.compile(r"https?://[^\s<>\"'\]\[|)]+")
_SHA = r"(?P<sha>[0-9a-f]{7,40})"
_NUM = r"(?P<num>\d+)"
_BBS = r"^/(?:projects|users)/(?P<owner>[^/]+)/repos/(?P<repo>[^/]+)"
# (kind, pattern over the path after the host's base path)
_SHAPES = [
    ("bitbucket_server", re.compile(rf"{_BBS}/commits/{_SHA}")),
    ("bitbucket_server", re.compile(rf"{_BBS}/pull-requests/{_NUM}")),
    ("gitlab", re.compile(rf"^/(?P<repo>.+?)/-/commits?/{_SHA}")),
    ("gitlab", re.compile(rf"^/(?P<repo>.+?)/-/merge_requests/{_NUM}")),
    ("bitbucket_cloud", re.compile(rf"^/(?P<repo>[^/]+/[^/]+)/commits?/{_SHA}")),
    ("bitbucket_cloud", re.compile(rf"^/(?P<repo>[^/]+/[^/]+)/pull-requests/{_NUM}")),
    ("github", re.compile(rf"^/(?P<repo>[^/]+/[^/]+)/commit/{_SHA}")),
    ("github", re.compile(rf"^/(?P<repo>[^/]+/[^/]+)/pull/{_NUM}")),
]  # fmt: skip
_DEFAULT_KIND = {"github.com": "github", "gitlab.com": "gitlab", "bitbucket.org": "bitbucket_cloud"}


class CodeHostError(Exception):
    pass


@dataclass(frozen=True)
class CodeRef:
    url: str
    kind: str  # github, gitlab, bitbucket_cloud, bitbucket_server
    base: str  # web base URL of the host, e.g. https://github.com
    repo: str  # owner/repo, group/sub/repo, or PROJECT/repo (Bitbucket Server)
    sha: str | None = None
    number: int | None = None  # PR / merge request


@dataclass
class PullRequest:
    title: str
    body: str
    commits: list[tuple[str, str]]  # (sha, message), oldest first


def find_refs(text: str, hosts: list[RepoHost]) -> list[CodeRef]:
    """Commit and PR links in free text (ticket descriptions, comments, remote links)."""
    refs: dict[str, CodeRef] = {}
    for raw in _URL.findall(text or ""):
        url = raw.rstrip(".,;:")
        if ref := parse_ref(url, hosts):
            refs.setdefault(f"{ref.repo}@{ref.sha or ref.number}", ref)
    return list(refs.values())


def parse_ref(url: str, hosts: list[RepoHost]) -> CodeRef | None:
    u = urllib.parse.urlparse(url)
    host = next((h for h in hosts if url.startswith(h.url.rstrip("/") + "/")), None)
    base = host.url.rstrip("/") if host else f"{u.scheme}://{u.netloc}"
    path = u.path[len(urllib.parse.urlparse(base).path) :] if host else u.path
    kind = host.kind if host else _DEFAULT_KIND.get(u.hostname or "")
    for shape_kind, pattern in _SHAPES:
        if kind and shape_kind != kind:
            continue
        if m := pattern.match(path):
            g = m.groupdict()
            repo = f"{g['owner']}/{g['repo']}" if "owner" in g else g["repo"]
            return CodeRef(url, shape_kind, base, repo, g.get("sha"),
                           int(g["num"]) if g.get("num") else None)  # fmt: skip
    return None


def host_for(ref: CodeRef, hosts: list[RepoHost]) -> RepoHost:
    for h in hosts:
        if h.url.rstrip("/") == ref.base:
            return h
    return RepoHost(url=ref.base, kind=ref.kind)  # public host, no credentials


def gitlab_unified(changes: list[dict]) -> str:
    """GitLab's JSON diffs as one unified diff."""
    out = []
    for c in changes:
        old, new = c.get("old_path"), c.get("new_path")
        out.append(f"diff --git a/{old} b/{new}\n")
        if c.get("new_file"):
            out.append(f"new file mode {c.get('b_mode') or '100644'}\n")
        if c.get("deleted_file"):
            out.append(f"deleted file mode {c.get('a_mode') or '100644'}\n")
        if c.get("renamed_file"):
            out.append(f"rename from {old}\nrename to {new}\n")
        body = c.get("diff") or ""
        if body:
            out.append(f"--- {'/dev/null' if c.get('new_file') else 'a/' + old}\n")
            out.append(f"+++ {'/dev/null' if c.get('deleted_file') else 'b/' + new}\n")
            out.append(body if body.endswith("\n") else body + "\n")
    return "".join(out)


class HostClient:
    def __init__(self, host: RepoHost, timeout: float = 30):
        self.host = host
        self.timeout = timeout
        self.base = host.url.rstrip("/")
        self.headers = api_headers(host, token_of(host))
        self.api = api_base(host)

    def _call(self, path: str, accept: str = "application/json", raw: bool = False):
        url = path if path.startswith("http") else f"{self.api}{path}"
        req = urllib.request.Request(url, headers={**self.headers, "Accept": accept})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                data = resp.read()
        except urllib.error.HTTPError as e:
            raise CodeHostError(f"{self.host.kind} {path}: HTTP {e.code}") from e
        except urllib.error.URLError as e:
            raise CodeHostError(f"{self.base} not reachable: {e.reason}") from e
        return data.decode(errors="replace") if raw else json.loads(data or b"null")

    def _repo_path(self, repo: str) -> str:
        if self.host.kind == "gitlab":
            return f"/projects/{urllib.parse.quote(repo, safe='')}"
        if self.host.kind == "bitbucket_server":
            project, name = repo.split("/", 1)
            return f"/projects/{project}/repos/{name}"
        return f"/repos/{repo}" if self.host.kind == "github" else f"/repositories/{repo}"

    def pull_request(self, ref: CodeRef) -> PullRequest:
        r, n, kind = self._repo_path(ref.repo), ref.number, self.host.kind
        if kind == "github":
            pr = self._call(f"{r}/pulls/{n}")
            commits = self._call(f"{r}/pulls/{n}/commits?per_page=100")
            return PullRequest(pr.get("title", ""), pr.get("body") or "",
                               [(c["sha"], c["commit"]["message"]) for c in commits])  # fmt: skip
        if kind == "gitlab":
            mr = self._call(f"{r}/merge_requests/{n}")
            commits = self._call(f"{r}/merge_requests/{n}/commits?per_page=100")
            return PullRequest(mr.get("title", ""), mr.get("description") or "",
                               [(c["id"], c["message"]) for c in reversed(commits)])  # fmt: skip
        if kind == "bitbucket_cloud":
            pr = self._call(f"{r}/pullrequests/{n}")
            values, page = [], f"{r}/pullrequests/{n}/commits"
            while page:
                data = self._call(page)
                values += data.get("values", [])
                page = data.get("next")
            return PullRequest(pr.get("title", ""), pr.get("description") or "",
                               [(c["hash"], c["message"]) for c in reversed(values)])  # fmt: skip
        pr = self._call(f"{r}/pull-requests/{n}")
        values, start = [], 0
        while True:
            data = self._call(f"{r}/pull-requests/{n}/commits?limit=100&start={start}")
            values += data.get("values", [])
            if data.get("isLastPage", True):
                break
            start = data["nextPageStart"]
        return PullRequest(pr.get("title", ""), pr.get("description") or "",
                           [(c["id"], c["message"]) for c in reversed(values)])  # fmt: skip

    def diff(self, ref: CodeRef) -> str:
        """The unified diff of a commit or a PR."""
        r, kind = self._repo_path(ref.repo), self.host.kind
        if kind == "github":
            what = f"commits/{ref.sha}" if ref.sha else f"pulls/{ref.number}"
            return self._call(f"{r}/{what}", accept="application/vnd.github.diff", raw=True)
        if kind == "gitlab":
            if ref.sha:
                return gitlab_unified(self._call(f"{r}/repository/commits/{ref.sha}/diff"))
            return gitlab_unified(self._call(f"{r}/merge_requests/{ref.number}/diffs?per_page=100"))
        if kind == "bitbucket_cloud":
            what = f"diff/{ref.sha}" if ref.sha else f"pullrequests/{ref.number}/diff"
            return self._call(f"{r}/{what}", accept="text/plain", raw=True)
        if ref.sha:
            return self._call(f"{r}/patch?until={ref.sha}", accept="text/plain", raw=True)
        return self._call(f"{self.base}/rest/api/latest{r}/pull-requests/{ref.number}.diff",
                          accept="text/plain", raw=True)  # fmt: skip
