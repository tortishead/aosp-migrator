"""GitHub REST access for publishing (no GraphQL), and pushing branches with the git CLI.

The token is a secret reference. For pushes it reaches git through GIT_ASKPASS and an
environment variable, so it never appears in a command line, a remote URL or a log.
"""

from __future__ import annotations

import json
import stat
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Protocol

from patchmig.config.schema import GithubOutput, resolve_secret
from patchmig.gitutil import git


class GithubError(Exception):
    pass


class GithubApi(Protocol):
    def find_pr(self, repo: str, head: str) -> dict | None: ...

    def create_pr(
        self, repo: str, title: str, body: str, head: str, base: str, draft: bool
    ) -> dict: ...

    def update_pr(self, repo: str, number: int, title: str, body: str) -> dict: ...

    def get_pr(self, repo: str, number: int) -> dict: ...

    def reviews(self, repo: str, number: int) -> list[dict]: ...

    def request_reviewers(self, repo: str, number: int, reviewers: list[str]) -> None: ...

    def add_labels(self, repo: str, number: int, labels: list[str]) -> None: ...


class HttpGithub:
    def __init__(self, config: GithubOutput, timeout: float = 30):
        self.config = config
        self.owner = config.owner
        self.base = config.api_url.rstrip("/")
        self._token = resolve_secret(config.token)
        self.timeout = timeout

    def _call(self, method: str, path: str, body: dict | None = None, params=None):
        url = f"{self.base}/repos/{self.owner}/{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(
            url,
            method=method,
            data=json.dumps(body).encode() if body is not None else None,
            headers={
                "Authorization": f"Bearer {self._token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read()
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")[:500]
            raise GithubError(f"GitHub {method} {path}: HTTP {e.code} {detail}") from e
        except urllib.error.URLError as e:
            raise GithubError(f"GitHub not reachable at {self.base}: {e.reason}") from e

    def find_pr(self, repo, head):
        prs = self._call("GET", f"{repo}/pulls", params={"head": f"{self.owner}:{head}",
                                                         "state": "all"})  # fmt: skip
        return prs[0] if prs else None

    def create_pr(self, repo, title, body, head, base, draft):
        return self._call("POST", f"{repo}/pulls", {"title": title, "body": body, "head": head,
                                                    "base": base, "draft": draft})  # fmt: skip

    def update_pr(self, repo, number, title, body):
        return self._call("PATCH", f"{repo}/pulls/{number}", {"title": title, "body": body})

    def get_pr(self, repo, number):
        return self._call("GET", f"{repo}/pulls/{number}")

    def reviews(self, repo, number):
        return self._call("GET", f"{repo}/pulls/{number}/reviews", params={"per_page": 100})

    def request_reviewers(self, repo, number, reviewers):
        if reviewers:
            self._call("POST", f"{repo}/pulls/{number}/requested_reviewers",
                       {"reviewers": reviewers})  # fmt: skip

    def add_labels(self, repo, number, labels):
        if labels:
            self._call("POST", f"{repo}/issues/{number}/labels", {"labels": labels})


def push(repo: Path, rev: str, url: str, branch: str, token: str | None) -> None:
    """Force-push `rev` to `branch` at `url` (the branch belongs to the tool)."""
    env = {"GIT_TERMINAL_PROMPT": "0"}
    with tempfile.TemporaryDirectory(prefix="patchmig-push-") as tmp:
        if token:
            askpass = Path(tmp) / "askpass.sh"
            # Username prompt gets a fixed name, password prompt the token from the env.
            askpass.write_text(
                '#!/bin/sh\ncase "$1" in Username*) echo x-access-token;; '
                '*) echo "$PATCHMIG_PUSH_TOKEN";; esac\n'
            )
            askpass.chmod(stat.S_IRWXU)
            env |= {"GIT_ASKPASS": str(askpass), "PATCHMIG_PUSH_TOKEN": token}
        git(repo, "-c", "credential.helper=", "push", "-q", "--force", url,
            f"{rev}:refs/heads/{branch}", env=env)  # fmt: skip


def push_token(config: GithubOutput) -> str | None:
    try:
        return resolve_secret(config.token)
    except (KeyError, OSError):
        return None
