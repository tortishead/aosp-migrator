"""Shallow checkouts (repo sync --depth=1): the upstream checks need history, which is fetched."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml
from sqlalchemy import select
from typer.testing import CliRunner

from patchmig.analysis import analyse
from patchmig.cli import app
from patchmig.config import load_config
from patchmig.ingest import ingest
from patchmig.knowledge import history
from patchmig.ledger import LedgerStore, Patch
from tests.fixtures.scenario import (
    CAR,
    EXPECTED,
    FRAMEWORKS,
    SOURCE_BASE_TAG,
    SOURCE_HEAD_TAG,
    TARGET_BASE_TAG,
    build_scenario,
)

STATES = {e.key: e.verdict for e in EXPECTED}


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True,
                          text=True).stdout.strip()  # fmt: skip


def shallow_copy(tree: Path, dest: Path) -> None:
    """What `repo sync --depth=1` leaves: one commit per project, only the tip's tags."""
    for project in (FRAMEWORKS, CAR):
        subprocess.run(["git", "clone", "-q", "--depth", "1", f"file://{tree / project}",
                        str(dest / project)], check=True)  # fmt: skip
    shutil.copytree(tree / ".repo", dest / ".repo")


class Shallow:
    def __init__(self, root: Path):
        self.scenario = build_scenario(root / "full")
        self.source, self.target = root / "shallow" / "source", root / "shallow" / "target"
        shallow_copy(self.scenario.source, self.source)
        shallow_copy(self.scenario.target, self.target)
        self.config_path = root / "project.yaml"
        self.config_path.write_text(yaml.safe_dump({
            "name": "shallow-test",
            "workdir": str(root / "work"),
            "migration": {"source_version": "14", "target_version": "15"},
            "trees": {
                "source": {"path": str(self.source), "manifest": ".repo/manifests/default.xml",
                           "base_ref": SOURCE_BASE_TAG, "head_ref": SOURCE_HEAD_TAG},
                "target": {"path": str(self.target), "base_ref": TARGET_BASE_TAG},
                "history_commits": 100,
            },
            "patch_sources": [{"type": "git_range"}],
        }))  # fmt: skip
        self.config = load_config(self.config_path)
        self.store = LedgerStore.open(self.config.ledger_path)

    def run(self):
        run = self.store.create_run(self.config)
        ingest_report = ingest(self.config, self.store, run)
        report = analyse(self.config, self.store, run) if ingest_report.total else None
        with self.store.session() as s:
            rows = s.scalars(select(Patch).where(Patch.run_id == run.id)).all()
        by_sha = {sha: key for key, sha in self.scenario.commits.items()}
        self.rungs = {by_sha[p.commit_sha]: (p.verdict or {}).get("rung") for p in rows}
        return ingest_report, report, {by_sha[p.commit_sha]: p.state.value for p in rows}


@pytest.fixture
def shallow(tmp_path, monkeypatch) -> Shallow:
    monkeypatch.setattr(history, "FIRST_STEP", 1)  # several deepening rounds, even here
    return Shallow(tmp_path)


def _decided(states: dict[str, str]) -> dict[str, str]:
    return {k: "dropped-by-revert" if v == "reverted" else v for k, v in states.items()}


def test_shallow_checkouts_are_detected(shallow):
    assert history.is_shallow(shallow.source / FRAMEWORKS)
    assert _git(shallow.source / FRAMEWORKS, "rev-list", "--count", "HEAD") == "1"
    ingest_report, _, states = shallow.run()
    # Without the OEM range, git_range would list just the tip commit; it refuses instead.
    assert states == {}
    assert any("shallow checkout without the OEM range" in w for w in ingest_report.warnings)


def test_history_deepens_just_enough_and_analysis_matches_full_trees(shallow):
    cli = CliRunner()
    res = cli.invoke(app, ["history", "-c", str(shallow.config_path)])
    assert res.exit_code == 0, res.output
    assert "source frameworks/base" in res.output and "OEM range complete" in res.output
    assert "Run again after ingest" in res.output

    _, report, states = shallow.run()
    assert set(states) == set(STATES)  # the whole OEM range is there now
    # The target is still shallow: the checks would miss Google's own fixes, and say so.
    assert any("target checkout is shallow" in w for w in report.warnings)
    # Here the textual rung still catches P7, but the conclusive evidence is gone: its
    # patch-id match is in history the shallow target does not have. A Google fix that was
    # changed again later has no textual match, and would look needed.
    assert shallow.rungs["P7"] == "textual"

    res = cli.invoke(app, ["history", "-c", str(shallow.config_path)])
    assert res.exit_code == 0, res.output
    assert "target frameworks/base" in res.output
    assert "history back to the source base" in res.output
    target = shallow.target / FRAMEWORKS
    source_base = _git(shallow.scenario.source / FRAMEWORKS, "rev-parse", SOURCE_BASE_TAG)
    assert _git(target, "merge-base", "--is-ancestor", source_base, TARGET_BASE_TAG) == ""

    _, report, states = shallow.run()
    assert _decided(states) == STATES
    assert shallow.rungs["P7"] == "identity"
    assert not any("shallow" in w for w in report.warnings)
    res = cli.invoke(app, ["history", "-c", str(shallow.config_path)])
    assert "ok" in res.output or "No shallow project" in res.output


def test_without_shared_history_the_target_gets_history_commits(shallow, tmp_path):
    target = shallow.target / CAR
    r = history.deepen_target(target, CAR, TARGET_BASE_TAG, "0" * 40, None, max_commits=3)
    assert r.ok and r.commits_before == 1 and r.commits_after == 3
    assert "no shared history; 3 commits to search" in r.detail
    dry = history.deepen_target(shallow.target / FRAMEWORKS, FRAMEWORKS, TARGET_BASE_TAG,
                                None, None, max_commits=3, dry_run=True)  # fmt: skip
    assert not dry.ok and dry.commits_after == 1 and dry.fetches == 0


def test_remote_must_be_unambiguous(shallow):
    repo = shallow.target / FRAMEWORKS
    _git(repo, "remote", "add", "oem", "file:///nowhere")
    r = history.deepen_target(repo, FRAMEWORKS, TARGET_BASE_TAG, None, None, 100)
    assert not r.ok and "set trees.<side>.remote" in r.detail
    r = history.deepen_target(repo, FRAMEWORKS, TARGET_BASE_TAG, None, "origin", 100)
    assert r.ok
