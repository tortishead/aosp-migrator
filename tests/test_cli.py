from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from patchmig.cli import app

runner = CliRunner()


def test_init_creates_valid_project(tmp_path: Path):
    result = runner.invoke(app, ["init", str(tmp_path / "proj")])
    assert result.exit_code == 0, result.output
    config = tmp_path / "proj" / "project.yaml"
    result = runner.invoke(app, ["config", "validate", "-c", str(config)])
    assert result.exit_code == 0, result.output
    assert "is valid" in result.output


def test_init_refuses_to_overwrite(tmp_path: Path):
    (tmp_path / "project.yaml").write_text("keep me")
    result = runner.invoke(app, ["init", str(tmp_path)])
    assert result.exit_code == 1
    assert (tmp_path / "project.yaml").read_text() == "keep me"


def test_validate_reports_errors(write_config):
    path = write_config({"thresholds": {"quick_approve": 2}})
    result = runner.invoke(app, ["config", "validate", "-c", str(path)])
    assert result.exit_code == 1
    assert "thresholds.quick_approve" in result.output


def test_run_new_and_status(write_config):
    path = write_config()
    result = runner.invoke(app, ["status", "-c", str(path)])
    assert "No runs yet" in result.output
    result = runner.invoke(app, ["run", "new", "-c", str(path), "--note", "first"])
    assert result.exit_code == 0, result.output
    assert "Created run 1" in result.output
    result = runner.invoke(app, ["status", "-c", str(path)])
    assert result.exit_code == 0, result.output
    assert "Run 1" in result.output
    assert "total" in result.output


def test_config_show_prints_resolved_yaml(write_config, tmp_path: Path):
    result = runner.invoke(app, ["config", "show", "-c", str(write_config())])
    assert result.exit_code == 0, result.output
    assert "label: mig-A14-A15" in result.output
    assert str(tmp_path / "src") in result.output
