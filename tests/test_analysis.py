from __future__ import annotations

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
from patchmig.jira_client import Ticket
from patchmig.ledger import Cluster, KnowledgeItem, LedgerStore, Patch, State
from patchmig.mcp_server.tools import ToolContext, ToolError, get_cluster, upstream_fate
from tests.fixtures.scenario import (
    BT_NEW,
    BT_OLD,
    CAR,
    EXPECTED,
    FRAMEWORKS,
    LEGACY_AUDIO,
    POWER,
    PROJECTS,
    TARGET_BASE_TAG,
    Scenario,
)
from tests.test_ingest import JIRA, FakeJira, make_config  # noqa: F401  (fixture)

VERDICT_STATE = {
    "needed": State.NEEDED,
    "upstream": State.UPSTREAM,
    "obsolete": State.OBSOLETE,
    "dropped-by-revert": State.REVERTED,
}


def _analysed(path: Path, jira=None):
    cfg = load_config(path)
    store = LedgerStore.in_memory()
    run = store.create_run(cfg)
    ingest(cfg, store, run, jira=jira)
    report = analyse(cfg, store, run)
    with store.session() as s:
        patches = s.scalars(select(Patch).where(Patch.run_id == run.id)).all()
        clusters = s.scalars(
            select(Cluster).where(Cluster.run_id == run.id).order_by(Cluster.order_index)
        ).all()
    return cfg, store, run, report, patches, clusters


@pytest.fixture
def analysed(scenario: Scenario, make_config):  # noqa: F811
    cfg, store, run, report, patches, clusters = _analysed(make_config([{"type": "git_range"}]))
    sha_to_key = {sha: key for key, sha in scenario.commits.items()}
    by_key = {sha_to_key[p.commit_sha]: p for p in patches}
    return cfg, store, run, report, by_key, clusters


def test_every_scenario_patch_gets_the_expected_verdict_and_cluster(analysed):
    *_, report, p, clusters = analysed
    for e in EXPECTED:
        assert p[e.key].state == VERDICT_STATE[e.verdict], (e.key, p[e.key].verdict)
    # Same partition as the scenario's cluster names.
    expected_groups = {}
    for e in EXPECTED:
        expected_groups.setdefault(e.cluster, set()).add(p[e.key].id)
    actual_groups = [set(c.result["members"]) for c in clusters]
    assert sorted(map(sorted, actual_groups)) == sorted(map(sorted, expected_groups.values()))
    assert report.clusters == len(expected_groups)
    assert report.warnings == []


def test_each_rung_records_its_evidence(analysed, scenario):
    *_, p, _ = analysed
    assert p["P7"].verdict["rung"] == "identity"
    assert p["P7"].verdict["evidence"]["match"] == "patch-id"
    assert p["P7"].verdict["evidence"]["subject"] == "Enable auto brightness by default"
    assert p["P7"].confidence == 1.0

    assert p["P9"].verdict["rung"] == "textual"
    assert p["P9"].verdict["evidence"]["matched"] == p["P9"].verdict["evidence"]["total"] == 2

    origin = scenario.commits["P10"]
    assert p["P10"].verdict["rung"] == "provenance"
    assert p["P10"].verdict["evidence"]["subject"] == "Add CarWatchdog health check"
    assert p["P10"].verdict["evidence"]["origin"] != origin  # the target's commit, not ours
    # Its patch-id differs from the original (the backport was adapted).
    assert p["P10"].verdict["checks"][0] == {"rung": "identity", "conclusive": False}

    assert p["P4"].verdict["rung"] == "obsolete"
    assert p["P4"].verdict["evidence"]["deleted"] == [LEGACY_AUDIO]
    assert "Remove LegacyAudio" in p["P4"].verdict["evidence"]["commits"][0]

    assert p["P5"].verdict["evidence"]["cancels_with"] == scenario.commits["P6"]
    assert p["P6"].verdict["evidence"]["cancels_with"] == scenario.commits["P5"]

    p1_checks = {c["rung"]: c for c in p["P1"].verdict["checks"]}
    assert p["P1"].verdict["verdict"] == "needed" and p1_checks["identity"]["conclusive"] is False


