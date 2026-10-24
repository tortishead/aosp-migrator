"""Signing in to Git hosts (GitHub, GitLab, Bitbucket Cloud and Server/DC) with a token.

The same token works for the REST API and for git over HTTPS, but each host wants it in a
different shape. For git the token never goes on the command line (other users can read
that): it is passed as an `http.<url>.extraHeader` through git's GIT_CONFIG_* environment
variables, which only applies to URLs under the host's base URL.
"""

from __future__ import annotations

import base64
import os

from patchmig.config.schema import RepoHost, resolve_secret

# Basic-auth user names hosts accept with a token alone
TOKEN_USER = {"github": "x-access-token", "gitlab": "oauth2", "bitbucket_cloud": "x-token-auth"}


def api_base(host: RepoHost) -> str:
    if host.api_url:
        return host.api_url.rstrip("/")
    base = host.url.rstrip("/")
    return {
        "github": "https://api.github.com" if base == "https://github.com" else f"{base}/api/v3",
        "gitlab": f"{base}/api/v4",
        "bitbucket_cloud": "https://api.bitbucket.org/2.0",
        "bitbucket_server": f"{base}/rest/api/1.0",
    }[host.kind]


def token_of(host: RepoHost | None) -> str | None:
    """The host's token, or None. Raises KeyError or OSError if its reference is broken."""
    return resolve_secret(host.token) if host and host.token else None


def _basic(user: str, token: str) -> str:
    return "Basic " + base64.b64encode(f"{user}:{token}".encode()).decode()


def api_headers(host: RepoHost, token: str | None) -> dict[str, str]:
    if not token:
        return {}
    if host.kind == "gitlab":
        return {"PRIVATE-TOKEN": token}
    if host.user:
        return {"Authorization": _basic(host.user, token)}
    return {"Authorization": f"Bearer {token}"}


def git_auth_header(host: RepoHost, token: str) -> str:
    if host.user:
        return _basic(host.user, token)
    if host.kind == "bitbucket_server":
        return f"Bearer {token}"  # HTTP access tokens
    return _basic(TOKEN_USER[host.kind], token)


def git_env(host: RepoHost | None) -> dict[str, str] | None:
    """Environment for git commands that reach the host over HTTPS; None without a token."""
    token = token_of(host)
    if not token:
        return None
    n = int(os.environ.get("GIT_CONFIG_COUNT", "0") or 0)
    return {
        "GIT_CONFIG_COUNT": str(n + 1),
        f"GIT_CONFIG_KEY_{n}": f"http.{host.url.rstrip('/')}/.extraHeader",
        f"GIT_CONFIG_VALUE_{n}": f"Authorization: {git_auth_header(host, token)}",
        "GIT_TERMINAL_PROMPT": "0",
    }
