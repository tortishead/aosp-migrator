from __future__ import annotations

import shlex
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from patchmig.cli import app
from patchmig.config.schema import BuildConfig, SshConfig
from patchmig.config.schema import TestsConfig as SelectionCfg
from patchmig.ledger import State
from patchmig.migrate.workspace import branch_name
from patchmig.verify import check_host, error_excerpt, verify
from patchmig.verify.host import SshHost
from patchmig.verify.selection import bp_modules, select
from tests.fixtures.buildtree import make_build_tree
from tests.fixtures.scenario import CAR, FRAMEWORKS, POWER, POWER_TEST_MAPPING, TARGET_BASE_TAG
from tests.test_migrate import CONFLICT, RESOLVED, Project, resolve_power

NAIVE = RESOLVED  # keeps the OEM overload's old one-argument call: does not compile


def naive_resolve(task, attempt) -> dict:
    """Resolves the conflict text but misses the call site: a build break."""
    path = task.workdir / FRAMEWORKS / POWER
    path.write_text(path.read_text().replace(CONFLICT, NAIVE))
    return {"outcome": "resolved", "summary": "merged", "confidence": 0.9}


def fix_call(task, attempt) -> dict:
    assert "method setMode cannot be applied to given types" in task.brief
    path = task.workdir / FRAMEWORKS / POWER
    path.write_text(path.read_text().replace("setMode(mode);", "setMode(mode, 0);"))
    return {"outcome": "fixed", "summary": "pass a reason", "confidence": 0.7,
            "files": [f"{FRAMEWORKS}/{POWER}"]}  # fmt: skip


class Built(Project):
    def __init__(self, root: Path, slots: int = 1, **extra):
        super().__init__(root, **extra)
        self.slots = make_build_tree(self.scenario, root / "buildhost", slots)
        self.config = self.config.model_copy(
            update={
                "build": BuildConfig(
                    transport="local", slots=self.slots, lunch_target="fake-userdebug"
                )
            }
        )
        self.config_path.write_text(
            yaml.safe_dump(self.config.model_dump(mode="json", exclude_none=True))
        )

    def verify(self, runner=None, **kw):
        return verify(self.config, self.store, self.run, runner, **kw)

    def slot_file(self, name: str, slot: int = 0) -> Path:
        return Path(self.slots[slot]) / "build" / name

    def slot_head(self, project: str, slot: int = 0) -> str:
        import subprocess

        return subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=Path(self.slots[slot]) / project,
            capture_output=True, text=True, check=True,
        ).stdout.strip()  # fmt: skip


@pytest.fixture
def built(tmp_path: Path) -> Built:
    return Built(tmp_path)


# ---- selection --------------------------------------------------------------------------


def test_android_bp_modules_and_test_mapping(built):
    assert bp_modules('package {\n    x: 1,\n}\n\ncc_library {\n    name: "libfoo",\n}\n') == [
        "libfoo"
    ]
    repo = built.scenario.target / FRAMEWORKS
    sel = select(repo, TARGET_BASE_TAG, FRAMEWORKS, [POWER], built.config.build, SelectionCfg())
    assert sel.modules == ["framework", "framework-res-values"]  # nearest Android.bp
    assert sel.tests == ["FrameworksCoreTests"]  # presubmit only, comments allowed
    build = built.config.build.model_copy(update={"module_map": {f"{FRAMEWORKS}/core/**": ["fw"]}})
    sel = select(repo, TARGET_BASE_TAG, FRAMEWORKS, [POWER, "README"], build, SelectionCfg())
    assert sel.modules == ["fw", "framework", "framework-res-values"]
    assert POWER_TEST_MAPPING.endswith("TEST_MAPPING")


# ---- verify -----------------------------------------------------------------------------