def test_transitions_are_recorded_by_the_orchestrator(analysed):
    _, store, _, _, p, clusters = analysed
    (t,) = store.history(p["P7"])
    assert (t.from_state, t.to_state, t.actor) == (State.INGESTED, State.UPSTREAM, "orchestrator")
    assert "patch-id" in t.reason
    (t,) = store.history(clusters[0])
    assert t.reason.startswith("forecast: ")


def test_knowledge_pack_has_footprint_and_file_fates(analysed):
    _, store, run, *_ = analysed
    with store.session() as s:
        items = {
            (i.kind, i.key): i.data
            for i in s.scalars(select(KnowledgeItem).where(KnowledgeItem.run_id == run.id))
        }
    bt = items[("file_fate", f"{FRAMEWORKS}:{BT_OLD}")]
    assert bt["status"] == "renamed" and bt["new_path"] == BT_NEW and bt["exact"]
    assert items[("file_fate", f"{CAR}:{LEGACY_AUDIO}")]["status"] == "deleted"
    power = items[("file_fate", f"{FRAMEWORKS}:{POWER}")]
    assert power["changed_signatures"]["setMode"]["after"] == [
        "public void setMode(int mode, int reason)"
    ]
    assert power["commits"] == [power["commits"][0]] and "reason" in power["commits"][0]
    fw = items[("footprint", FRAMEWORKS)]
    assert fw["patches"] == 5 and set(fw["files"]) == {
        POWER,
        BT_OLD,
        "core/res/res/values/config.xml",
    }
    assert "isOemMode" in fw["symbols_defined"]


def _cluster_of(clusters, patch):
    return next(c for c in clusters if patch.id in c.result["members"])


def test_power_mode_cluster_is_folded_across_projects(analysed):
    *_, p, clusters = analysed
    c = _cluster_of(clusters, p["P1"])
    assert c.state == State.NEEDED and c.order_index == 0
    kinds = {(e["kind"], e["detail"]) for e in c.result["edges"]}
    assert ("jira", "OEM-101") in kinds and ("symbol", "isOemMode") in kinds
    assert set(c.result["net_diff_by_project"]) == {FRAMEWORKS, CAR}
    # The fix (P2) is folded into the change it fixes.
    assert "mMode >= 0" in c.net_diff and "mMode > 0" not in c.net_diff
    assert f"+++ b/{FRAMEWORKS}/{POWER}" in c.net_diff
    forecast = c.result["forecast"]
    assert forecast["level"] == "rewrite"
    assert any("setMode() changed signature" in r for r in forecast["reasons"])


def test_other_clusters_forecast_and_dropped_clusters(analysed):
    *_, p, clusters = analysed
    bt = _cluster_of(clusters, p["P3"])
    assert bt.result["forecast"]["level"] == "clean"
    assert "rename-aware apply" in bt.result["forecast"]["reasons"][0]
    reverted = _cluster_of(clusters, p["P5"])
    assert reverted.state == State.REVERTED and reverted.net_diff is None
    assert reverted.result["forecast"] is None
    assert _cluster_of(clusters, p["P7"]).state == State.UPSTREAM
    assert _cluster_of(clusters, p["P4"]).state == State.OBSOLETE


