from __future__ import annotations

import csv
import subprocess
from pathlib import Path

import pytest
import yaml
from openpyxl import Workbook
from sqlalchemy import select
from typer.testing import CliRunner

from patchmig.cli import app
from patchmig.config import load_config
from patchmig.ingest import IngestError, collect, ingest
from patchmig.jira_client import Ticket
from patchmig.ledger import LedgerStore, Patch
from patchmig.ledger import Ticket as TicketRow
from tests.fixtures.scenario import (
    BT_OLD,
    CAR,
    EXPECTED,
    FRAMEWORKS,
    SOURCE_BASE_TAG,
    SOURCE_HEAD_TAG,
    TARGET_BASE_TAG,
    Scenario,
)


class FakeJira:
    def __init__(self, tickets: dict[str, Ticket], search_results: list[str] | None = None):
        self.tickets = tickets
        self.search_results = search_results or []

    def search(self, jql: str) -> list[Ticket]:
        return [self.tickets[k] for k in self.search_results]

    def get(self, key: str) -> Ticket | None:
        return self.tickets.get(key)

    def remote_links(self, key: str) -> list[dict]:
        return []

    def download(self, url: str, max_bytes: int) -> bytes:
        raise AssertionError("no attachments in this fake")


@pytest.fixture
def make_config(scenario: Scenario, tmp_path: Path):
    def _make(sources: list[dict], **extra) -> Path:
        cfg = {
            "name": "ingest-test",
            "migration": {"source_version": "14", "target_version": "15"},
            "trees": {
                "source": {
                    "path": str(scenario.source),
                    "manifest": ".repo/manifests/default.xml",
                    "base_ref": SOURCE_BASE_TAG,
                    "head_ref": SOURCE_HEAD_TAG,
                },
                "target": {"path": str(scenario.target), "base_ref": TARGET_BASE_TAG},
            },
            "patch_sources": sources,
            **extra,
        }
        path = tmp_path / "project.yaml"
        path.write_text(yaml.safe_dump(cfg))
        return path

    return _make


N = len(EXPECTED)  # patches in the scenario


def by_subject(patches):
    return {p.subject: p for p in patches}


def keyed(scenario: Scenario, patches):
    """Map scenario keys (P1..P10) to patches via commit SHA."""
    sha_to_key = {sha: key for key, sha in scenario.commits.items()}
    return {sha_to_key[p.commit_sha]: p for p in patches if p.commit_sha in sha_to_key}


def test_git_range_finds_all_patches_with_links(scenario, make_config):
    cfg = load_config(make_config([{"type": "git_range"}]))
    patches, found, ctx = collect(cfg)
    assert found == {"git_range": N}
    assert ctx.warnings == []
    p = keyed(scenario, patches)
    assert len(p) == N

    assert p["P1"].change_id == "I1000000000000000000000000000000000000001"
    assert p["P1"].jira_keys == ["OEM-101"]
    assert p["P1"].chain_role == "base"
    assert p["P2"].chain_role == "fix"
    assert p["P6"].chain_role == "revert"
    assert p["P6"].links["reverts"] == scenario.commits["P5"]
    assert p["P5"].links["reverted_by"] == [scenario.commits["P6"]]

    assert "isOemMode" in p["P1"].footprint.defined
    assert [f.path for f in p["P3"].footprint.files] == [BT_OLD]
    assert p["P3"].project == FRAMEWORKS and p["P4"].project == CAR
    assert all(x.fingerprint for x in patches)
    # Ordered oldest first.
    assert [x.commit_sha for x in patches] == [scenario.commits[f"P{i}"] for i in range(1, N + 1)]


def test_git_range_limited_to_projects(make_config):
    cfg = load_config(make_config([{"type": "git_range", "projects": [CAR, "nope/x"]}]))
    patches, _, ctx = collect(cfg)
    assert {p.project for p in patches} == {CAR}
    assert any("nope/x" in w for w in ctx.warnings)


def _format_patches(scenario: Scenario, out: Path) -> None:
    for project in (FRAMEWORKS, CAR):
        folder = out / project
        folder.mkdir(parents=True)
        subprocess.run(
            [
                "git",
                "format-patch",
                "-q",
                "-o",
                str(folder),
                f"{SOURCE_BASE_TAG}..{SOURCE_HEAD_TAG}",
            ],
            cwd=scenario.source / project,
            check=True,
        )


def test_diff_folder_matches_git_fingerprints(scenario, make_config, tmp_path):
    diffs = tmp_path / "diffs"
    _format_patches(scenario, diffs)
    cfg = load_config(make_config([{"type": "diff_folder", "path": str(diffs)}]))
    from_diffs, found, ctx = collect(cfg)
    assert found == {"diff_folder": N}, ctx.warnings
    git_cfg = load_config(make_config([{"type": "git_range"}]))
    from_git, _, _ = collect(git_cfg)
    assert {p.fingerprint for p in from_diffs} == {p.fingerprint for p in from_git}
    p1 = by_subject(from_diffs)["Add OEM power mode"]
    assert p1.jira_keys == ["OEM-101"] and p1.project == FRAMEWORKS
    assert p1.change_id == "I1000000000000000000000000000000000000001"


