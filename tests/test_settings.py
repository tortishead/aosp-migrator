"""Settings in the browser: form and YAML saves, the token store, sign-in to hosts and Copilot."""

from __future__ import annotations

import json
import os
import stat
import subprocess
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from patchmig.agents.copilot import CopilotCliAdapter, copilot_env
from patchmig.checks import check_copilot, check_hosts, check_trees
from patchmig.cli import app as cli
from patchmig.config import load_config
from patchmig.config.editor import SettingsEditor, SettingsError, secret_fields
from patchmig.config.schema import LlmConfig, RepoHost
from patchmig.githosts import git_env
from patchmig.ledger import LedgerStore
from patchmig.web.api import create_app
from tests.conftest import MINIMAL_CONFIG
from tests.test_agents import _task, fake_copilot  # noqa: F401  (fixture)
from tests.test_ingest import make_config  # noqa: F401  (fixture)

TOKEN = "github_pat_11SECRETSECRET"


@pytest.fixture
def editor(write_config, tmp_path: Path) -> SettingsEditor:
    path = write_config({"jira": {"url": "https://jira.oem.com", "project": "AUTO",
                                  "token": "env:NO_SUCH_JIRA_TOKEN"}})  # fmt: skip
    path.write_text("# hand-written comment\n" + path.read_text())
    return SettingsEditor(path)


def test_secret_fields_finds_every_token_in_the_schema():
    values = {
        **MINIMAL_CONFIG,
        "trees": {**MINIMAL_CONFIG["trees"], "target": {
            **MINIMAL_CONFIG["trees"]["target"],
            "host": {"kind": "bitbucket_server", "url": "https://bb.oem.com", "token": "env:BB"}}},
        "jira": {"url": "u", "project": "P", "token": "env:J"},
        "code_hosts": [{"kind": "gitlab", "url": "https://gl"},
                       {"kind": "github", "url": "https://gh", "token": "file:/x"}],
        "build": {"lunch_target": "t", "ssh": {"host": "h", "user": "u", "workdir": "/w",
                                               "key": "file:/k"}},
        "llm": {"github_token": "env:COPILOT"},
        "output": {"github": {"owner": "o", "token": "env:GH", "base_branch": "b"}},
    }  # fmt: skip
    assert secret_fields(values) == {
        "trees.target.host.token": "env:BB",
        "jira.token": "env:J",
        "code_hosts.1.token": "file:/x",
        "build.ssh.key": "file:/k",
        "llm.github_token": "env:COPILOT",
        "output.github.token": "env:GH",
    }


def test_typed_token_goes_to_the_store_and_never_comes_back(editor: SettingsEditor):
    view = editor.view()
    assert view["problems"] == [] and view["values"]["jira"]["project"] == "AUTO"
    assert view["secrets"]["jira.token"] == {"ref": "env:NO_SUCH_JIRA_TOKEN", "available": False,
                                             "managed": False}  # fmt: skip
    assert view["schema"]["$defs"]["LlmConfig"]["properties"]["github_token"]["anyOf"][0][
        "x-secret"]  # fmt: skip

    values = {**view["values"], "llm": {"github_host": "oem.ghe.com"}}
    saved = editor.save(values, {"llm.github_token": TOKEN}, view["version"], "xuan")
    ref = saved["values"]["llm"]["github_token"]
    stored = Path(ref.removeprefix("file:"))
    assert ref.startswith("file:") and stored.parent == editor.secrets_dir
    assert stored.read_text().strip() == TOKEN
    assert stat.S_IMODE(stored.stat().st_mode) == 0o600
    assert stat.S_IMODE(editor.secrets_dir.stat().st_mode) == 0o700
    assert (editor.secrets_dir / ".gitignore").read_text() == "*\n"
    assert saved["secrets"]["llm.github_token"] == {"ref": ref, "available": True, "managed": True}
    assert TOKEN not in json.dumps(saved) and TOKEN not in editor.path.read_text()
    assert load_config(editor.path).llm.github_host == "oem.ghe.com"
    history = list(editor.history_dir.iterdir())
    assert len(history) == 1 and "xuan" in history[0].name
    assert history[0].read_text().startswith("# hand-written comment")

    # A new token replaces the stored one; removing the field deletes it.
    again = editor.save(saved["values"], {"llm.github_token": TOKEN + "2"}, saved["version"], "x")
    assert not stored.exists()
    newer = Path(again["values"]["llm"]["github_token"].removeprefix("file:"))
    assert newer.read_text().strip() == TOKEN + "2"
    gone = {**again["values"], "llm": {}}
    editor.save(gone, {}, again["version"], "x")
    assert not newer.exists() and list(editor.secrets_dir.iterdir()) == [
        editor.secrets_dir / ".gitignore"]  # fmt: skip


