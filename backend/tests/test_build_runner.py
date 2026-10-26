import threading
import time
from collections.abc import Callable
from pathlib import Path

import pytest
from fake_jira import FakeJiraServer, FakeJiraStore
from fake_jira.build import fail_settings_build, write_envsetup
from fake_jira.prescan import prescan_store
from fastapi import FastAPI
from fastapi.testclient import TestClient
from repo_tree import RepoTree
from sqlalchemy import select
from test_import import session
from test_prescan_api import app, client, run_prescan, target, workspace_id  # noqa: F401
from test_tree import snapshot

from aosp_migrator.app import create_app
from aosp_migrator.build import tree as target_tree
from aosp_migrator.build.runner import BuildError, BuildJobConflict, BuildRunner
from aosp_migrator.config import Config
from aosp_migrator.db.models import BuildJob, BuildRun, HistoryEvent, Item, JiraIssue, MigrationPatch, Workspace
from aosp_migrator.prescan.runner import current_diffs, current_patch, diff_scan

LUNCH = "aosp_x86_64-eng"


@pytest.fixture
def fake_jira(fake_jira_server: FakeJiraServer, target: Path) -> FakeJiraStore:
    """The pre-scan scenario; PROJ-102's new resource file carries the failure marker."""
    store = prescan_store(target)
    fail_settings_build(store)
    fake_jira_server.store = store
    return store


@pytest.fixture
def workspace(app: FastAPI, client: TestClient, workspace_id: int, target: Path) -> int:
    """Imported, pre-scanned, with the fake envsetup and a lunch target."""
    write_envsetup(target)
    with session(app) as s:
        ws = s.get(Workspace, workspace_id)
        assert ws is not None
        ws.lunch_target = LUNCH
        s.commit()
    assert run_prescan(client, workspace_id)["status"] == "done"
    return workspace_id


def runner(app: FastAPI, config: Config, executor: Callable[[Callable[[], None]], None] = lambda run: run()) -> BuildRunner:
    return BuildRunner(app.state.sessionmaker, config.builds_dir, lambda: [], executor=executor, kill_grace=1)


def job_of(app: FastAPI, job_id: int) -> BuildJob:
    with session(app) as s:
        job = s.get(BuildJob, job_id)
        assert job is not None
        return job


def runs(app: FastAPI, job_id: int) -> list[BuildRun]:
    with session(app) as s:
        return list(s.scalars(select(BuildRun).where(BuildRun.job_id == job_id).order_by(BuildRun.id)))


def item(app: FastAPI, key: str) -> Item:
    with session(app) as s:
        found = s.scalar(select(Item).join(JiraIssue, Item.issue_id == JiraIssue.id).where(JiraIssue.key == key))
        assert found is not None
        return found


def events(app: FastAPI, key: str) -> list[tuple[str, dict]]:
    with session(app) as s:
        rows = s.scalars(select(HistoryEvent).where(HistoryEvent.item_id == item(app, key).id, HistoryEvent.type.like("%build%") | (HistoryEvent.type == "tree_restored")).order_by(HistoryEvent.id))
        return [(e.type, e.payload) for e in rows]


def log_of(run: BuildRun) -> str:
    assert run.log_path is not None
    return Path(run.log_path).read_text()


def test_success_failure_and_nothing_to_build(app: FastAPI, config: Config, workspace: int, target: Path) -> None:
    before = snapshot(target)
    job = runner(app, config).start(workspace, keys=["PROJ-105", "PROJ-102", "PROJ-101"])
    job = job_of(app, job.id)
    assert (job.status, job.scope, job.done, job.total, job.errors, job.skipped) == ("done", "keys", 3, 3, [], [])
    assert snapshot(target) == before

    core, settings, camera = runs(app, job.id)
    assert (core.status, core.tool, core.modules, core.summary, core.inflight) == ("build_successful", "mmm", ["system/core"], "", None)
    assert core.command == f"source {target}/build/envsetup.sh && lunch {LUNCH} && mmm system/core"
    assert "lunch aosp_x86_64-eng\nmmm system/core\n#### build completed successfully" in log_of(core)
    assert Path(core.log_path or "").parent == config.builds_dir(workspace)
    assert (item(app, "PROJ-105").build_status, item(app, "PROJ-105").build_reason) == ("build_successful", "")

    assert (settings.status, settings.modules) == ("build_failed", ["packages/apps/Settings"])
    assert settings.summary == (
        "packages/apps/Settings/res/values/oem_strings.xml:3: error: FAIL_BUILD marker\n"
        "FAILED: out/target/common/obj/APPS/Settings_intermediates/package-res.apk"
    )
    assert (item(app, "PROJ-102").build_status, item(app, "PROJ-102").build_reason) == ("build_failed", settings.summary)

    # frameworks/av has no Android.bp in the fixture tree.
    assert (camera.status, camera.summary, camera.tool, camera.command) == ("build_successful", "nothing to build", None, None)
    assert "Not built (no module): frameworks/av/services/camera/libcameraservice/CameraService.cpp" in log_of(camera)

    with session(app) as s:
        patch = current_patch(s, diff_scan(s, current_diffs(s, item(app, "PROJ-105").issue_id)[0].id))
        assert patch is not None and core.patch_ids == [patch.id]
    assert events(app, "PROJ-105") == [
        ("build_started", {"run_id": core.id, "job_id": job.id}),
        ("build_finished", {"run_id": core.id, "status": "build_successful", "summary": ""}),
    ]


