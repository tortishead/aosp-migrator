"""Engineer notes: scopes, briefs, and reruns of only what a note change affects."""

from __future__ import annotations

import pytest
from sqlalchemy import select
from typer.testing import CliRunner

from patchmig import notes
from patchmig.agents.fake import FakeAdapter
from patchmig.agents.intent import write_intents
from patchmig.agents.runner import AgentRunner
from patchmig.cli import app
from patchmig.ledger import Cluster, State, Transition
from patchmig.memory import patch_key
from tests.fixtures.scenario import BT_OLD, FRAMEWORKS
from tests.test_migrate import Project, answer, approve, resolve_power

MCP = {"type": "local", "command": "true"}


def writer(asked: list):
    def respond(task, attempt):
        asked.append(task)
        return {"problem": "p", "change": "c", "verification": "v", "components": [],
                "sources": ["diff"], "confidence": 0.7}  # fmt: skip

    return respond


@pytest.fixture
def project(tmp_path) -> Project:
    return Project(tmp_path)


def runner_for(project: Project, respond) -> AgentRunner:
    project.fake = FakeAdapter(respond)
    return AgentRunner(
        project.fake, project.store, project.run, project.config.llm, project.config.budget
    )


def test_scopes_editing_and_removal(project):
    p1, p3 = project.patch("P1"), project.patch("P3")
    power = project.cluster_of("P1")
    store = project.store
    everyone = notes.add(store, "Target is BMW QSSI 15.", "xuan")
    path = notes.add(store, "BT moved to packages/modules.", "xuan", "path",
                     f"{FRAMEWORKS}/services/*/BtManager.java")  # fmt: skip
    one = notes.add(store, "Keep the old overload.", "xuan", "patch", patch_key(p1),
                    stages=["migrate"])  # fmt: skip
    cluster = notes.add(store, "Power cluster: check CarPowerService.", "xuan", "cluster",
                        str(power.id))  # fmt: skip
    ticket = notes.add(store, "OEM-101 is China only.", "xuan", "ticket", "OEM-101")
    active = notes.active(store)

    def ids(found):
        return [n.id for n in found]

    assert ids(notes.for_patch(active, p1, "analyse")) == [everyone.id, cluster.id, ticket.id]
    assert ids(notes.for_patch(active, p1, "migrate")) == [
        everyone.id, one.id, cluster.id, ticket.id]  # fmt: skip
    assert ids(notes.for_patch(active, p3, "migrate")) == [everyone.id, path.id]
    assert BT_OLD.endswith("/BtManager.java")

    with pytest.raises(notes.NoteError):
        notes.add(store, "x", "xuan", "path")  # a path note needs its glob
    with pytest.raises(notes.NoteError):
        notes.add(store, "x", "xuan", stages=["build"])

    edited = notes.edit(store, ticket.id, "anna", text="OEM-101 is for all variants.")
    assert edited.text == "OEM-101 is for all variants." and edited.author == "anna"
    assert edited.revisions[0]["text"] == "OEM-101 is China only."
    notes.remove(store, path.id, "anna")
    assert path.id not in ids(notes.active(store))
    with pytest.raises(notes.NoteError):
        notes.edit(store, path.id, "anna", text="again")


def test_intents_get_notes_and_only_stale_ones_are_rewritten(project):
    store, run = project.store, project.run
    notes.add(store, "Target is BMW QSSI 15.", "xuan", stages=["analyse"])
    asked: list = []
    report = write_intents(project.config, store, run, runner_for(project, writer(asked)))
    assert report.written == 4
    assert all("Notes from the migration engineers" in t.brief for t in asked)
    assert all("Target is BMW QSSI 15." in t.brief for t in asked)
    assert notes.stale_intents(store, run) == []

    # A note on one patch makes only that intent stale; a migrate-only note none.
    p3 = project.patch("P3")
    note = notes.add(store, "Slow phones need 12 s.", "xuan", "patch", patch_key(p3))
    notes.add(store, "Only for the agent.", "xuan", "ticket", "OEM-101", stages=["migrate"])
    assert [x.id for x in notes.stale_intents(store, run)] == [p3.id]
    asked.clear()
    report = write_intents(project.config, store, run, runner_for(project, writer(asked)),
                           stale=True)  # fmt: skip
    assert report.written == 1 and [t.subject_id for t in asked] == [p3.id]
    assert "Slow phones need 12 s." in asked[0].brief
    assert notes.stale_intents(store, run) == []

    # Removing the note makes it stale again; --patch rewrites one on demand.
    notes.remove(store, note.id, "xuan")
    assert [x.id for x in notes.stale_intents(store, run)] == [p3.id]
    asked.clear()
    p1 = project.patch("P1")
    write_intents(project.config, store, run, runner_for(project, writer(asked)), redo=[p1.id])
    assert [t.subject_id for t in asked] == [p1.id]


