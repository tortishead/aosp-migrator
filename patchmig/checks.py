"""Connection tests for the configured services, shared by `migrate check` and the web settings.

Each check returns (name, ok, detail) lines. None of them costs Copilot credits or changes
anything: they sign in, list or read. Details never contain a secret, only whether it is set.
"""

from __future__ import annotations

import json
import os
import subprocess
import urllib.error
import urllib.request

from patchmig.agents.copilot import copilot_env, github_api, github_kind
from patchmig.config import ProjectConfig
from patchmig.config.schema import RepoHost, resolve_secret
from patchmig.gitutil import is_repo, resolve
from patchmig.ingest.manifest import ManifestError, tree_projects

Check = tuple[str, bool, str]
CHECKS = ("trees", "hosts", "copilot", "jira", "build")
# The config sections each check reads: it runs even while the rest of the config is incomplete.
SECTIONS = {"trees": ("trees",), "hosts": ("trees", "code_hosts", "output"), "copilot": ("llm",),
            "jira": ("jira",), "build": ("build", "trees")}  # fmt: skip


def _secret_problem(ref: str | None) -> str | None:
    try:
        if ref:
            resolve_secret(ref)
        return None
    except (KeyError, OSError) as e:
        return f"{ref.partition(':')[0]} reference not available ({type(e).__name__}: {e})"


def check_trees(config: ProjectConfig) -> list[Check]:
    out: list[Check] = []
    for side in ("source", "target"):
        tree = getattr(config.trees, side)
        if not tree.path.is_dir():
            out.append((f"{side} tree", False, f"{tree.path} is not a folder"))
            continue
        try:
            projects = tree_projects(tree.path, tree.manifest)
        except ManifestError as e:
            out.append((f"{side} tree", False, str(e)))
            continue
        repos = [p for p in projects if is_repo(tree.path / p)]
        missing = [p for p in repos if resolve(tree.path / p, tree.base_ref) is None]
        detail = f"{len(repos)} of {len(projects)} projects checked out"
        if missing:
            detail += f"; base_ref {tree.base_ref} missing in {len(missing)}, e.g. {missing[0]}"
        out.append((f"{side} tree {tree.path}", bool(repos) and not missing, detail))
    return out


def _who(host: RepoHost) -> str:
    from patchmig.ingest.codehosts import HostClient

    client = HostClient(host)
    if host.kind == "bitbucket_server":
        data = client._call("/projects?limit=1")
        return f"token accepted ({data.get('size', 0)}+ projects visible)"
    me = client._call("/user")
    return "signed in as " + str(me.get("login") or me.get("username") or
                                 me.get("display_name") or me.get("nickname") or "?")  # fmt: skip


def check_hosts(config: ProjectConfig) -> list[Check]:
    from patchmig.ingest.codehosts import CodeHostError
    from patchmig.publish.hosts import PR_HOSTS, host_of

    out: list[Check] = []
    hosts = list(config.hosts())
    if (
        config.output
        and config.output.target in PR_HOSTS
        and getattr(config.output, config.output.target)
    ):
        publish_to = host_of(config)  # its own token may differ from the target tree's
        if all((h.url, h.token) != (publish_to.url, publish_to.token) for h in hosts):
            hosts.append(publish_to)
    for host in hosts:
        name = f"{host.kind} {host.url}"
        if not host.token:
            out.append((name, True, "no token: public repositories only"))
            continue
        if problem := _secret_problem(host.token):
            out.append((name, False, f"token: {problem}"))
            continue
        try:
            out.append((name, True, _who(host)))
        except CodeHostError as e:
            out.append((name, False, str(e)))
    if not out:
        out.append(("git hosts", True, "none configured"))
    return out


def check_copilot(config: ProjectConfig) -> list[Check]:
    llm, out = config.llm, []
    try:
        env = copilot_env(llm)
    except (KeyError, OSError):
        return [("Copilot token", False, f"llm.github_token: {_secret_problem(llm.github_token)}")]
    try:
        proc = subprocess.run([llm.executable, "--version"], capture_output=True, text=True,
                              timeout=60, env={"COPILOT_AUTO_UPDATE": "false", **os.environ,
                                               **env})  # fmt: skip
        version = (proc.stdout or proc.stderr).strip().splitlines()
        out.append(("Copilot CLI", proc.returncode == 0, version[0] if version else "no output"))
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        out.append(("Copilot CLI", False, f"not runnable as '{llm.executable}': {e}"))
    host = llm.github_host or "github.com"
    if not llm.github_token:
        out.append((f"sign-in on {host}", True, "no token set: the CLI uses its own login"))
        return out
    token = resolve_secret(llm.github_token)
    if token.startswith("ghp_"):
        why = ("classic PATs (ghp_) do not work with Copilot CLI; use a fine-grained PAT with "
               "the Copilot Requests permission")  # fmt: skip
        out.append((f"token for {host}", False, why))
        return out
    req = urllib.request.Request(f"{github_api(host)}/user", headers={
        "Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"})  # fmt: skip
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            login = json.loads(resp.read() or b"{}").get("login", "?")
        out.append((f"token for {host} ({github_kind(host)})", True, f"signed in as {login}"))
    except urllib.error.HTTPError as e:
        out.append((f"token for {host}", False, f"HTTP {e.code} from {github_api(host)}/user"))
    except urllib.error.URLError as e:
        out.append((f"token for {host}", False, f"{github_api(host)} not reachable: {e.reason}"))
    return out


def check_jira(config: ProjectConfig) -> list[Check]:
    from patchmig.jira_client import HttpJiraClient, JiraError

    if config.jira is None:
        return [("Jira", True, "not configured")]
    if problem := _secret_problem(config.jira.token):
        return [("Jira token", False, problem)]
    try:
        return [("Jira", True, line) for line in HttpJiraClient(config.jira).check(
            config.jira.project)]  # fmt: skip
    except JiraError as e:
        return [("Jira", False, str(e))]


def check_build(config: ProjectConfig) -> list[Check]:
    from patchmig.verify import VerifyError, check_host

    if config.build is None:
        return [("build host", True, "not configured")]
    if config.build.ssh and (problem := _secret_problem(config.build.ssh.key)):
        return [("build SSH key", False, problem)]
    try:
        return check_host(config)
    except VerifyError as e:
        return [("build host", False, str(e))]


def run_check(name: str, config: ProjectConfig) -> list[Check]:
    fn = {"trees": check_trees, "hosts": check_hosts, "copilot": check_copilot,
          "jira": check_jira, "build": check_build}[name]  # fmt: skip
    return fn(config)