def test_ineligible_items_are_skipped(app: FastAPI, config: Config, workspace: int) -> None:
    job = job_of(app, runner(app, config).start(workspace, keys=["PROJ-103", "PROJ-105", "PROJ-111", "PROJ-110"]).id)
    assert job.issue_keys == ["PROJ-105"]
    assert job.skipped == [
        {"key": "PROJ-103", "reason": "pre-scan: present"},
        {"key": "PROJ-111", "reason": "not pre-scanned"},
        {"key": "PROJ-110", "reason": "pre-scan: unmapped"},
    ]
    assert (job.status, job.done, job.total) == ("done", 1, 1)
    assert item(app, "PROJ-103").build_status is None


def test_environment_is_checked_before_the_job(app: FastAPI, config: Config, workspace: int, target: Path) -> None:
    with pytest.raises(BuildError, match="Not in the backlog: PROJ-201"):
        runner(app, config).start(workspace, keys=["PROJ-201"])
    (target / "build" / "envsetup.sh").unlink()
    with pytest.raises(BuildError, match="envsetup.sh not found"):
        runner(app, config).start(workspace, keys=["PROJ-105"])
    with session(app) as s:
        ws = s.get(Workspace, workspace)
        assert ws is not None
        ws.lunch_target = None
        s.commit()
    with pytest.raises(BuildError, match="lunch target"):
        runner(app, config).start(workspace, keys=["PROJ-105"])


def test_dirty_project_is_refused_and_untouched(app: FastAPI, config: Config, workspace: int, target: Path) -> None:
    (target / "system/core/init/init.cpp").write_text("// my local edit\n")
    before = snapshot(target)
    job = job_of(app, runner(app, config).start(workspace, keys=["PROJ-105", "PROJ-107"]).id)
    assert snapshot(target) == before
    dirty, hal = runs(app, job.id)
    assert (dirty.status, dirty.summary, dirty.command) == ("apply_failed", "project dirty: system/core", None)
    assert (item(app, "PROJ-105").build_status, item(app, "PROJ-105").build_reason) == ("apply_failed", "project dirty: system/core")
    assert "project dirty: system/core" in log_of(dirty)
    # The next unit still builds.
    assert (hal.status, hal.modules) == ("build_successful", ["hardware/interfaces"])


def test_stale_item_is_rescanned_first(app: FastAPI, config: Config, workspace: int, target: Path) -> None:
    tree = RepoTree(target)
    tree.commit("system/core", {"init/README": "init\n"}, "Add README")
    moved_to_conflict = (target / "frameworks/av/services/camera/libcameraservice/CameraService.h").read_text()
    tree.commit("frameworks/av", {"services/camera/libcameraservice/CameraService.h": moved_to_conflict.replace("\n", "\n// local\n")}, "Local change")
    before = snapshot(target)
    job = job_of(app, runner(app, config).start(workspace, keys=["PROJ-105", "PROJ-101"]).id)
    assert snapshot(target) == before

    core, camera = runs(app, job.id)
    head = tree.git("system/core", "rev-parse", "HEAD").strip()
    with session(app) as s:
        patch = s.get(MigrationPatch, core.patch_ids[0])
        assert patch is not None and patch.base_head == head
    assert core.status == "build_successful"
    assert (camera.status, camera.patch_ids) == ("apply_failed", [])
    assert camera.summary is not None and camera.summary.startswith("PROJ-101: pre-scan: conflicts: frameworks/av:")
    assert item(app, "PROJ-101").triage_label == "conflicts"


