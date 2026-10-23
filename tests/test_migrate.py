from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import yaml
from sqlalchemy import select
from typer.testing import CliRunner

from patchmig.agents.fake import FakeAdapter
from patchmig.agents.runner import AgentRunner
from patchmig.analysis import analyse
from patchmig.cli import app
from patchmig.config import load_config
from patchmig.ingest import ingest
from patchmig.ledger import Cluster, LedgerStore, Patch, State
from patchmig.ledger.models import AgentCall
from patchmig.migrate import migrate
from patchmig.migrate.workspace import branch_name
from tests.fixtures.scenario import (
    BT_NEW,
    CAR,
    CAR_POWER,
    FRAMEWORKS,
    POWER,
    SOURCE_BASE_TAG,
    SOURCE_HEAD_TAG,
    TARGET_BASE_TAG,
    build_scenario,
)

CONFLICT = (
    "<<<<<<< ours\n    public void setMode(int mode, int reason) {\n=======\n"
    "    public synchronized void setMode(int mode) {\n>>>>>>> theirs\n"
)
RESOLVED = "    public synchronized void setMode(int mode, int reason) {\n"


class Project:
    """A fresh scenario (migration writes branches into the target), ingested and analysed."""

    def __init__(self, root: Path, **extra):
        self.scenario = build_scenario(root / "trees")
        cfg = {
            "name": "migrate-test",
            "workdir": str(root / "work"),
            "migration": {"source_version": "14", "target_version": "15"},
            "trees": {
                "source": {
                    "path": str(self.scenario.source),
                    "manifest": ".repo/manifests/default.xml",
                    "base_ref": SOURCE_BASE_TAG,
                    "head_ref": SOURCE_HEAD_TAG,
                },
                "target": {"path": str(self.scenario.target), "base_ref": TARGET_BASE_TAG},
            },
            "patch_sources": [{"type": "git_range"}],
            **extra,
        }
        self.config_path = root / "project.yaml"
        self.config_path.write_text(yaml.safe_dump(cfg))
        self.config = load_config(self.config_path)
        self.store = LedgerStore.open(self.config.ledger_path)
        self.run = self.store.create_run(self.config)
        ingest(self.config, self.store, self.run)
        analyse(self.config, self.store, self.run)
        self.fake: FakeAdapter | None = None

    def runner(self, cluster_agent, reviewer=None) -> AgentRunner:
        def respond(task, attempt):
            if task.agent == "reviewer":
                return (reviewer or approve())(task, attempt)
            return cluster_agent(task, attempt)

        self.fake = FakeAdapter(respond)
        return AgentRunner(self.fake, self.store, self.run, self.config.llm, self.config.budget)

    def migrate(self, runner=None, **kw):
        return migrate(self.config, self.store, self.run, runner, **kw)

    def patch(self, key: str) -> Patch:
        sha = self.scenario.commits[key]
        with self.store.session() as s:
            return s.scalars(select(Patch).where(Patch.commit_sha == sha)).one()

    def cluster_of(self, key: str) -> Cluster:
        with self.store.session() as s:
            return s.get(Cluster, self.patch(key).cluster_id)

    def target_git(self, project: str, *args: str) -> str:
        return subprocess.run(
            ["git", *args],
            cwd=self.scenario.target / project,
            check=True,
            capture_output=True,
            text=True,
        ).stdout

    def agent_calls(self) -> list[AgentCall]:
        with self.store.session() as s:
            return list(s.scalars(select(AgentCall).order_by(AgentCall.id)))


def approve(confidence: float = 0.8):
    return lambda task, attempt: {
        "verdict": "approve",
        "intent_met": True,
        "issues": [],
        "summary": "Matches the intent.",
        "confidence": confidence,
    }


def answer(outcome: str, confidence: float = 0.9, **extra) -> dict:
    return {"outcome": outcome, "summary": f"{outcome} it", "confidence": confidence, **extra}


def resolve_power(task, attempt) -> dict:
    """What a good agent does: merge both sides and adapt the OEM overload to the new API."""
    path = task.workdir / FRAMEWORKS / POWER
    text = path.read_text()
    assert CONFLICT in text
    text = text.replace(CONFLICT, RESOLVED).replace(
        "        setMode(mode);\n", "        setMode(mode, 0);\n"
    )
    path.write_text(text)
    return answer("resolved", files=[f"{FRAMEWORKS}/{POWER}"])


@pytest.fixture
def project(tmp_path: Path) -> Project:
    return Project(tmp_path)