def test_invalid_or_stale_saves_change_nothing(editor: SettingsEditor):
    view = editor.view()
    before = editor.path.read_text()
    literal = {**view["values"], "jira": {**view["values"]["jira"], "token": "plain-token"}}
    with pytest.raises(SettingsError, match="secrets must be references"):
        editor.save(literal, {}, view["version"], "x")
    bad = {**view["values"], "budget": {"max_fix_attempts": -1}}
    with pytest.raises(SettingsError, match="budget.max_fix_attempts"):
        editor.save(bad, {"llm.github_token": TOKEN}, view["version"], "x")
    with pytest.raises(SettingsError, match="not a secret field"):
        editor.save(view["values"], {"name": TOKEN}, view["version"], "x")
    with pytest.raises(SettingsError) as e:
        editor.save(view["values"], {}, "0" * 16, "x")
    assert e.value.conflict
    assert editor.path.read_text() == before and not editor.secrets_dir.exists()


def test_yaml_save_keeps_comments(editor: SettingsEditor):
    view = editor.view()
    text = view["yaml"].replace("name: test-project", "name: renamed  # by hand")
    saved = editor.save_yaml(text, view["version"], "x")
    assert "# by hand" in editor.path.read_text() and saved["values"]["name"] == "renamed"
    with pytest.raises(SettingsError, match="not valid YAML"):
        editor.save_yaml("name: [", saved["version"], "x")


def test_settings_api_needs_the_admin_token_and_reloads(make_config):  # noqa: F811
    path = make_config([{"type": "git_range"}])
    cfg = load_config(path)
    store = LedgerStore.open(cfg.ledger_path)
    store.create_run(cfg)
    client = TestClient(create_app(cfg, store, SettingsEditor(path), "s3cret"))
    view = client.get("/api/settings").json()
    assert view["editable"] and view["values"]["name"] == "ingest-test"
    assert client.get("/api/settings/auth").status_code == 401
    assert client.get("/api/settings/auth", headers={"Authorization": "Bearer nope"}
                      ).status_code == 401  # fmt: skip
    auth = {"Authorization": "Bearer s3cret"}
    assert client.get("/api/settings/auth", headers=auth).json() == {"ok": True}

    body = {"version": view["version"], "author": "xuan",
            "values": {**view["values"], "name": "renamed"}}  # fmt: skip
    assert client.put("/api/settings", json=body).status_code == 401
    res = client.put("/api/settings", json=body, headers=auth)
    assert res.status_code == 200 and res.json()["reload_error"] is None
    assert client.put("/api/settings", json=body, headers=auth).status_code == 409  # stale
    body = {**body, "version": res.json()["version"], "values": {"name": "x"}}
    res = client.put("/api/settings", json=body, headers=auth)
    assert res.status_code == 422 and any(
        "migration" in p for p in res.json()["detail"]["problems"]
    )

    checks = client.post("/api/settings/check/trees", headers=auth).json()
    assert [c["ok"] for c in checks] == [True, True]
    assert client.post("/api/settings/check/nope", headers=auth).status_code == 404
    assert len(client.get("/api/runs").json()) == 1

    read_only = TestClient(create_app(cfg, store, SettingsEditor(path), None))
    assert not read_only.get("/api/settings").json()["editable"]
    assert read_only.put("/api/settings", json=body, headers=auth).status_code == 403


def test_without_a_valid_config_the_server_starts_with_settings_only(tmp_path: Path):
    path = tmp_path / "new" / "project.yaml"
    client = TestClient(create_app(None, None, SettingsEditor(path), "t"))
    view = client.get("/api/settings").json()
    assert not view["exists"] and view["values"] == {} and client.get("/api/runs").json() == []
    assert client.get("/api/runs/1/summary").status_code == 404
    values = {**MINIMAL_CONFIG, "workdir": str(tmp_path / "work")}
    body = {"version": view["version"], "author": "x", "values": values}
    res = client.put("/api/settings", headers={"Authorization": "Bearer t"}, json=body)
    assert res.status_code == 200 and res.json()["reload_error"] is None
    assert path.exists() and client.get("/api/runs").json() == []  # ledger opened