def test_intent_reuse_needs_the_same_notes(project):
    store = project.store
    asked: list = []
    write_intents(project.config, store, project.run, runner_for(project, writer(asked)))
    from patchmig.ingest import ingest

    def next_run():
        run = store.create_run(project.config)
        ingest(project.config, store, run)
        return run

    report = write_intents(project.config, store, next_run(), runner_for(project, writer(asked)))
    assert report.reused == 4  # the first run wrote only the needed patches' intents
    asked.clear()
    report = write_intents(project.config, store, next_run(), runner_for(project, writer(asked)))
    assert report.written == 0 and report.reused == 10  # same patches: free

    notes.add(store, "Target is BMW QSSI 15.", "xuan")
    asked.clear()
    run = next_run()
    project.run = run
    report = write_intents(project.config, store, run, runner_for(project, writer(asked)))
    assert report.reused == 0 and len(asked) == 10  # not analysed: nothing is dropped


def test_cluster_agent_reviewer_get_notes_and_stale_cluster_reruns(project):
    store, run = project.store, project.run
    power = project.cluster_of("P1")
    bt = project.cluster_of("P3")
    general = notes.add(store, "Target is BMW QSSI 15.", "xuan", stages=["migrate"])
    project.migrate(project.runner(resolve_power, approve(0.8)), mcp=MCP)
    agent_brief, review_brief = (t.brief for t, _ in project.fake.calls)
    assert "Target is BMW QSSI 15." in agent_brief and "Target is BMW QSSI 15." in review_brief
    power = project.cluster_of("P1")
    assert power.state == State.RESOLVED
    record = power.result["migration"]
    assert record["knowledge"]["notes"] == [general.id]
    assert sorted(record["members"]) == sorted(project.patch(k).id for k in ("P1", "P2", "P8"))
    assert project.cluster_of("P3").state == State.APPLIED  # cherry-picked, no agent
    assert notes.stale_clusters(store, run) == []

    # A general note change: only the cluster the agent worked on is stale.
    notes.edit(store, general.id, "xuan", text="Target is BMW QSSI 15, API level 35.")
    assert [(x.id, x.why) for x in notes.stale_clusters(store, run)] == [
        (power.id, "its notes or intents changed")]  # fmt: skip
    # A note about the cherry-picked cluster makes it stale too, and needs the agent.
    notes.add(store, "Use the 12 s timeout from the OEM spec.", "xuan", "cluster", str(bt.id))
    stale = notes.stale_clusters(store, run)
    assert [x.id for x in stale] == [power.id, bt.id]

    reopened, messages = notes.rerun(store, run, [x.id for x in stale], "human:xuan",
                                     "knowledge changed")  # fmt: skip
    assert reopened == [power.id, bt.id] and messages == []
    assert project.cluster_of("P1").state == State.NEEDED
    assert all(project.patch(k).state == State.NEEDED for k in ("P1", "P2", "P3", "P8"))
    with store.session() as s:
        last = s.scalars(select(Transition).where(Transition.entity_type == "cluster",
                                                  Transition.entity_id == bt.id)
                         .order_by(Transition.id.desc())).first()  # fmt: skip
    assert last.actor == "human:xuan" and last.reason == "knowledge changed"

    def agent(task, attempt):
        if f"cluster:{bt.id}:" in task.key:
            assert "Use the 12 s timeout" in task.brief
            assert "API level 35" in task.brief
            return answer("resolved", files=[])
        return resolve_power(task, attempt)

    project.migrate(project.runner(agent, approve(0.8)), mcp=MCP)
    keys = [t.key for t, _ in project.fake.calls]
    assert f"cluster:{bt.id}:migrate:1" in keys  # the note brought the agent in
    assert notes.stale_clusters(store, run) == []


