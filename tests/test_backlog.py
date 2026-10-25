"""Workflow step 2: the backlog lasts, and engineers add what the tickets leave out."""

from __future__ import annotations

import subprocess

import pytest
from typer.testing import CliRunner

from patchmig import backlog as bl
from patchmig.cli import app
from patchmig.config import load_config
from patchmig.ingest import IngestError, ingest
from patchmig.jira_client import Ticket
from patchmig.ledger import LedgerStore, Note
from tests.fixtures.scenario import FRAMEWORKS
from tests.test_ingest import (
    JIRA,
    FakeJira,
    N,
    make_config,  # noqa: F401  (fixture)
)
from tests.test_jira_ingest import ATTACHED


@pytest.fixture
def jira():
    return FakeJira(
        {
            "OEM-101": Ticket("OEM-101", "OEM power mode", "Needed for OEM sleep", "Done"),
            "OEM-950": Ticket(
                "OEM-950",
                "BT reconnect logging",
                "Log every reconnect",
                "Open",
                comments=["QA: needed for field logs"],
            ),  # fmt: skip
            "OEM-960": Ticket("OEM-960", "Wake-up reason", "Found later"),
        },
        search_results=["OEM-101", "OEM-950"],
    )


def _backlog(make_config, jira):  # noqa: F811
    cfg = load_config(make_config([{"type": "jira_query", "jql": "x"}], jira=JIRA))
    store = LedgerStore.in_memory()
    run = store.create_run(cfg)
    ingest(cfg, store, run, jira=jira)
    return cfg, store, run


def test_a_ticket_without_patch_is_an_item_and_gets_one_from_an_added_file(
    make_config,  # noqa: F811
    jira,
):
    cfg, store, run = _backlog(make_config, jira)
    item = bl.get(store, run, "OEM-950")
    assert item.kind == "ticket" and item.title == "BT reconnect logging"
    assert bl.item_patches(store, item) == []
    assert len(bl.item_patches(store, bl.get(store, run, "OEM-101"))) == 2  # P1, P2

    # An engineer adds the patch, a spec link and a hint; then the item is imported again.
    bl.add_info(cfg, store, run, "OEM-950", "ana", filename="bt.diff", content=ATTACHED)
    bl.add_info(cfg, store, run, "OEM-950", "ana", link="https://wiki.oem/bt-logging")
    done = bl.add_info(cfg, store, run, "OEM-950", "ana", text="Only on head units")
    report = ingest(cfg, store, run, jira=jira, items=["OEM-950"])
    assert report.new_patches == 1 and report.items_refreshed == 1, report.warnings
    (patch,) = bl.item_patches(store, bl.get(store, run, "OEM-950"))
    assert patch.sources == ["added"] and patch.project == FRAMEWORKS
    assert patch.subject == "BT reconnect logging"  # a plain diff: the ticket names it
    with store.session() as s:
        note = s.get(Note, done["note"])
    assert (note.scope, note.target) == ("ticket", "OEM-950")

    # Importing everything again changes nothing; a removed addition stays in the history.
    again = ingest(cfg, store, run, jira=jira)
    assert again.new_patches == 0 and again.items_new == []
    bl.remove_info(store, run, "OEM-950", 2, "ana")
    added = bl.get(store, run, "OEM-950").added
    assert added[1]["removed_by"] == "ana" and len(bl.active_additions(bl.get(
        store, run, "OEM-950"))) == 1  # fmt: skip


def test_a_new_ticket_by_key_and_wrong_input(make_config, jira):  # noqa: F811
    cfg, store, run = _backlog(make_config, jira)
    report = ingest(cfg, store, run, jira=jira, items=["OEM-960", "OEM-999"])
    assert report.items_new == ["OEM-960"]
    assert any("OEM-999" in w for w in report.warnings)
    with pytest.raises(bl.BacklogError, match="not in the backlog"):
        bl.get(store, run, "OEM-999")
    with pytest.raises(IngestError, match="not in the backlog"):
        ingest(cfg, store, run, jira=jira, items=["patch:77"])
    with pytest.raises(bl.BacklogError, match="one thing at a time"):
        bl.add_info(cfg, store, run, "OEM-960", "ana", text="a", link="https://x")
    with pytest.raises(bl.BacklogError, match="http"):
        bl.add_info(cfg, store, run, "OEM-960", "ana", link="file:///etc/passwd")
    entry = bl.add_info(cfg, store, run, "OEM-960", "ana", filename="../../x.log", content="E")
    assert entry["path"].endswith("OEM-960/1-x.log")


def test_a_patch_without_ticket_is_its_own_item(scenario, make_config, tmp_path):  # noqa: F811
    diffs = tmp_path / "diffs" / FRAMEWORKS
    diffs.mkdir(parents=True)
    diff = subprocess.run(["git", "show", "--format=", scenario.commits["P3"]],
                          cwd=scenario.source / FRAMEWORKS, capture_output=True, text=True,
                          check=True).stdout  # fmt: skip
    (diffs / "bt-timeout.diff").write_text(diff)
    cfg = load_config(make_config([{"type": "diff_folder", "path": str(tmp_path / "diffs")}]))
    store = LedgerStore.in_memory()
    run = store.create_run(cfg)
    ingest(cfg, store, run)
    (item,) = bl.items(store, run)
    assert item.kind == "patch" and item.title == "bt-timeout"
    (patch,) = bl.item_patches(store, item)
    assert item.key == f"patch:{patch.id}"
    note = bl.add_info(cfg, store, run, item.key, "ana", text="OEM only")
    with store.session() as s:
        assert s.get(Note, note["note"]).scope == "patch"


def test_backlog_from_the_command_line(make_config, tmp_path):  # noqa: F811
    path = make_config([{"type": "git_range"}])
    runner = CliRunner()
    assert runner.invoke(app, ["ingest", "-c", str(path), "--no-jira"]).exit_code == 0
    result = runner.invoke(app, ["backlog", "list", "-c", str(path)])
    assert result.exit_code == 0 and result.output.count("\n") == N - 2  # 8 tickets
    assert "OEM-101" in result.output and "imported" in result.output
    log = tmp_path / "crash.log"
    log.write_text("E/BtManager: reconnect")
    result = runner.invoke(app, ["backlog", "add", "OEM-202", "--by", "ana", "--file", str(log),
                                 "-c", str(path)])  # fmt: skip
    assert result.exit_code == 0, result.output
    assert "migrate ingest --item OEM-202" in result.output
    result = runner.invoke(app, ["backlog", "show", "OEM-202", "-c", str(path)])
    assert "added [1] file: crash.log by ana" in result.output
    result = runner.invoke(app, ["ingest", "-c", str(path), "--no-jira", "--item", "OEM-202"])
    assert result.exit_code == 0 and "0 new patches, 1 already there" in result.output
