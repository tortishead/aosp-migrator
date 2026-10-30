from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient
from repo_tree import RepoTree
from test_build_runner import events, fake_jira, item, job_of, log_of, runner, runs, workspace  # noqa: F401
from test_import import session
from test_prescan_api import app, client, target, workspace_id  # noqa: F401
from test_tree import snapshot

from aosp_migrator.config import Config
from aosp_migrator.db.models import MigrationPatch
from aosp_migrator.prescan.chain import chain, current_patches, order, patch_plan, stale_items
from aosp_migrator.prescan.runner import current_diffs, read_diff_text
from aosp_migrator.prescan.stack import Stack, StackDiff

CORE = "system/core"


def tie(*keys: str) -> dict[int, tuple[bool, int, str]]:
    """Items 1, 2, ... outside any cluster, with these keys."""
    return {n: (True, 0, key) for n, key in enumerate(keys, 1)}


def test_order_puts_prerequisites_first() -> None:
    # 1 needs 3, 3 needs 2.
    assert order({1: [3], 3: [2]}, tie("A", "B", "C")) == [[2], [3], [1]]


def test_order_ties_by_cluster_order_then_key() -> None:
    ties = {1: (True, 0, "PROJ-2"), 2: (True, 0, "PROJ-1"), 3: (False, 2, "PROJ-9"), 4: (False, 1, "PROJ-8")}
    assert order({}, ties) == [[4], [3], [2], [1]]


def test_cycle_is_one_unit() -> None:
    # 1 and 2 need each other, 3 needs 1, 2 needs 4.
    units = order({1: [2], 2: [1, 4], 3: [1]}, tie("B", "A", "C", "D"))
    assert units == [[4], [2, 1], [3]]


def test_needs_outside_the_items_are_ignored() -> None:
    assert order({1: [7], 2: [1]}, tie("A", "B")) == [[1], [2]]


def test_chain_from_dependencies_and_clusters(app: FastAPI, client: TestClient, workspace: int) -> None:
    ids = {key: item(app, key).id for key in ("PROJ-103", "PROJ-105", "PROJ-106", "PROJ-108")}
    with session(app) as s:
        # PROJ-106's diff is written on top of PROJ-105's (a `pending` dependency).
        found = chain(s, workspace, [ids["PROJ-106"]])
        assert found.units == [[ids["PROJ-105"]], [ids["PROJ-106"]]]
        assert found.needs == {ids["PROJ-105"]: set(), ids["PROJ-106"]: {ids["PROJ-105"]}}
        assert chain(s, workspace, [ids["PROJ-105"]]).units == [[ids["PROJ-105"]]]

    # A cluster adds the members before an item; one that needs a later member makes a cycle with it.
    cluster = client.post(f"/api/workspaces/{workspace}/clusters", json={"keys": ["PROJ-108", "PROJ-106", "PROJ-103"], "name": "c"}).json()
    client.put(f"/api/workspaces/{workspace}/clusters/{cluster['id']}/order", json={"keys": ["PROJ-108", "PROJ-106", "PROJ-103"]})
    with session(app) as s:
        found = chain(s, workspace, [ids["PROJ-103"]])
        assert found.units == [[ids["PROJ-108"]], [ids["PROJ-105"]], [ids["PROJ-106"]], [ids["PROJ-103"]]]
    client.put(f"/api/workspaces/{workspace}/clusters/{cluster['id']}/order", json={"keys": ["PROJ-106", "PROJ-108", "PROJ-103"]})
    client.post(f"/api/workspaces/{workspace}/clusters/{cluster['id']}/members", json={"keys": ["PROJ-105"]})
    client.put(f"/api/workspaces/{workspace}/clusters/{cluster['id']}/order", json={"keys": ["PROJ-106", "PROJ-105", "PROJ-108", "PROJ-103"]})
    with session(app) as s:
        found = chain(s, workspace, [ids["PROJ-105"]])
        # PROJ-106 comes before PROJ-105 in the cluster but needs it: one unit, in cluster order.
        assert found.units == [[ids["PROJ-106"], ids["PROJ-105"]]]


