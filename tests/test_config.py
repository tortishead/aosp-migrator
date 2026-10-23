from __future__ import annotations

from importlib.resources import files
from pathlib import Path

import pytest

from patchmig.config import ConfigError, config_hash, dump_config, load_config
from patchmig.config.schema import resolve_secret


def test_minimal_config_gets_defaults(write_config):
    cfg = load_config(write_config())
    assert cfg.migration.label == "mig-A14-A15"
    assert cfg.llm.runtime == "copilot-cli"
    assert cfg.thresholds.quick_approve == 0.85
    assert cfg.output is None


def test_relative_paths_resolve_against_config_folder(write_config, tmp_path: Path):
    cfg = load_config(write_config())
    assert cfg.trees.source.path == tmp_path / "src"
    assert cfg.ledger_path == tmp_path / ".patchmig" / "ledger.db"


def test_example_template_is_valid(tmp_path: Path):
    path = tmp_path / "project.yaml"
    path.write_text(files("patchmig.templates").joinpath("project.example.yaml").read_text())
    cfg = load_config(path)
    assert cfg.output.target == "github"
    assert cfg.build.parallelism == 1


def test_literal_secret_is_rejected(write_config):
    jira = {"url": "https://jira", "project": "OEM", "token": "hunter2"}
    with pytest.raises(ConfigError) as err:
        load_config(write_config({"jira": jira}))
    assert any("jira.token" in p and "references" in p for p in err.value.problems)


def test_all_problems_reported_at_once(write_config):
    with pytest.raises(ConfigError) as err:
        load_config(
            write_config(
                {
                    "thresholds": {"quick_approve": 0.4, "careful_review": 0.6},
                    "llm": {"models": {"planner": "x"}},
                    "unknown_key": 1,
                }
            )
        )
    text = "\n".join(err.value.problems)
    assert "careful_review must not be above quick_approve" in text
    assert "unknown agents ['planner']" in text
    assert "unknown_key" in text


def test_jira_source_requires_jira_section(write_config):
    with pytest.raises(ConfigError, match="needs the 'jira' section"):
        load_config(write_config({"patch_sources": [{"type": "jira_query", "jql": "x"}]}))


def test_output_target_must_be_configured(write_config):
    with pytest.raises(ConfigError, match=r"output\.github is missing"):
        load_config(write_config({"output": {"target": "github"}}))


def test_module_build_cmd_needs_placeholder(write_config):
    build = {
        "ssh": {"host": "b", "user": "u", "workdir": "/w"},
        "lunch_target": "t",
        "module_build_cmd": "m everything",
    }
    with pytest.raises(ConfigError, match="placeholder"):
        load_config(write_config({"build": build}))


def test_source_tree_needs_head_ref(write_config):
    trees = {
        "source": {"path": "src", "base_ref": "a"},
        "target": {"path": "dst", "base_ref": "b"},
    }
    with pytest.raises(ConfigError, match="head_ref is required"):
        load_config(write_config({"trees": trees}))


def test_bad_yaml_and_missing_file(write_config, tmp_path: Path):
    with pytest.raises(ConfigError, match="not valid YAML"):
        load_config(write_config(raw="name: [unclosed"))
    with pytest.raises(ConfigError, match="cannot read file"):
        load_config(tmp_path / "nope.yaml")


def test_hash_is_stable_and_changes_with_content(write_config):
    a = load_config(write_config())
    b = load_config(write_config())
    assert config_hash(a) == config_hash(b)
    c = load_config(write_config({"budget": {"max_fix_attempts": 5}}))
    assert config_hash(a) != config_hash(c)


def test_dump_round_trips(write_config, tmp_path: Path):
    cfg = load_config(write_config())
    again = tmp_path / "again.yaml"
    again.write_text(dump_config(cfg))
    assert config_hash(load_config(again)) == config_hash(cfg)


def test_resolve_secret(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("PATCHMIG_TEST_TOKEN", "abc")
    assert resolve_secret("env:PATCHMIG_TEST_TOKEN") == "abc"
    key = tmp_path / "key"
    key.write_text("secret\n")
    assert resolve_secret(f"file:{key}") == "secret"
