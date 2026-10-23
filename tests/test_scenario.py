"""Checks that the synthetic trees really contain the situations the scenarios promise.

Later milestones rely on these properties, so they are asserted with plain git here.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from tests.fixtures.scenario import (
    CAR,
    EXPECTED,
    FRAMEWORKS,
    PROJECTS,
    SOURCE_BASE_TAG,
    SOURCE_HEAD_TAG,
    TARGET_BASE_TAG,
    WATCHDOG,
    Scenario,
)


def git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        check=check,
    )


def patch_id(repo: Path, rev: str) -> str:
    show = git(repo, "show", rev).stdout
    out = subprocess.run(
        ["git", "patch-id", "--stable"], cwd=repo, input=show, capture_output=True, text=True
    )
    return out.stdout.split()[0]


@pytest.fixture
def target_with_source(scenario: Scenario, tmp_path: Path):
    """Clone a target project and fetch the source project's OEM commits into it."""

    def _clone(project: str) -> Path:
        clone = tmp_path / project.replace("/", "_")
        git(tmp_path, "clone", "-q", str(scenario.target / project), str(clone))
        git(clone, "fetch", "-q", str(scenario.source / project), f"refs/tags/{SOURCE_HEAD_TAG}")
        return clone

    return _clone


def test_every_expected_patch_was_committed(scenario: Scenario):
    assert sorted(scenario.commits) == sorted(e.key for e in EXPECTED)
    for e in EXPECTED:
        repo = scenario.source / e.project
        assert git(repo, "cat-file", "-t", scenario.commits[e.key]).stdout.strip() == "commit"


def test_trees_share_identical_base_commits(scenario: Scenario):
    for project in PROJECTS:
        src = git(scenario.source / project, "rev-parse", SOURCE_BASE_TAG).stdout
        dst = git(scenario.target / project, "rev-parse", SOURCE_BASE_TAG).stdout
        assert src == dst


def test_build_is_reproducible(scenario: Scenario, tmp_path: Path):
    from tests.fixtures.scenario import build_scenario

    again = build_scenario(tmp_path / "again")
    assert again.commits == scenario.commits


def test_manifests_list_projects(scenario: Scenario):
    for tree, rev in ((scenario.source, SOURCE_HEAD_TAG), (scenario.target, TARGET_BASE_TAG)):
        text = (tree / ".repo/manifests/default.xml").read_text()
        assert f'revision="{rev}"' in text
        assert all(f'path="{p}"' in text for p in PROJECTS)


def test_p7_is_already_upstream_by_patch_id(scenario: Scenario):
    oem = patch_id(scenario.source / FRAMEWORKS, scenario.commits["P7"])
    upstream = git(
        scenario.target / FRAMEWORKS, "log", "--format=%H", "--grep=Enable auto brightness"
    ).stdout.split()
    assert len(upstream) == 1
    assert patch_id(scenario.target / FRAMEWORKS, upstream[0]) == oem


def test_p1_conflicts_with_upstream_signature_change(scenario: Scenario, target_with_source):
    repo = target_with_source(FRAMEWORKS)
    result = git(repo, "cherry-pick", scenario.commits["P1"], check=False)
    assert result.returncode != 0
    assert "CONFLICT" in result.stdout + result.stderr


def test_p3_follows_the_upstream_file_move(scenario: Scenario, target_with_source):
    repo = target_with_source(FRAMEWORKS)
    git(repo, "cherry-pick", scenario.commits["P3"])
    moved = repo / "services/core/java/com/android/server/bluetooth/BtManager.java"
    assert "RECONNECT_TIMEOUT_MS = 12000" in moved.read_text()


def test_p4_touches_a_file_deleted_upstream(scenario: Scenario, target_with_source):
    repo = target_with_source(CAR)
    result = git(repo, "cherry-pick", scenario.commits["P4"], check=False)
    assert result.returncode != 0
    assert "deleted" in (result.stdout + result.stderr).lower()


def test_p6_reverts_p5(scenario: Scenario):
    repo = scenario.source / CAR
    message = git(repo, "log", "-1", "--format=%B", scenario.commits["P6"]).stdout
    assert f"This reverts commit {scenario.commits['P5']}" in message
    diff = git(repo, "diff", f"{scenario.commits['P5']}~1", scenario.commits["P6"]).stdout
    assert diff == ""


def test_p8_depends_on_symbol_from_p1(scenario: Scenario):
    p1 = git(scenario.source / FRAMEWORKS, "show", scenario.commits["P1"]).stdout
    p8 = git(scenario.source / CAR, "show", scenario.commits["P8"]).stdout
    assert "+    public boolean isOemMode()" in p1
    assert "isOemMode()" in p8


def test_p9_line_is_upstream_but_patch_id_differs(scenario: Scenario):
    fw = scenario.target / FRAMEWORKS
    config = git(fw, "show", f"{TARGET_BASE_TAG}:core/res/res/values/config.xml").stdout
    assert '<integer name="config_screenBrightnessSettingDefault">128</integer>' in config
    oem = patch_id(scenario.source / FRAMEWORKS, scenario.commits["P9"])
    upstream = git(fw, "rev-list", f"{SOURCE_BASE_TAG}..{TARGET_BASE_TAG}").stdout.split()
    assert oem not in {patch_id(fw, sha) for sha in upstream}


def test_p10_is_a_backport_of_a_target_commit(scenario: Scenario):
    message = git(scenario.source / CAR, "log", "-1", "--format=%B", scenario.commits["P10"])
    original = message.stdout.split("cherry picked from commit ")[1][:40]
    car = scenario.target / CAR
    assert git(car, "merge-base", "--is-ancestor", original, TARGET_BASE_TAG).returncode == 0
    assert patch_id(car, original) != patch_id(scenario.source / CAR, scenario.commits["P10"])
    assert git(car, "show", "--name-only", "--format=", original).stdout.strip() == WATCHDOG


def test_trees_carry_build_metadata_for_module_and_test_selection(scenario):
    from tests.fixtures.scenario import CAR_BP, CAR_TEST_MAPPING, FW_BP, POWER_TEST_MAPPING

    for tree in (scenario.source, scenario.target):
        assert 'name: "framework"' in (tree / FRAMEWORKS / FW_BP).read_text()
        assert "presubmit" in (tree / FRAMEWORKS / POWER_TEST_MAPPING).read_text()
        assert 'name: "CarService"' in (tree / CAR / CAR_BP).read_text()
        assert "CarServiceUnitTest" in (tree / CAR / CAR_TEST_MAPPING).read_text()