def test_without_agent_cherry_pick_migrates_the_moved_file(project):
    report = project.migrate()
    by_cluster = {o.cluster_id: o for o in report.outcomes}

    bt = project.cluster_of("P3")
    assert bt.state == State.APPLIED and bt.confidence == 0.9
    assert by_cluster[bt.id].strategy == "cherry_pick"
    assert project.patch("P3").state == State.APPLIED
    branch = branch_name(project.config.name, project.run.id, bt.id)
    log = project.target_git(FRAMEWORKS, "log", "-1", "--format=%an|%cn|%B", branch)
    assert log.startswith("OEM Dev|patchmig|Tune BT reconnect timeout")
    assert f"(cherry picked from commit {project.scenario.commits['P3']})" in log
    # Rename-aware: the change landed in the file's new upstream location.
    assert "RECONNECT_TIMEOUT_MS = 12000" in project.target_git(
        FRAMEWORKS, "show", f"{branch}:{BT_NEW}"
    )

    power = project.cluster_of("P1")
    assert by_cluster[power.id].state == "needs_agent"
    assert power.state == State.NEEDED  # nothing decided without the agent
    m = power.result["migration"]
    assert m["projects"][CAR]["strategy"] == "cherry_pick"
    fw = m["projects"][FRAMEWORKS]
    assert fw["cherry_pick"]["conflicts"] == [POWER]
    assert fw["net_diff"]["conflicts"] == [POWER]
    # The worktree is sparse and holds the conflict for the agent.
    worktree = Path(fw["path"])
    assert CONFLICT in (worktree / POWER).read_text()
    assert not (worktree / "services").exists()


def test_agent_resolves_the_conflict_and_the_reviewer_sets_confidence(project):
    runner = project.runner(resolve_power, approve(0.8))
    project.migrate(runner, mcp={"type": "local", "command": "true"})

    power = project.cluster_of("P1")
    assert power.state == State.RESOLVED
    assert power.confidence == 0.8  # min(agent 0.9, reviewer 0.8)
    for key in ("P1", "P2", "P8"):
        assert project.patch(key).state == State.RESOLVED
    branch = branch_name(project.config.name, project.run.id, power.id)
    migrated = project.target_git(FRAMEWORKS, "show", f"{branch}:{POWER}")
    assert RESOLVED in migrated and "<<<<<<<" not in migrated
    msg = project.target_git(FRAMEWORKS, "log", "-1", "--format=%B", branch)
    assert "strategy: resolved" in msg and "Bug: OEM-101" in msg
    assert "Original-Change-Id: I1000000000000000000000000000000000000001" in msg
    # P8 (other project) was cherry-picked onto the same cluster branch there.
    assert "isOemMode" in project.target_git(CAR, "show", f"{branch}:{CAR_POWER}")

    cluster_task, review_task = (t for t, _ in project.fake.calls)
    assert cluster_task.agent == "cluster_agent"
    assert cluster_task.tools.allow == ["write", "patchmig"]
    assert list(cluster_task.tools.mcp_servers) == ["patchmig"]
    assert cluster_task.tools.deny == ["shell"]
    assert f"`{FRAMEWORKS}/{POWER}`" in cluster_task.brief
    assert "migrated by cherry_pick and committed" in cluster_task.brief
    assert "setMode" in cluster_task.brief  # upstream signature change is explained
    assert review_task.agent == "reviewer"
    assert "write" in review_task.tools.deny
    assert "int reason" in review_task.brief  # the reviewer sees the migrated diff

    m = power.result["migration"]
    assert m["strategy"] == "resolved"
    assert m["guardrails"]["ok"] and m["review"]["verdict"] == "approve"
    calls = project.agent_calls()
    assert [c.agent for c in calls] == ["cluster_agent", "reviewer"]
    assert all(c.cluster_id == power.id for c in calls)


def test_guardrail_failure_is_sent_back_once(project):
    def sloppy_then_good(task, attempt):
        if "rejected by the automatic checks" not in task.brief:
            return answer("resolved")  # leaves the conflict markers in place
        assert "conflict markers left" in task.brief
        return resolve_power(task, attempt)

    project.migrate(project.runner(sloppy_then_good))
    power = project.cluster_of("P1")
    assert power.state == State.RESOLVED
    attempts = power.result["migration"]["agent"]["attempts"]
    assert len(attempts) == 2 and not attempts[0]["guardrails"]["ok"]


