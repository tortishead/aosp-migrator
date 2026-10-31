"""Is the GitHub token the `copilot` CLI reads from the backend's environment there, and valid?

The CLI takes the first of `TOKEN_VARIABLES` that is set, else its stored `/login`
credential (or `gh auth`). It inherits the backend's environment, so a token exported
only in another shell, or after the backend started, never reaches it.

Valid: GitHub's `GET /user` accepts it. That shows the token authenticates; whether its
account has Copilot only a Copilot call shows. Classic PATs (`ghp_`) authenticate there
but the CLI refuses them.
"""

import os
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from typing import Any, Literal

import httpx2

TOKEN_VARIABLES = ("COPILOT_GITHUB_TOKEN", "GH_TOKEN", "GITHUB_TOKEN")
CLASSIC_PAT_PREFIX = "ghp_"
# Phrases the CLI prints when it has no usable credential.
AUTH_ERRORS = ("no authentication information", "not logged in", "authentication failed", "bad credentials", "unauthorized")

TokenStatus = Literal["missing", "invalid", "valid", "unverified"]


@dataclass
class TokenCheck:
    status: TokenStatus
    # The environment variable the token came from; None when none is set.
    variable: str | None
    # The token's GitHub login when it is valid.
    login: str | None
    message: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def find_token(env: Mapping[str, str] | None = None) -> tuple[str, str] | None:
    """(variable, token) for the first token variable set, as the CLI picks it."""
    env = os.environ if env is None else env
    for variable in TOKEN_VARIABLES:
        if token := env.get(variable, "").strip():
            return variable, token
    return None


def missing_message() -> str:
    return (
        "COPILOT_GITHUB_TOKEN is not set in the backend's environment (nor GH_TOKEN or GITHUB_TOKEN). "
        "Export it in the shell that starts the backend (`export COPILOT_GITHUB_TOKEN=...`, above any "
        "non-interactive `return` in ~/.bashrc) and restart the backend, or log the CLI in with `copilot` and `/login`."
    )


def api_url(env: Mapping[str, str] | None = None) -> str:
    """REST API root for `GH_HOST` (GitHub Enterprise Server), else github.com."""
    env = os.environ if env is None else env
    host = env.get("GH_HOST", "").strip().removeprefix("https://").removeprefix("http://").rstrip("/")
    if not host or host == "github.com":
        return "https://api.github.com"
    return f"https://{host}/api/v3"


def check_token(env: Mapping[str, str] | None = None, transport: httpx2.BaseTransport | None = None, timeout: float = 10) -> TokenCheck:
    """Look for the token and ask GitHub who it belongs to; never raises."""
    env = os.environ if env is None else env
    found = find_token(env)
    if found is None:
        return TokenCheck("missing", None, None, missing_message())
    variable, token = found
    if token.startswith(CLASSIC_PAT_PREFIX):
        return TokenCheck(
            "invalid",
            variable,
            None,
            f"{variable} holds a classic personal access token (ghp_...), which the Copilot CLI does not accept. "
            "Use a fine-grained token with the Copilot Requests permission, or an OAuth token.",
        )
    url = f"{api_url(env)}/user"
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    try:
        with httpx2.Client(transport=transport, timeout=timeout) as http:
            response = http.get(url, headers=headers)
    except httpx2.HTTPError as exc:
        return TokenCheck("unverified", variable, None, f"{variable} is set, but GitHub could not be reached to check it: {exc}")
    if response.status_code == 401:
        return TokenCheck("invalid", variable, None, f"{variable} is not valid: GitHub rejected it (HTTP 401, bad credentials). It may be mistyped, expired or revoked.")
    if response.status_code != 200:
        return TokenCheck("unverified", variable, None, f"{variable} is set, but GitHub answered HTTP {response.status_code} when checking it.")
    try:
        login = response.json().get("login")
    except ValueError:
        login = None
    who = f" for GitHub user {login}" if login else ""
    return TokenCheck("valid", variable, login, f"{variable} is valid{who}.")


def auth_hint(error: str, env: Mapping[str, str] | None = None) -> str | None:
    """For a CLI error about authentication, what the environment says about the token (no network)."""
    if not any(phrase in error.lower() for phrase in AUTH_ERRORS):
        return None
    found = find_token(env)
    if found is None:
        return missing_message()
    return f"{found[0]} is set in the backend's environment but was not accepted: check it with Test Copilot in Settings."