def test_analysing_again_keeps_what_was_decided_and_places_new_patches(analysed, tmp_path):
    from patchmig import backlog as bl
    from patchmig.ledger import Transition
    from tests.test_jira_ingest import ATTACHED

    cfg, store, run, _, p, clusters = analysed
    with store.session() as s:
        moves = s.query(Transition).count()
    again = analyse(cfg, store, run)
    assert (again.decided, again.clusters_new, again.clusters_changed) == (0, 0, 0)
    with store.session() as s:
        assert s.query(Transition).count() == moves  # nothing moved
        assert s.query(KnowledgeItem).filter_by(kind="file_fate").count() == len(
            {(f.project, f.path) for f in (_fate(i) for i in s.query(KnowledgeItem).filter_by(
                kind="file_fate"))})  # fmt: skip

    # The power cluster has been migrated: it is left as it is.
    power, bt = _cluster_of(clusters, p["P1"]), _cluster_of(clusters, p["P3"])
    with store.session() as s, s.begin():
        row = s.get(Cluster, power.id)
        row.result = {**row.result, "migration": {"state": "applied"}}
    # New information: a follow-up diff for the BT ticket, and one for the power ticket.
    bl.add_info(cfg, store, run, "OEM-202", "xuan", filename="bt.diff", content=ATTACHED)
    new_file = f"{FRAMEWORKS}/core/java/android/os/OemPowerLog.java"
    power_fix = (f"diff --git a/{new_file} b/{new_file}\nnew file mode 100644\n--- /dev/null\n"
                 f"+++ b/{new_file}\n@@ -0,0 +1,3 @@\n+package android.os;\n+\n"
                 "+class OemPowerLog {}\n")  # fmt: skip
    bl.add_info(cfg, store, run, "OEM-101", "xuan", filename="power.diff", content=power_fix)
    ingest(cfg, store, run, items=["OEM-202", "OEM-101"])
    report = analyse(cfg, store, run)
    assert report.decided == 2 and report.clusters_new == 1 and report.clusters_changed == 1
    with store.session() as s:
        added = s.scalars(select(Patch).where(Patch.sources == ["added"])).all()
        by_key = {x.jira_keys[0]: x for x in added}
        assert by_key["OEM-202"].cluster_id == bt.id  # joined the BT cluster, not migrated yet
        assert by_key["OEM-202"].state == State.NEEDED
        own = s.get(Cluster, by_key["OEM-101"].cluster_id)
        assert own.id not in {c.id for c in clusters}  # the power cluster stays closed
        assert power.order_index in own.result["depends_on_order"]
        assert set(s.get(Cluster, power.id).result["members"]) == set(power.result["members"])
    assert any(f"depends on cluster {power.id}, already migrated" in w for w in report.warnings)


def _fate(item):
    from patchmig.knowledge.upstream import FileFate

    return FileFate.from_json(item.data)


def test_path_rules_park_patches_for_a_decision(make_config):  # noqa: F811
    rules = [{"pattern": f"{FRAMEWORKS}/services/**", "rule": "needs_decision",
              "reason": "system services need an architect"}]  # fmt: skip
    *_, patches, clusters = _analysed(make_config([{"type": "git_range"}], path_rules=rules))
    parked = [p for p in patches if p.state == State.NEEDS_DECISION]
    assert [p.subject for p in parked] == ["Tune BT reconnect timeout for car head units"]
    assert "system services need an architect" in parked[0].verdict["question"]
    assert _cluster_of(clusters, parked[0]).state == State.NEEDS_DECISION


def test_jira_issue_links_connect_patches(make_config):  # noqa: F811
    jira = FakeJira({"OEM-202": Ticket("OEM-202", "BT", issue_links=["OEM-707"])})
    path = make_config([{"type": "git_range"}], jira=JIRA)
    *_, patches, clusters = _analysed(path, jira=jira)
    bt = next(p for p in patches if p.subject.startswith("Tune BT"))
    c = _cluster_of(clusters, bt)
    assert {e["kind"] for e in c.result["edges"]} == {"jira_link"}
    assert len(c.result["members"]) == 2


@pytest.fixture
def flat_target(scenario: Scenario, tmp_path: Path) -> Path:
    """A target tree with the same files but no shared history (e.g. an imported QSSI drop)."""
    root = tmp_path / "flat"
    for project in PROJECTS:
        repo = root / project
        repo.mkdir(parents=True)
        archive = subprocess.run(
            ["git", "archive", TARGET_BASE_TAG], cwd=scenario.target / project,
            capture_output=True, check=True,
        )  # fmt: skip
        subprocess.run(["tar", "-x"], cwd=repo, input=archive.stdout, check=True)
        for args in (["init", "-q"], ["add", "-A"],
                     ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "drop"],
                     ["tag", TARGET_BASE_TAG]):  # fmt: skip
            subprocess.run(["git", *args], cwd=repo, check=True)
    return root


