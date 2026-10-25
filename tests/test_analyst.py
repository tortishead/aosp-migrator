"""Workflow step 3: Copilot analyses each backlog item, and again after new information."""

from __future__ import annotations

import pytest

from patchmig import backlog as bl
from patchmig.agents.analyst import analyse_items, stale_items
from patchmig.ingest import ingest
from patchmig.ledger import BacklogItem, Cluster, State
from tests.fixtures.analyst import analyst
from tests.test_migrate import Project


@pytest.fixture
def project(tmp_path) -> Project:
    return Project(tmp_path)


def _item(project: Project, key: str) -> BacklogItem:
    return bl.get(project.store, project.run, key)


def _analyse(project: Project, respond, **kw):
    return analyse_items(project.config, project.store, project.run, project.runner(respond), **kw)


def test_a_confident_already_in_target_drops_the_patch_and_its_cluster(project):
    evidence = [{"project": "frameworks/base", "path": "x.java", "snippet": "timeout = 12"}]
    report = _analyse(project, analyst(by_key={"OEM-202": {
        "relevance": "already_in_target", "target_evidence": evidence}}))  # fmt: skip
    p3 = project.patch("P3")
    assert report.moved == [f"#{p3.id} needed -> upstream"]
    assert p3.state == State.UPSTREAM and p3.verdict["rung"] == "analyst"
    assert p3.verdict["evidence"]["target"] == evidence
    assert p3.verdict["before_analyst"]["verdict"] == "needed"
    assert project.cluster_of("P3").state == State.UPSTREAM
    assert project.cluster_of("P3").net_diff is None  # nothing left to migrate

    # New information: the engineer says otherwise; the item is stale and analysed again.
    bl.add_info(project.config, project.store, project.run, "OEM-202", "xuan",
                text="The target still uses 5 s; we checked on the bench.")  # fmt: skip
    assert [x.name for x in stale_items(project.store, project.run)] == ["OEM-202"]
    asked: list = []
    report = _analyse(project, analyst(asked=asked))
    assert [t.key for t in asked] == ["item:OEM-202"]
    assert "we checked on the bench" in asked[0].brief
    assert "Your earlier analysis of this item" in asked[0].brief
    p3 = project.patch("P3")
    assert p3.state == State.NEEDED and p3.verdict["verdict"] == "needed"
    assert "rung" in p3.verdict and p3.verdict["rung"] is None  # what the checks said
    assert project.cluster_of("P3").state == State.NEEDED
    item = _item(project, "OEM-202")
    assert item.analysis["relevance"] == "needed" and len(item.analyses) == 1
    assert item.analyses[0]["relevance"] == "already_in_target"  # kept for comparison


def test_unsure_or_missing_information_asks_a_human(project):
    _analyse(project, analyst(by_key={
        "OEM-202": {"relevance": "unclear", "missing_information": ["Which head units?"]},
        "OEM-606": {"relevance": "obsolete", "confidence": 0.3},
    }))  # fmt: skip
    p3, p8 = project.patch("P3"), project.patch("P8")
    assert p3.state == State.NEEDS_DECISION and p3.verdict["question"] == "Which head units?"
    assert p8.state == State.NEEDS_DECISION and "thinks obsolete" in p8.verdict["question"]
    assert project.cluster_of("P3").state == State.NEEDS_DECISION


def test_checks_outrank_and_migrated_clusters_stay(project):
    # P7 is upstream by identity: the analyst disagreeing only leaves a note on it.
    _analyse(project, analyst(), keys=["OEM-505"])
    p7 = project.patch("P7")
    assert p7.state == State.UPSTREAM and p7.verdict["rung"] == "identity"
    assert p7.verdict["analyst_disagrees"] == "reason for OEM-505"

    power = project.cluster_of("P1")
    with project.store.session() as s, s.begin():
        row = s.get(Cluster, power.id)
        row.result = {**row.result, "migration": {"state": "applied"}}
    _analyse(project, analyst(relevance="obsolete"), keys=["OEM-101"])
    assert project.patch("P1").state == State.NEEDED  # its cluster is migrated
    assert "Still needed: obsolete" in project.patch("P1").intent  # the intent still changes


def test_related_items_are_checked_and_a_ticket_without_patch_is_analysed(project):
    ingest(project.config, project.store, project.run, items=["OEM-202"])
    related = [
        {"ref": "OEM-606", "relation": "related", "why": "both power"},
        {"ref": "OEM-999", "relation": "duplicate", "why": "made up"},
        {"ref": f"#{project.patch('P1').id}", "relation": "depends_on", "why": "API"},
    ]
    _analyse(project, analyst(by_key={"OEM-101": {"related": related}}))
    found = _item(project, "OEM-101").analysis["related"]
    assert [(r["ref"], r["known"]) for r in found] == [
        ("OEM-606", True),
        ("OEM-999", False),
        (f"#{project.patch('P1').id}", True),
    ]

    # A ticket item with no patch at all is always worth asking about.
    with project.store.session() as s, s.begin():
        s.add(BacklogItem(run_id=project.run.id, key="OEM-990", kind="ticket",
                          title="Wake-up reason", added=[], analyses=[]))  # fmt: skip
    asked: list = []
    _analyse(
        project,
        analyst(
            asked=asked,
            relevance="needed",
            solution={"approach": "needs_patch", "summary": "write one"},
        ),
    )
    assert [t.key for t in asked] == ["item:OEM-990"]
    assert "no patch found for this ticket" in asked[0].brief
    assert _item(project, "OEM-990").analysis["solution"]["approach"] == "needs_patch"


def test_the_step_runs_the_checks_then_copilot(project):
    from patchmig import steps

    lines: list[str] = []
    ctx = steps.Context(project.config_path, project.config, project.store,
                        lambda text, level: lines.append(text),
                        adapter=lambda cfg: project.fake)  # fmt: skip
    project.runner(analyst())  # sets project.fake
    steps.analyse(ctx)
    assert any(x.startswith("Copilot analysis: 3 items analysed") for x in lines)
    lines.clear()
    steps.analyse(ctx, items="OEM-101")
    assert any(x.startswith("Copilot analysis: 1 items analysed") for x in lines)
    with pytest.raises(steps.StepFailed, match="not in the backlog"):
        steps.analyse(ctx, items="OEM-999")


def test_the_solution_reaches_the_agents_that_build_it(project):
    """Workflow step 4: the cluster agent and the reviewer get the analysis of the tickets."""
    from tests.test_migrate import resolve_power

    solution = {"approach": "adapt", "summary": "Pass a reason to setMode.",
                "steps": ["Add the reason argument"], "files": ["frameworks/base/x.java"],
                "risks": ["Callers in Car"]}  # fmt: skip
    _analyse(project, analyst(by_key={"OEM-101": {"solution": solution}}))
    project.migrate(project.runner(resolve_power), cluster_ids=[project.cluster_of("P1").id])
    briefs = {t.agent: t.brief for t, _ in project.fake.calls}
    for agent in ("cluster_agent", "reviewer"):
        section = briefs[agent].split("## The analysis of these tickets (before migration)")[1]
        assert "Solution (adapt): Pass a reason to setMode." in section
        assert "1. Add the reason argument" in section and "Risk: Callers in Car" in section
