from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from patchmig.agents.analyst import analyse_items
from patchmig.agents.curator import curate, signals
from patchmig.agents.fake import FakeAdapter
from patchmig.agents.runner import AgentRunner
from patchmig.cli import app
from patchmig.ledger import Patch, State
from patchmig.memory import Memory, MemoryRepoError, change_ids
from patchmig.report import run_report
from tests.fixtures.analyst import analyst
from tests.fixtures.scenario import BT_NEW, BT_OLD, FRAMEWORKS, POWER
from tests.test_migrate import Project, answer, resolve_power
from tests.test_publish_review import Published

POWER_ID = "I1000000000000000000000000000000000000001"
BT_ID = "I1000000000000000000000000000000000000003"


class Remembered(Published):
    """First migration done: analysed, reviewed (with feedback), approved, published, recorded."""

    def __init__(self, root: Path):
        super().__init__(root, verify=False)
        self.memory_path = root / "memory"
        self.config = self.config.model_copy(
            update={"memory": self.config.memory.model_copy(update={"path": self.memory_path})}
        )
        self.config_path.write_text(
            yaml.safe_dump(self.config.model_dump(mode="json", exclude_none=True))
        )
        from patchmig import notes as engineer_notes

        engineer_notes.add(self.store, "BT timeout is 12 s on all head units.", "xuan", "ticket",
                           "OEM-202", stages=["analyse"])  # fmt: skip
        analyse_items(self.config, self.store, self.run, self.runner(analyst(by_key={
            "OEM-202": {"solution": {"approach": "cherry_pick", "summary": "Moved file only."},
                        "related": [{"ref": "OEM-101", "relation": "related", "why": "both"}]},
        })))  # fmt: skip
        with self.store.session() as s, s.begin():
            for p in s.query(Patch).all():
                p.intent = f"Intent of {p.subject}"
        self.migrate(self.runner(resolve_power))
        self.verify()
        r = self.reviewer()
        power = self.cluster_of("P1")
        r.reject(power.id, "lead", "Pass REASON_OEM, not 0, as the reason.")
        self.migrate(self.runner(resolve_power), cluster_ids=[power.id])
        self.verify(cluster_ids=[power.id])
        for key in ("P1", "P3", "P7"):
            r.approve(self.cluster_of(key).id, "xuan")
        self.publish()
        self.memory = Memory.open(self.config, create=True)
        self.report = self.memory.record(self.config, self.store, self.run)


@pytest.fixture
def remembered(tmp_path: Path, monkeypatch) -> Remembered:
    monkeypatch.setenv("GH_TOKEN", "test-token")
    return Remembered(tmp_path / "first")


def memory_git(mem: Memory, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=mem.path, capture_output=True, text=True,
                          check=True).stdout  # fmt: skip


def test_record_writes_approved_facts_and_commits(remembered, monkeypatch):
    r, mem = remembered, remembered.memory
    assert r.report.patches >= 4 and r.report.commit
    power = yaml.safe_load((mem.path / f"patches/{POWER_ID}.yaml").read_text())
    (entry,) = power["history"]
    assert entry["migration"] == "mig-A14-A15" and entry["outcome"] == "verified"
    assert entry["intent"] == "Intent of Add OEM power mode"
    assert entry["approved_by"] == "xuan" and entry["prs"]
    assert entry["feedback"] == ["Pass REASON_OEM, not 0, as the reason."]
    assert power["jira"] == ["OEM-101"]
    # Not approved: not remembered.
    assert not (mem.path / "patches/I1000000000000000000000000000000000000004.yaml").exists()
    # The file the BT patch changes moved upstream: a mapping for the next migration.
    assert mem.mappings(FRAMEWORKS)[BT_OLD] == {
        "fate": "renamed", "to": f"{FRAMEWORKS}/{BT_NEW}", "migration": "mig-A14-A15",
    }  # fmt: skip
    decisions = yaml.safe_load((mem.path / "decisions/mig-A14-A15.yaml").read_text())
    assert decisions["runs"][r.run.id][0]["kind"] == "feedback"

    monkeypatch.setattr("patchmig.memory._now", lambda: "2099-01-01T00:00:00+00:00")
    again = mem.record(r.config, r.store, r.run)
    assert again.commit is None  # nothing new: no commit, however much time passed
    assert memory_git(mem, "rev-list", "--count", "HEAD").strip() == "1"