def test_cherry_picked_cluster_builds_only_its_module(built):
    built.migrate()
    bt = built.cluster_of("P3")
    report = built.verify(cluster_ids=[bt.id])
    assert [o.state for o in report.outcomes] == ["verified"]
    bt = built.cluster_of("P3")
    assert bt.state == State.VERIFIED and built.patch("P3").state == State.VERIFIED
    v = bt.result["verification"]
    assert v["selection"]["modules"] == ["services.core"]
    assert built.slot_file("m.log").read_text() == "services.core\n"  # no full build
    assert v["builds"][0]["ok"] and v["reason"] == "built; no tests selected"
    # The slot is back on the target base afterwards.
    base = built.target_git(FRAMEWORKS, "rev-parse", TARGET_BASE_TAG).strip()
    assert built.slot_head(FRAMEWORKS) == base


def test_build_break_is_fixed_by_the_agent_and_tested(built):
    built.migrate(built.runner(naive_resolve))
    power = built.cluster_of("P1")
    assert power.state == State.RESOLVED
    old_head = power.result["migration"]["projects"][FRAMEWORKS]["head"]

    runner = built.runner(fix_call)
    built.verify(runner)
    power = built.cluster_of("P1")
    assert power.state == State.VERIFIED
    assert power.confidence == 0.7  # the fix's confidence caps the cluster's
    v = power.result["verification"]
    assert [b["ok"] for b in v["builds"]] == [False, True]
    assert "cannot be applied" in v["builds"][0]["log_tail"]
    assert v["fixes"][0]["answer"]["outcome"] == "fixed"
    assert v["tests"]["ok"] and set(v["tests"]["tests"]) == {"FrameworksCoreTests",
                                                            "CarServiceUnitTest"}  # fmt: skip
    # The fix is a new commit on the cluster branch, and the record follows it.
    m = power.result["migration"]
    new_head = m["projects"][FRAMEWORKS]["head"]
    assert new_head != old_head
    branch = branch_name(built.config.name, built.run.id, power.id)
    assert built.target_git(FRAMEWORKS, "rev-parse", branch).strip() == new_head
    assert "setMode(mode, 0);" in m["diff"]
    assert [t.agent for t, _ in built.fake.calls] == ["build_fixer"]
    assert built.fake.calls[0][0].key.endswith(":fix:1")


def test_cluster_credit_cap_parks_only_that_cluster(built):
    from patchmig.agents.runner import AgentRunner
    from patchmig.config.schema import BudgetConfig

    built.migrate(built.runner(naive_resolve))  # agent + reviewer: 2 credits on the cluster
    built.config = built.config.model_copy(update={"budget": BudgetConfig(cluster_credits=2.5)})
    runner = AgentRunner(built.runner(fix_call).adapter, built.store, built.run,
                         built.config.llm, built.config.budget)  # fmt: skip
    report = built.verify(runner)
    assert report.stopped_by_budget is None
    power = built.cluster_of("P1")
    assert power.state == State.NEEDS_DECISION
    assert "cluster_credits" in power.result["verification"]["reason"]
    assert built.cluster_of("P3").state == State.VERIFIED  # the other cluster went on


def test_build_break_without_agent_fails(built):
    built.migrate(built.runner(naive_resolve))
    built.verify(None)
    power = built.cluster_of("P1")
    assert power.state == State.FAILED
    assert "build fails (no agent)" in built.store.history(power)[-1].reason
    assert built.patch("P8").state == State.FAILED


def test_fixer_blaming_the_target_is_checked(built):
    built.migrate(built.runner(naive_resolve))

    def blame_then_fix(task, attempt):
        if "Earlier fix attempts" not in task.brief:
            return {"outcome": "not_caused_by_cluster", "summary": "target is broken",
                    "confidence": 0.5}  # fmt: skip
        assert "the target builds" in task.brief
        return fix_call(task, attempt)

    built.verify(built.runner(blame_then_fix))
    power = built.cluster_of("P1")
    assert power.state == State.VERIFIED
    fixes = power.result["verification"]["fixes"]
    assert fixes[0]["base_build"]["ok"] and fixes[1]["answer"]["outcome"] == "fixed"