def test_git_token_reaches_git_only_for_its_host(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("BB_TOKEN", "bbtok")
    cases = [
        (RepoHost(kind="bitbucket_server", url="https://bb.oem.com/", token="env:BB_TOKEN"),
         "Bearer bbtok"),
        (RepoHost(kind="bitbucket_cloud", url="https://bitbucket.org", user="me",
                  token="env:BB_TOKEN"), "Basic bWU6YmJ0b2s="),
        (RepoHost(kind="github", url="https://ghe.oem.com", token="env:BB_TOKEN"),
         "Basic eC1hY2Nlc3MtdG9rZW46YmJ0b2s="),
    ]  # fmt: skip
    for host, header in cases:
        env = git_env(host)
        base = host.url.rstrip("/")

        def urlmatch(url: str, env=env) -> str:
            res = subprocess.run(["git", "config", "--get-urlmatch", "http.extraHeader", url],
                                 env={**os.environ, **env},
                                 capture_output=True, text=True, cwd=tmp_path)  # fmt: skip
            return res.stdout.strip()

        assert urlmatch(f"{base}/scm/aosp/frameworks_base.git") == f"Authorization: {header}"
        assert urlmatch("https://elsewhere.com/x.git") == ""
    assert git_env(RepoHost(kind="gitlab", url="https://gl")) is None
    with pytest.raises(KeyError):
        git_env(RepoHost(kind="gitlab", url="https://gl", token="env:NOT_SET_ANYWHERE"))


def test_copilot_sign_in_by_token_and_host(monkeypatch, tmp_path: Path, fake_copilot):  # noqa: F811
    monkeypatch.setenv("COPILOT_PAT", TOKEN)
    assert copilot_env(LlmConfig()) == {}
    assert copilot_env(LlmConfig(github_token="env:COPILOT_PAT")) == {"COPILOT_GITHUB_TOKEN": TOKEN}
    assert copilot_env(LlmConfig(github_host="oem.ghe.com", github_token="env:COPILOT_PAT")) == {
        "GH_HOST": "oem.ghe.com", "COPILOT_GITHUB_TOKEN": TOKEN}  # fmt: skip
    ghes = copilot_env(LlmConfig(github_host="git.oem.com", github_token="env:COPILOT_PAT"))
    assert ghes["COPILOT_PROVIDER_GHES_HOST"] == "git.oem.com" and ghes["COPILOT_OFFLINE"] == "true"
    assert ghes["COPILOT_PROVIDER_GHES_TOKEN"] == TOKEN

    exe, calls = fake_copilot
    cfg = LlmConfig(executable=str(exe), github_host="oem.ghe.com", github_token="env:COPILOT_PAT")
    CopilotCliAdapter(cfg, tmp_path).run(_task(tmp_path), 1)
    call = calls()[-1]
    assert call["token"] == TOKEN and call["gh_host"] == "oem.ghe.com"
    assert TOKEN not in json.dumps(call["argv"])  # env only, never the command line


def test_checks_without_network(monkeypatch, make_config, fake_copilot):  # noqa: F811
    exe, _ = fake_copilot
    monkeypatch.setenv("CLASSIC", "ghp_classic")
    hosts = [{"kind": "gitlab", "url": "https://gl.oem.com"},
             {"kind": "github", "url": "https://ghe", "token": "env:NOPE"}]  # fmt: skip
    llm = {"executable": str(exe), "github_token": "env:CLASSIC"}
    path = make_config([{"type": "git_range"}], llm=llm, code_hosts=hosts)
    cfg = load_config(path)
    copilot = check_copilot(cfg)
    assert copilot[0] == ("Copilot CLI", True, "fake-copilot 1.0")
    assert not copilot[1][1] and "classic PATs" in copilot[1][2]
    hosts = check_hosts(cfg)
    assert hosts[0][1] and "public" in hosts[0][2]
    assert not hosts[1][1] and "env reference not available" in hosts[1][2]
    assert all(ok for _, ok, _ in check_trees(cfg))

    res = CliRunner().invoke(cli, ["check", "trees", "copilot", "-c", str(path)])
    assert res.exit_code == 1 and "ok   source tree" in res.output and "FAIL token" in res.output
    assert "ghp_classic" not in res.output


def test_tree_hosts_count_as_code_hosts(write_config):
    host = {"kind": "bitbucket_server", "url": "https://bb.oem.com", "token": "env:X"}
    trees = {**MINIMAL_CONFIG["trees"],
             "source": {**MINIMAL_CONFIG["trees"]["source"], "host": host},
             "target": {**MINIMAL_CONFIG["trees"]["target"], "host": host}}  # fmt: skip
    cfg = load_config(write_config({"trees": trees, "code_hosts": [
        {"kind": "github", "url": "https://github.com"}]}))  # fmt: skip
    assert [h.url for h in cfg.hosts()] == ["https://github.com", "https://bb.oem.com"]
    assert yaml.safe_load(Path(write_config({"trees": trees})).read_text())["trees"]["source"][
        "host"]["kind"] == "bitbucket_server"  # fmt: skip