def test_cluster_is_one_unit_in_order(app: FastAPI, client: TestClient, config: Config, workspace: int, target: Path) -> None:
    cluster = client.post(f"/api/workspaces/{workspace}/clusters", json={"keys": ["PROJ-106", "PROJ-105", "PROJ-103"], "name": "boot props"}).json()
    client.put(f"/api/workspaces/{workspace}/clusters/{cluster['id']}/order", json={"keys": ["PROJ-105", "PROJ-106", "PROJ-103"]})
    before = snapshot(target)
    job = job_of(app, runner(app, config).start(workspace, cluster_id=cluster["id"]).id)
    assert snapshot(target) == before
    assert (job.scope, job.issue_keys, job.skipped, job.total, job.done) == ("cluster", ["PROJ-105", "PROJ-106"], [{"key": "PROJ-103", "reason": "pre-scan: present"}], 1, 1)
    (run,) = runs(app, job.id)
    assert (run.status, run.cluster_id, run.modules, len(run.patch_ids)) == ("build_successful", cluster["id"], ["system/core"], 2)
    assert run.item_ids == [item(app, "PROJ-105").id, item(app, "PROJ-106").id]
    assert item(app, "PROJ-106").build_status == "build_successful"


def test_timeout_kills_the_build(app: FastAPI, config: Config, workspace: int, target: Path) -> None:
    (target / "SLOW_BUILD").write_text("SLOW_BUILD\n")
    with session(app) as s:
        ws = s.get(Workspace, workspace)
        assert ws is not None
        ws.build_timeout = 1
        s.commit()
    before = snapshot(target)
    started = time.monotonic()
    job = job_of(app, runner(app, config).start(workspace, keys=["PROJ-105"]).id)
    assert time.monotonic() - started < 20
    assert snapshot(target) == before
    (run,) = runs(app, job.id)
    assert run.status == "build_failed"
    assert run.summary is not None and run.summary.startswith("timed out after 1 s\n")
    assert "Timed out after 1 s" in log_of(run)


def wait_for(condition: Callable[[], bool]) -> None:
    deadline = time.monotonic() + 30
    while not condition():
        assert time.monotonic() < deadline, "timed out waiting"
        time.sleep(0.05)


def test_cancel_mid_build_restores_the_tree(app: FastAPI, config: Config, workspace: int, target: Path) -> None:
    (target / "SLOW_BUILD").write_text("SLOW_BUILD\n")
    before = snapshot(target)
    build = runner(app, config, executor=lambda run: threading.Thread(target=run).start())
    job_id = build.start(workspace, keys=["PROJ-102", "PROJ-105"]).id
    wait_for(lambda: bool(runs(app, job_id)) and "mmm packages/apps/Settings" in log_of(runs(app, job_id)[0]))
    # Patched while it builds.
    assert (target / "packages/apps/Settings/res/values/oem_strings.xml").is_file()

    with session(app) as s:
        job = s.get(BuildJob, job_id)
        assert job is not None
        job.cancel_requested = True
        s.commit()
    wait_for(lambda: job_of(app, job_id).status != "running")
    job = job_of(app, job_id)
    assert (job.status, job.done) == ("canceled", 0)
    assert snapshot(target) == before
    (run,) = runs(app, job_id)
    assert (run.status, run.inflight) == ("canceled", None)
    # The previous status stays.
    assert item(app, "PROJ-102").build_status is None
    assert events(app, "PROJ-102")[-1] == ("build_finished", {"run_id": run.id, "status": "canceled", "summary": "canceled"})


def test_one_job_per_workspace(app: FastAPI, config: Config, workspace: int) -> None:
    pending: list[Callable[[], None]] = []
    build = runner(app, config, executor=pending.append)
    build.start(workspace, keys=["PROJ-105"])
    with pytest.raises(BuildJobConflict):
        build.start(workspace, keys=["PROJ-101"])
    pending.pop()()
    build.start(workspace, keys=["PROJ-101"])


def test_restart_restores_a_unit_in_flight(app: FastAPI, client: TestClient, config: Config, workspace: int, target: Path) -> None:
    before = snapshot(target)
    pending: list[Callable[[], None]] = []
    job = runner(app, config, executor=pending.append).start(workspace, keys=["PROJ-102"])
    # As left by a crash mid-build: the patch applied, the in-flight record stored.
    with session(app) as s:
        settings = item(app, "PROJ-102")
        patch = current_patch(s, diff_scan(s, current_diffs(s, settings.issue_id)[0].id))
        assert patch is not None
        unit = [target_tree.UnitPatch(patch.project, patch.diff, patch.base_head)]
        inflight = target_tree.prepare(target, unit)
        run = BuildRun(job_id=job.id, item_ids=[settings.id], patch_ids=[patch.id], modules=[], inflight=inflight.to_json())
        s.add(run)
        s.commit()
        target_tree.apply_patches(target, unit)
    assert snapshot(target) != before

    restarted = create_app(config)
    assert snapshot(target) == before
    assert (job_of(restarted, job.id).status, job_of(restarted, job.id).error) == ("failed", "Interrupted by a server restart")
    (stored,) = runs(restarted, job.id)
    assert (stored.status, stored.inflight) == ("interrupted", None)
    assert events(restarted, "PROJ-102")[-1] == ("tree_restored", {"run_id": stored.id})
    assert item(restarted, "PROJ-102").build_status is None