def test_confirmed_ticket_analyses_are_remembered_and_briefed(remembered, tmp_path):
    mem = remembered.memory
    # Approved: power (OEM-101, OEM-606), BT (OEM-202), brightness (OEM-505, OEM-808).
    assert remembered.report.tickets == 5
    bt = yaml.safe_load((mem.path / "tickets/OEM-202.yaml").read_text())
    (entry,) = bt["history"]
    assert entry["understanding"] == "understanding of OEM-202"
    assert (entry["relevance"], entry["approach"]) == ("needed", "cherry_pick")
    assert entry["solution"] == "Moved file only."
    assert entry["related"] == ["OEM-101 (related): both"]
    assert entry["notes"] == ["BT timeout is 12 s on all head units."]
    assert entry["outcome"] == "verified" and entry["prs"] and entry["approved_by"] == ["xuan"]
    assert not (mem.path / "tickets/OEM-303.yaml").exists()  # dropped, not confirmed

    nxt = Project(tmp_path / "next", memory={"path": str(remembered.memory_path)})
    asked: list = []
    analyse_items(nxt.config, nxt.store, nxt.run, nxt.runner(analyst(asked=asked)),
                  memory=Memory.open(nxt.config))  # fmt: skip
    (brief,) = [t.brief for t in asked if t.key == "item:OEM-202"]
    section = brief.split("## From earlier migrations (migration memory)")[1]
    assert "mig-A14-A15 solution (cherry_pick): Moved file only." in section
    assert "engineer note: BT timeout is 12 s on all head units." in section


def test_history_is_found_through_original_change_id(remembered):
    mem = remembered.memory
    cherry = Patch(subject="Add OEM power mode", fingerprint="other",
                   message=f"Add OEM power mode\n\nOriginal-Change-Id: {POWER_ID}\n",
                   change_id="I" + "f" * 40)  # fmt: skip
    assert change_ids(cherry) == ["I" + "f" * 40, POWER_ID]
    assert mem.history(cherry)["key"] == POWER_ID
    assert mem.history(Patch(subject="x", fingerprint="none", message="")) is None


def test_open_needs_a_repo_unless_created(tmp_path):
    p = Project(tmp_path, memory={"path": str(tmp_path / "mem")})
    with pytest.raises(MemoryRepoError, match="not a git repository"):
        Memory.open(p.config)
    assert (Memory.open(p.config, create=True).path / ".git").exists()


def test_next_migration_briefs_the_analyst_and_the_agent(remembered, tmp_path):
    lesson = remembered.memory.propose(
        "OEM callers pass REASON_OEM", "CarPowerService.setMode takes a reason; OEM code uses "
        "REASON_OEM.", [f"{FRAMEWORKS}/{POWER}"], {},
    )  # fmt: skip
    remembered.memory.approve(lesson.name, "xuan")
    remembered.memory.propose("Unrelated", "Only for audio.", ["frameworks/av/**"], {})

    nxt = Project(tmp_path / "next", memory={"path": str(remembered.memory_path)})
    mem = Memory.open(nxt.config)
    asked: list = []
    analyse_items(nxt.config, nxt.store, nxt.run, nxt.runner(analyst(asked=asked)), memory=mem)
    (power,) = [t.brief for t in asked if t.key == "item:OEM-101"]
    remembered_text = power.split("## From earlier migrations (migration memory)")[1]
    assert "in mig-A14-A15 verified" in remembered_text

    briefs = []

    def agent(task, attempt):
        briefs.append(task.brief)
        return resolve_power(task, attempt)

    nxt.migrate(nxt.runner(agent), memory=mem, cluster_ids=[nxt.cluster_of("P1").id])
    (brief,) = [b for b in briefs if "Add OEM power mode" in b]
    section = brief.split("## From earlier migrations (migration memory)")[1]
    assert "in mig-A14-A15 verified" in section
    assert section.count("reviewer: Pass REASON_OEM") == 1  # once per cluster, not per patch
    assert "Lesson (OEM callers pass REASON_OEM)" in section
    assert "Only for audio" not in section  # proposed, and for other paths