def stacked_rework(app: FastAPI, target: Path, review: str = "accepted") -> tuple[int, int]:
    """A `stacked` rework patch for PROJ-106 on PROJ-105's current patch, as rung 1 would make it; their ids."""
    with session(app) as s:
        (base,) = current_patches(s, s.merge(item(app, "PROJ-105")))
        (diff,) = current_diffs(s, item(app, "PROJ-106").issue_id)
        text = read_diff_text(diff)
        assert text is not None
        scratch = target.parent / "scratch"
        scratch.mkdir(exist_ok=True)
        stack = Stack(target, scratch)
        assert stack.add([StackDiff("base", CORE, base.diff)]).applied
        stacked = stack.add([StackDiff(diff.filename, CORE, text)])
        assert stacked.applied and stacked.diffs[0].patch is not None
        patch = MigrationPatch(
            item_id=item(app, "PROJ-106").id,
            project=CORE,
            source="original",
            level="stacked",
            base_head=stack.heads[CORE],
            base_patch_ids=[base.id],
            review=review,
            diff=stacked.diffs[0].patch,
        )
        s.add(patch)
        s.commit()
        return base.id, patch.id


def test_rework_patch_replaces_the_original_in_its_project(app: FastAPI, client: TestClient, workspace: int, target: Path) -> None:
    base_id, patch_id = stacked_rework(app, target)
    with session(app) as s:
        assert [p.id for p in patch_plan(s, s.merge(item(app, "PROJ-106"))) if isinstance(p, MigrationPatch)] == [patch_id]
        assert stale_items(s, workspace, target, [item(app, "PROJ-106").id]) == {}
    patches = client.get(f"/api/workspaces/{workspace}/items/PROJ-106/patches").json()
    assert [(p["id"], p["level"], p["base_patch_ids"], p["review"], p["current"]) for p in patches][0] == (patch_id, "stacked", [base_id], "accepted", True)

    # Rejected: back to the original patch, if any.
    with session(app) as s:
        rework = s.get(MigrationPatch, patch_id)
        assert rework is not None
        rework.review = "rejected"
        s.commit()
        assert patch_id not in [p.id for p in current_patches(s, s.merge(item(app, "PROJ-106")))]


def test_stale_on_base_change_and_head_move(app: FastAPI, workspace: int, target: Path) -> None:
    base_id, _ = stacked_rework(app, target)
    dependent = item(app, "PROJ-106").id
    # A newer rework patch of PROJ-105 replaces the base PROJ-106 was made on.
    with session(app) as s:
        base = s.get(MigrationPatch, base_id)
        assert base is not None
        s.add(MigrationPatch(item_id=base.item_id, project=CORE, source="ai", level="resolve", base_head=base.base_head, review="proposed", diff=base.diff))
        s.commit()
        assert stale_items(s, workspace, target, [dependent]) == {dependent: ["base PROJ-105 changed"]}

    RepoTree(target).commit(CORE, {"init/README": "init\n"}, "Add README")
    with session(app) as s:
        assert stale_items(s, workspace, target, [dependent])[dependent] == [f"{CORE} HEAD moved", "base PROJ-105 changed"]


def test_build_applies_the_base_first_and_a_stale_patch_still_builds(app: FastAPI, config: Config, workspace: int, target: Path) -> None:
    base_id, patch_id = stacked_rework(app, target)
    RepoTree(target).commit(CORE, {"init/README": "init\n"}, "Add README")
    before = snapshot(target)
    job = job_of(app, runner(app, config).start(workspace, keys=["PROJ-106"]).id)
    assert snapshot(target) == before
    assert (job.issue_keys, job.skipped) == (["PROJ-106"], [])
    (run,) = runs(app, job.id)
    assert (run.status, run.patch_ids, run.modules) == ("build_successful", [base_id, patch_id], ["system/core"])
    # Only the item itself is built; the base is another item's patch.
    assert run.item_ids == [item(app, "PROJ-106").id]
    assert item(app, "PROJ-105").build_status is None


def test_proposed_patch_is_not_built(app: FastAPI, config: Config, workspace: int, target: Path) -> None:
    stacked_rework(app, target, review="proposed")
    job = job_of(app, runner(app, config).start(workspace, keys=["PROJ-106"]).id)
    assert (job.issue_keys, job.skipped) == ([], [{"key": "PROJ-106", "reason": f"patch proposed: {CORE}"}])


def test_stacked_build_of_patches_with_a_base(app: FastAPI, config: Config, workspace: int, target: Path) -> None:
    _, patch_id = stacked_rework(app, target)
    before = snapshot(target)
    job = job_of(app, runner(app, config).start(workspace, keys=["PROJ-105", "PROJ-106"], stacked=True).id)
    assert snapshot(target) == before
    (run,) = runs(app, job.id)
    assert (run.status, run.item_ids) == ("build_successful", [item(app, "PROJ-105").id, item(app, "PROJ-106").id])
    # PROJ-106 stacks its rework patch, not its diff.
    assert f"Stacked PROJ-106: patch {patch_id} ({CORE}) applies" in log_of(run)
    assert events(app, "PROJ-106")[-1][1]["status"] == "build_successful"