def test_without_shared_history_contents_are_compared(make_config, flat_target, scenario):  # noqa: F811
    path = make_config([{"type": "git_range"}])
    raw = yaml.safe_load(path.read_text())
    raw["trees"]["target"]["path"] = str(flat_target)
    path.write_text(yaml.safe_dump(raw))
    *_, report, patches, _ = _analysed(path)
    sha_to_key = {sha: key for key, sha in scenario.commits.items()}
    p = {sha_to_key[x.commit_sha]: x for x in patches}
    assert p["P7"].state == State.UPSTREAM and p["P7"].verdict["rung"] == "textual"
    assert p["P4"].state == State.OBSOLETE
    assert p["P3"].state == State.NEEDED
    # Without history, the backport's origin can't be found: it stays needed for review.
    assert p["P10"].state == State.NEEDED
    assert report.warnings == []


def test_cli_analyse_and_clusters(make_config):  # noqa: F811
    path = make_config([{"type": "git_range"}])
    runner = CliRunner()
    assert runner.invoke(app, ["clusters", "-c", str(path)]).exit_code == 1  # no run yet
    assert runner.invoke(app, ["ingest", "-c", str(path), "--no-jira"]).exit_code == 0
    result = runner.invoke(app, ["analyse", "-c", str(path), "--no-agent"])
    assert result.exit_code == 0, result.output
    assert f"{len(EXPECTED)} patches ({len(EXPECTED)} checked now), 6 clusters" in result.output
    assert "reverted        2" in result.output
    assert "clusters to migrate: clean 1, rewrite 1" in result.output
    result = runner.invoke(app, ["clusters", "-c", str(path)])
    lines = result.output.splitlines()
    assert len(lines) == 6 and "rewrite" in lines[0] and "Add OEM power mode" in lines[0]
    cid = int(lines[0].split("[")[1].split("]")[0])
    result = runner.invoke(app, ["clusters", "-c", str(path), "--show", str(cid)])
    assert "link" in result.output and "forecast: setMode()" in result.output
    assert f"+++ b/{CAR}/" in result.output
    result = runner.invoke(app, ["analyse", "-c", str(path), "--no-agent"])
    assert result.exit_code == 0 and "(0 checked now)" in result.output


def test_mcp_tools_expose_clusters_and_fates(analysed):
    cfg, store, run, _, p, clusters = analysed
    ctx = ToolContext(cfg, store, run.id)
    c = get_cluster(ctx, clusters[0].id)
    assert c["forecast"]["level"] == "rewrite" and p["P1"].id in c["members"]
    fate = upstream_fate(ctx, FRAMEWORKS, BT_OLD)
    assert fate["new_path"] == BT_NEW
    with pytest.raises(ToolError, match="not in the knowledge pack"):
        upstream_fate(ctx, FRAMEWORKS, "nope.java")
    with pytest.raises(ToolError, match="not part of run"):
        get_cluster(ToolContext(cfg, store, run.id + 1), clusters[0].id)


def test_missing_target_project_is_unknown_not_obsolete(make_config, flat_target, scenario):  # noqa: F811
    import shutil

    shutil.rmtree(flat_target / CAR)
    path = make_config([{"type": "git_range"}])
    raw = yaml.safe_load(path.read_text())
    raw["trees"]["target"]["path"] = str(flat_target)
    path.write_text(yaml.safe_dump(raw))
    *_, report, patches, _ = _analysed(path)
    car = {p.subject: p for p in patches if p.project == CAR}
    assert car["Halve ducking volume in legacy audio"].state == State.NEEDED
    assert car["Log car power transitions"].state == State.REVERTED
    assert report.warnings == [f"{CAR}: no such project in the target tree"]