def test_curator_proposes_lessons_from_feedback_with_one_call(remembered):
    r = remembered
    assert any("Pass REASON_OEM" in s for s in signals(r.store, r.run))
    seen = []

    def curator(task, attempt):
        seen.append(task)
        assert "Pass REASON_OEM" in task.brief
        lesson = {
            "title": "Use REASON_OEM for OEM setMode calls",
            "text": "Android 15 setMode takes a reason.",
            "applies_to": ["packages/services/Car/**"],
            "clusters": ["power"],
        }
        return {"lessons": [lesson]}

    fake = FakeAdapter(curator)
    runner = AgentRunner(fake, r.store, r.run, r.config.llm, r.config.budget)
    out = curate(r.config, r.store, r.run, runner, r.memory)
    assert len(seen) == 1 and out.proposed == ["use-reason-oem-for-oem-setmode-calls"]
    (lesson,) = r.memory.lessons("proposed")
    assert lesson.applies_to == ["packages/services/Car/**"] and lesson.meta["run"] == r.run.id
    assert r.memory.lessons("approved") == []

    cli = CliRunner()
    args = ["-c", str(r.config_path)]
    res = cli.invoke(app, ["memory", "list", "--proposed", *args])
    assert "Use REASON_OEM" in res.output
    res = cli.invoke(app, ["memory", "approve", lesson.name, "--by", "xuan", *args])
    assert res.exit_code == 0, res.output
    (approved,) = r.memory.lessons("approved")
    assert approved.meta["approved_by"] == "xuan"
    res = cli.invoke(app, ["memory", "reject", "nope", "--by", "xuan", *args])
    assert res.exit_code == 1 and "no proposed lesson" in res.output


def test_curator_makes_no_call_without_signals(tmp_path):
    p = Project(tmp_path, memory={"path": str(tmp_path / "mem")})
    fake = FakeAdapter(lambda t, a: pytest.fail("no call expected"))
    runner = AgentRunner(fake, p.store, p.run, p.config.llm, p.config.budget)
    out = curate(p.config, p.store, p.run, runner, Memory.open(p.config, create=True))
    assert out.signals == 0 and fake.calls == []


def test_report_and_memory_record_cli(remembered):
    r = remembered
    text = run_report(r.store, r.run.id)
    assert text.startswith("# migrate-test: Android 14 → 15")
    assert "## Patches" in text and "| verified |" in text
    assert "## Build and test" in text and "module builds" in text
    assert "| cluster_agent |" in text and "| reviewer |" in text
    assert "| Confidence | Clusters |" in text  # levels of the shared scale

    cli = CliRunner()
    out = r.config.workdir / "report.md"
    res = cli.invoke(app, ["report", "-c", str(r.config_path), "--out", str(out)])
    assert res.exit_code == 0, res.output
    assert out.read_text() == text
    res = cli.invoke(app, ["memory", "record", "-c", str(r.config_path)])
    assert res.exit_code == 0 and "(nothing new)" in res.output


def test_needs_a_person_lists_open_questions(tmp_path, monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "test-token")
    p = Published(tmp_path, verify=False)
    p.migrate(p.runner(lambda t, a: answer("needs_decision", question="Keep mode on suspend?")))
    text = run_report(p.store, p.run.id)
    assert "## Needs a person" in text and "Keep mode on suspend?" in text
    assert p.cluster_of("P1").state == State.NEEDS_DECISION


def test_lesson_names_cannot_leave_the_lessons_folder(tmp_path):
    mem = Memory(tmp_path / "mem")
    (tmp_path / "mem").mkdir()
    victim = tmp_path / "notes.md"
    victim.write_text("keep me")
    with pytest.raises(MemoryRepoError, match="no proposed lesson"):
        mem.reject("../../../notes", "mallory")
    assert victim.read_text() == "keep me"