def test_repeated_guardrail_failure_fails_the_cluster(project):
    project.migrate(project.runner(lambda task, attempt: answer("resolved")))
    power = project.cluster_of("P1")
    assert power.state == State.FAILED
    assert project.patch("P1").state == State.FAILED
    assert "conflict markers" in project.store.history(power)[-1].reason
    assert [t.agent for t, _ in project.fake.calls] == ["cluster_agent", "cluster_agent"]


def test_forbidden_path_fails_and_unexpected_files_are_noted(tmp_path):
    rule = {"pattern": f"{FRAMEWORKS}/core/java/android/os/Extra*", "rule": "forbidden",
            "reason": "no new framework API"}  # fmt: skip
    project = Project(tmp_path, path_rules=[rule])

    def adds_a_file(task, attempt):
        if CONFLICT in (task.workdir / FRAMEWORKS / POWER).read_text():
            resolve_power(task, attempt)
        (task.workdir / FRAMEWORKS / "core/java/android/os/Extra.java").write_text("class X {}\n")
        return answer("resolved")

    project.migrate(project.runner(adds_a_file))
    power = project.cluster_of("P1")
    assert power.state == State.FAILED
    report = power.result["migration"]["guardrails"]
    assert any("forbidden by path rule" in h for h in report["hard"])
    assert any("not touched by the original patches" in s for s in report["soft"])


def test_upstream_claim_needs_evidence_that_exists_in_the_target(project, tmp_path):
    good = {"project": FRAMEWORKS, "path": POWER,
            "snippet": "public void setMode(int mode,   int reason) {"}  # fmt: skip
    project.migrate(project.runner(lambda t, a: answer("upstream", evidence=[good])))
    power = project.cluster_of("P1")
    assert power.state == State.UPSTREAM
    assert project.patch("P8").state == State.UPSTREAM
    assert project.store.history(power)[-1].actor == "agent:cluster_agent"

    other = Project(tmp_path / "other")
    bad = {**good, "snippet": "public void setOemMode(boolean oem) {"}
    other.migrate(other.runner(lambda t, a: answer("upstream", evidence=[bad])))
    power = other.cluster_of("P1")
    assert power.state == State.NEEDS_DECISION
    assert "evidence was not found" in power.result["migration"]["question"]
    # Nothing the agent left behind is kept: the worktree is back at the branch start.
    worktree = Path(power.result["migration"]["projects"][FRAMEWORKS]["path"])
    assert "<<<<<<<" not in (worktree / POWER).read_text()


def test_needs_decision_and_failed_outcomes(project, tmp_path):
    question = "Should OEM mode survive suspend now that setMode takes a reason?"
    project.migrate(project.runner(lambda t, a: answer("needs_decision", question=question)))
    power = project.cluster_of("P1")
    assert power.state == State.NEEDS_DECISION
    assert project.store.history(power)[-1].reason == question
    assert project.fake.calls[-1][0].agent == "cluster_agent"  # no review without a change

    other = Project(tmp_path / "other")
    other.migrate(other.runner(lambda t, a: answer("failed", confidence=0.1)))
    assert other.cluster_of("P1").state == State.FAILED


def test_reviewer_rejection_and_concerns(project, tmp_path):
    def reject(task, attempt):
        return {"verdict": "reject", "intent_met": False, "issues": ["mode 0 check lost"],
                "summary": "Wrong.", "confidence": 0.9}  # fmt: skip

    project.migrate(project.runner(resolve_power, reject))
    power = project.cluster_of("P1")
    assert power.state == State.FAILED
    assert "mode 0 check lost" in project.store.history(power)[-1].reason

    other = Project(tmp_path / "other")
    concerns = lambda t, a: {**approve(0.95)(t, a), "verdict": "concerns"}  # noqa: E731
    other.migrate(other.runner(resolve_power, concerns))
    power = other.cluster_of("P1")
    assert power.state == State.RESOLVED
    assert power.confidence < other.config.thresholds.quick_approve


def test_dependent_cluster_waits_then_builds_on_its_dependency(tmp_path):
    project = Project(tmp_path, analysis={"max_cluster_patches": 2})
    p1, p2 = project.cluster_of("P1"), project.cluster_of("P2")
    assert p1.id != p2.id and p2.result["depends_on_order"] == [p1.order_index]

    report = project.migrate(cluster_ids=[p2.id])
    assert report.outcomes[0].state == "skipped" and "waits for" in report.outcomes[0].reason

    project.migrate(project.runner(resolve_power))
    p2 = project.cluster_of("P2")
    # The fix applies cleanly on top of the agent-resolved P1 branch, not on the target base.
    assert p2.state == State.APPLIED
    fw = p2.result["migration"]["projects"][FRAMEWORKS]
    assert (
        fw["base"] == project.cluster_of("P1").result["migration"]["projects"][FRAMEWORKS]["head"]
    )
    text = project.target_git(
        FRAMEWORKS, "show", f"{branch_name(project.config.name, project.run.id, p2.id)}:{POWER}"
    )
    assert "mMode >= 0" in text and RESOLVED in text