def test_test_regressions_fail_and_existing_failures_do_not(built, tmp_path):
    built.slot_file("FAIL_IF").write_text("CarServiceUnitTest\tisOemMode\n")
    built.migrate(built.runner(resolve_power))
    built.verify()
    power = built.cluster_of("P1")
    assert power.state == State.FAILED
    tests = power.result["verification"]["tests"]
    assert not tests["ok"] and tests["baseline"]["ok"]

    other = Built(tmp_path / "other")
    other.slot_file("FAIL_ALWAYS").write_text("CarServiceUnitTest\n")
    other.migrate(other.runner(resolve_power))
    other.verify()
    power = other.cluster_of("P1")
    assert power.state == State.VERIFIED
    assert "not caused by the cluster" in power.result["verification"]["reason"]
    assert power.confidence < other.config.thresholds.quick_approve


def test_clusters_build_in_parallel_slots(tmp_path):
    built = Built(tmp_path, slots=2)
    built.migrate(built.runner(resolve_power))
    report = built.verify()
    assert sorted(o.state for o in report.outcomes) == ["verified", "verified"]
    used = {built.cluster_of(k).result["verification"]["slot"] for k in ("P1", "P3")}
    assert used <= set(built.slots)
    for slot in range(2):
        for project in (FRAMEWORKS, CAR):
            base = built.target_git(project, "rev-parse", TARGET_BASE_TAG).strip()
            assert built.slot_head(project, slot) == base


# ---- host -------------------------------------------------------------------------------


def test_check_host_and_cli(built):
    assert all(ok for _, ok, _ in check_host(built.config))
    (Path(built.slots[0]) / "build" / "envsetup.sh").unlink()
    names = [name for name, ok, _ in check_host(built.config) if not ok]
    assert names == [f"{built.slots[0]}: build/envsetup.sh"]
    result = CliRunner().invoke(app, ["build-check", "--config", str(built.config_path)])
    assert result.exit_code == 1 and "FAIL" in result.output


def test_cli_verify_without_fixes(tmp_path):
    built = Built(tmp_path)
    built.migrate()
    result = CliRunner().invoke(app, ["verify", "--config", str(built.config_path), "--no-fix"])
    assert result.exit_code == 0, result.output
    assert "verified" in result.output and "built; no tests selected" in result.output


def test_ssh_command_uses_batch_mode_and_a_key_file(tmp_path, monkeypatch):
    key = tmp_path / "id_build"
    key.write_text("PRIVATE KEY MATERIAL")
    monkeypatch.setenv("BUILD_KEY", str(key))
    host = SshHost(SshConfig(host="b1", user="ci", port=2222, key="env:BUILD_KEY", workdir="/t"))
    cmd = host.command("cd /t && m 'a b'")
    assert cmd[:2] == ["ssh", "-o"] and "BatchMode=yes" in cmd
    assert cmd[cmd.index("-p") + 1] == "2222" and cmd[cmd.index("-i") + 1] == str(key)
    assert cmd[-2] == "ci@b1" and shlex.split(cmd[-1]) == ["bash", "-lc", "cd /t && m 'a b'"]
    assert not any("PRIVATE" in part for part in cmd)
    slots = BuildConfig(ssh=host.ssh, lunch_target="x", parallelism=3).tree_slots()
    assert slots == ["/t-1", "/t-2", "/t-3"]


def test_error_excerpt_keeps_compiler_errors():
    log = "\n".join([f"line {i}" for i in range(500)] + ["Foo.java:3: error: boom", "  ^", ""])
    out = error_excerpt(log)
    assert "Foo.java:3: error: boom" in out and "line 10\n" not in out
    assert error_excerpt("a\nb") == "a\nb"


def test_web_cluster_view_shows_the_verification(built):
    from patchmig.web import data

    built.migrate()
    bt = built.cluster_of("P3")
    built.verify(cluster_ids=[bt.id])
    v = data.cluster(built.store, built.run.id, bt.id)["verification"]
    assert v["state"] == "verified" and v["builds"][0]["command"] == "m services.core"