def test_plain_tree_relative_diff_is_mapped_to_project(scenario, make_config, tmp_path):
    diffs = tmp_path / "plain"
    diffs.mkdir()
    sha = scenario.commits["P3"]
    diff = subprocess.run(
        [
            "git",
            "show",
            "--format=",
            f"--src-prefix=a/{FRAMEWORKS}/",
            f"--dst-prefix=b/{FRAMEWORKS}/",
            sha,
        ],
        cwd=scenario.source / FRAMEWORKS,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    (diffs / "bt-timeout.diff").write_text(diff)
    (diffs / "empty.patch").write_text("nothing here\n")
    cfg = load_config(
        make_config([{"type": "diff_folder", "path": str(diffs)}, {"type": "git_range"}])
    )
    patches, found, ctx = collect(cfg)
    assert found == {"diff_folder": 1, "git_range": N}
    assert any("empty.patch" in w for w in ctx.warnings)
    # The plain diff merged into the git commit with the same patch-id.
    p3 = keyed(scenario, patches)["P3"]
    assert p3.sources == ["diff_folder", "git_range"]
    assert p3.diff_path.endswith("bt-timeout.diff")
    assert len(patches) == N


def test_spreadsheet_csv(scenario, make_config, tmp_path):
    sheet = tmp_path / "patches.csv"
    with sheet.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["Commit", "Repo", "Ticket"])
        w.writerow([scenario.commits["P1"][:10], FRAMEWORKS, "OEM-101"])
        w.writerow(["", "", "OEM-404"])  # Jira only: finds P5 and P6
        w.writerow(["deadbeef", "", ""])
        w.writerow(["", "", ""])
    src = {
        "type": "spreadsheet",
        "path": str(sheet),
        "columns": {"commit": "Commit", "project": "Repo", "jira": "Ticket"},
    }
    patches, found, ctx = collect(load_config(make_config([src])))
    assert sorted(keyed(scenario, patches)) == ["P1", "P5", "P6"]
    assert any("deadbeef not found" in w for w in ctx.warnings)
    assert any("neither a commit nor a Jira key" in w for w in ctx.warnings)


def test_spreadsheet_xlsx_enriches_git_range(scenario, make_config, tmp_path):
    wb = Workbook()
    ws = wb.active
    ws.append(["commit", "jira"])
    ws.append([scenario.commits["P3"], "OEM-999"])
    path = tmp_path / "patches.xlsx"
    wb.save(path)
    src = [{"type": "git_range"}, {"type": "spreadsheet", "path": str(path)}]
    patches, found, _ = collect(load_config(make_config(src)))
    assert found == {"git_range": N, "spreadsheet": 1}
    p3 = keyed(scenario, patches)["P3"]
    assert p3.jira_keys == ["OEM-202", "OEM-999"]
    assert p3.sources == ["git_range", "spreadsheet"]


JIRA = {
    "url": "https://jira.example",
    "project": "OEM",
    "token": "env:PATCHMIG_TEST_JIRA",
}


def test_jira_query_and_ticket_cache(scenario, make_config):
    fake = FakeJira(
        {
            "OEM-101": Ticket("OEM-101", "OEM power mode", "Needed for OEM sleep", "Done"),
            "OEM-606": Ticket("OEM-606", "Car service honours OEM mode", issue_links=["OEM-101"]),
        },
        search_results=["OEM-101"],
    )
    cfg = load_config(make_config([{"type": "jira_query", "jql": "x"}], jira=JIRA))
    store = LedgerStore.in_memory()
    run = store.create_run(cfg)
    report = ingest(cfg, store, run, jira=fake)
    assert report.total == 2  # P1 and P2 mention OEM-101
    assert report.tickets_fetched == 1
    with store.session() as s:
        ticket = s.get(TicketRow, "OEM-101")
    assert ticket.summary == "OEM power mode"


def test_ingest_stores_patches_and_refuses_second_ingest(scenario, make_config):
    cfg = load_config(make_config([{"type": "git_range"}], jira=JIRA))
    store = LedgerStore.in_memory()
    run = store.create_run(cfg)
    fake = FakeJira({"OEM-101": Ticket("OEM-101", "OEM power mode")})
    report = ingest(cfg, store, run, jira=fake)
    assert report.total == N
    assert report.roles == {"base": N - 2, "fix": 1, "revert": 1}
    assert sorted(report.tickets_missing) == [
        "OEM-202",
        "OEM-303",
        "OEM-404",
        "OEM-505",
        "OEM-606",
        "OEM-707",
        "OEM-808",
    ]
    with store.session() as s:
        rows = s.scalars(select(Patch).where(Patch.run_id == run.id).order_by(Patch.seq)).all()
    assert [r.commit_sha for r in rows] == [scenario.commits[f"P{i}"] for i in range(1, N + 1)]
    assert rows[0].footprint["files"][0]["path"] == "core/java/android/os/PowerHelper.java"
    with pytest.raises(IngestError, match=f"already has {N} patches"):
        ingest(cfg, store, run)


def test_missing_manifest_is_reported(make_config, scenario):
    path = make_config([{"type": "git_range"}])
    raw = yaml.safe_load(path.read_text())
    raw["trees"]["source"]["manifest"] = "missing.xml"
    path.write_text(yaml.safe_dump(raw))
    with pytest.raises(IngestError, match="cannot read manifest"):
        collect(load_config(path))


def test_cli_ingest_and_list(make_config):
    path = make_config([{"type": "git_range"}])
    runner = CliRunner()
    result = runner.invoke(app, ["ingest", "-c", str(path), "--no-jira"])
    assert result.exit_code == 0, result.output
    assert f"Run 1: {N} patches" in result.output
    assert "revert 1" in result.output
    result = runner.invoke(app, ["patches", "-c", str(path)])
    assert result.exit_code == 0, result.output
    assert 'Revert "Log car power transitions"' in result.output
    assert result.output.count("\n") == N