def test_revived_cluster_is_rewritten_from_the_patch_itself(project):
    """P4's file was deleted upstream; a human says the intent still matters."""
    p4 = project.patch("P4")
    project.store.transition(p4, State.NEEDED, actor="human:xuan", reason="ducking still needed",
                             force=True)  # fmt: skip
    audio = project.cluster_of("P4")
    project.store.transition(audio, State.NEEDED, actor="human:xuan", reason="revived")

    def rewrite(task, attempt):
        assert "the patches do not apply" in task.brief
        assert "LegacyAudio.java: deleted" in task.brief
        assert "setVolume(volume * 0.5f)" in task.brief  # the member's own diff
        path = task.workdir / CAR / CAR_POWER
        path.write_text(path.read_text().replace("    }\n}", "    }\n    // duck at 50%\n}"))
        return answer("rewritten", confidence=0.6)

    project.migrate(project.runner(rewrite), cluster_ids=[audio.id])
    audio = project.cluster_of("P4")
    assert audio.state == State.REWRITTEN
    assert audio.confidence == 0.6
    assert project.patch("P4").state == State.REWRITTEN


def test_cli_apply_without_agent(project):
    result = CliRunner().invoke(app, ["apply", "--config", str(project.config_path), "--no-agent"])
    assert result.exit_code == 0, result.output
    assert "applied" in result.output and "needs_agent" in result.output
    assert "Estimated credits" not in result.output


def test_web_cluster_view_shows_the_migration(project):
    from patchmig.web import data

    project.migrate(project.runner(resolve_power))
    power = project.cluster_of("P1")
    view = data.cluster(project.store, project.run.id, power.id)
    m = view["migration"]
    assert m["strategy"] == "resolved" and m["review"]["verdict"] == "approve"
    assert {p["project"] for p in m["projects"]} == {FRAMEWORKS, CAR}
    assert all("path" not in p for p in m["projects"])  # no local paths in exports
    assert f"+++ b/{FRAMEWORKS}/{POWER}" in m["diff"] and RESOLVED.strip() in m["diff"]
    assert (
        data.cluster(project.store, project.run.id, project.cluster_of("P7").id)["migration"]
        is None
    )


def test_two_projects_on_one_target_tree_keep_their_own_branches(project, tmp_path):
    bt = project.cluster_of("P3")
    project.migrate(None, cluster_ids=[bt.id])
    first = branch_name(project.config.name, project.run.id, bt.id)
    head = project.target_git(FRAMEWORKS, "rev-parse", first).strip()

    pilot = project.config.model_copy(update={"name": "Pilot slice", "workdir": tmp_path / "w2"})
    store = LedgerStore.open(pilot.ledger_path)
    run = store.create_run(pilot)
    ingest(pilot, store, run)
    analyse(pilot, store, run)
    with store.session() as s:
        other = s.scalars(
            select(Cluster).where(Cluster.run_id == run.id, Cluster.id == bt.id)
        ).one()
    migrate(pilot, store, run, None, cluster_ids=[other.id])
    second = branch_name("Pilot slice", run.id, other.id)
    assert second == f"patchmig/pilot-slice/r{run.id}/c{other.id}" and second != first
    assert project.target_git(FRAMEWORKS, "rev-parse", first).strip() == head  # untouched
    assert project.target_git(FRAMEWORKS, "rev-parse", second).strip()


def test_intents_are_not_bought_for_dropped_patches(project):
    from patchmig.agents.intent import write_intents

    asked = []

    def writer(task, attempt):
        asked.append(task.subject_id)
        return {"problem": "p", "change": "c", "verification": "v", "components": [],
                "sources": ["diff"], "confidence": 0.7}  # fmt: skip

    report = write_intents(project.config, project.store, project.run, project.runner(writer))
    assert report.not_needed == 6 and report.written == 4
    assert {project.patch(k).id for k in ("P1", "P2", "P3", "P8")} == set(asked)