def test_rerun_takes_dependents_along_and_spares_approved(project):
    store, run = project.store, project.run
    project.migrate(project.runner(resolve_power, approve(0.8)), mcp=MCP)
    with store.session() as s:
        clusters = s.scalars(select(Cluster).where(Cluster.run_id == run.id)).all()
    power = project.cluster_of("P1")
    # Make the BT cluster depend on the power cluster, as if it were built on it.
    bt = project.cluster_of("P3")
    with store.session() as s, s.begin():
        row = s.get(Cluster, bt.id)
        row.result = {**row.result, "depends_on_order": [power.order_index]}
    assert len(clusters) > 2
    reopened, _ = notes.rerun(store, run, [power.id], "orchestrator", "rerun requested")
    assert set(reopened) == {power.id, bt.id}

    project.migrate(project.runner(resolve_power, approve(0.8)), mcp=MCP)
    with store.session() as s, s.begin():
        row = s.get(Cluster, bt.id)
        row.result = {**row.result, "review": {"decision": "approved", "by": "xuan"}}
    reopened, messages = notes.rerun(store, run, [power.id], "orchestrator", "rerun requested")
    assert reopened == [power.id] and "approved" in messages[0]
    assert notes.stale_clusters(store, run) == []  # approved clusters are never stale

    with pytest.raises(notes.NoteError):  # analysed as dropped: nothing to rerun
        notes.rerun(store, run, [project.cluster_of("P4").id], "orchestrator", "x")


def test_notes_cli(project):
    cli = CliRunner()
    cfg = ["-c", str(project.config_path)]
    p3 = project.patch("P3")
    res = cli.invoke(app, ["notes", "add", "BT reconnect: 12 s on all variants.", "--by", "xuan",
                           "--patch", str(p3.id), "--for", "analyse", *cfg])  # fmt: skip
    assert res.exit_code == 0, res.output
    assert (
        f"for patch {patch_key(p3)} (#{p3.id} Tune BT reconnect timeout for car head units)"
        in res.output
    )
    res = cli.invoke(app, ["notes", "add", "x", "--by", "xuan", "--path", "a/*", "--ticket",
                           "OEM-1", *cfg])  # fmt: skip
    assert res.exit_code == 1 and "at most one" in res.output
    cli.invoke(app, ["notes", "add", "Everyone: QSSI target.", "--by", "xuan", *cfg])

    res = cli.invoke(app, ["notes", "list", "--patch", str(project.patch("P1").id), *cfg])
    assert "QSSI target" in res.output and "12 s" not in res.output
    res = cli.invoke(app, ["notes", "edit", "1", "--by", "anna", "--text", "13 s.", *cfg])
    assert res.exit_code == 0 and "13 s." in res.output and "edited 1x" in res.output
    res = cli.invoke(app, ["notes", "remove", "2", "--by", "anna", *cfg])
    assert res.exit_code == 0
    res = cli.invoke(app, ["notes", "list", "--removed", *cfg])
    assert "(removed)" in res.output

    assert cli.invoke(app, ["apply", "--no-agent", *cfg]).exit_code == 0
    res = cli.invoke(app, ["notes", "stale", *cfg])
    assert "nothing is stale" in res.output
    bt = project.cluster_of("P3")
    assert bt.state == State.APPLIED
    cli.invoke(app, ["notes", "add", "Use 14 s.", "--by", "anna", "--cluster", str(bt.id), *cfg])
    res = cli.invoke(app, ["notes", "stale", *cfg])
    assert "1 cluster(s)" in res.output and "a note about this cluster changed" in res.output
    res = cli.invoke(app, ["apply", "--stale", "--no-agent", "--by", "anna", *cfg])
    assert res.exit_code == 0, res.output
    assert "needs_agent" in res.output  # the note needs the agent, which is off here
    res = cli.invoke(app, ["apply", "--redo", *cfg])
    assert res.exit_code == 1 and "--redo needs --cluster" in res.output


def test_web_shows_notes_and_marks_changed_ones(project):
    from patchmig.web import data

    store = project.store
    first = notes.add(store, "Target is BMW QSSI 15.", "xuan", stages=["migrate"])
    project.migrate(project.runner(resolve_power, approve(0.8)), mcp=MCP)
    power = project.cluster_of("P1")
    view = data.cluster(store, project.run.id, power.id)
    assert [(n["id"], n["stale"]) for n in view["notes"]] == [(first.id, False)]
    later = notes.add(store, "OEM-101 is China only.", "xuan", "ticket", "OEM-101")
    view = data.cluster(store, project.run.id, power.id)
    assert [(n["id"], n["stale"]) for n in view["notes"]] == [(first.id, False), (later.id, True)]
    p1 = data.patch(store, project.config, project.run.id, project.patch("P1").id)
    assert [n["where"] for n in p1["notes"]] == ["all patches", "ticket OEM-101"]
