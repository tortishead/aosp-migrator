from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from patchmig.analysis import analyse
from patchmig.cli import app as cli
from patchmig.config import load_config
from patchmig.ingest import ingest
from patchmig.ledger import LedgerStore
from patchmig.web.api import create_app
from patchmig.web.export import export_site
from tests.fixtures.scenario import EXPECTED, FRAMEWORKS
from tests.test_ingest import make_config  # noqa: F401  (fixture)


@pytest.fixture
def project(make_config):  # noqa: F811
    path = make_config([{"type": "git_range"}])
    cfg = load_config(path)
    store = LedgerStore.open(cfg.ledger_path)
    run = store.create_run(cfg, note="ingest")
    ingest(cfg, store, run)
    analyse(cfg, store, run)
    empty = store.create_run(cfg, note="not ingested yet")
    return path, cfg, store, run, empty


@pytest.fixture
def client(project):
    _, cfg, store, *_ = project
    return TestClient(create_app(cfg, store))


def test_runs_and_summary(client, project):
    *_, run, empty = project
    runs = client.get("/api/runs").json()
    assert [r["id"] for r in runs] == [empty.id, run.id]  # newest first
    assert runs[1]["patches"] == len(EXPECTED) and runs[1]["clusters"] == 6
    s = client.get(f"/api/runs/{run.id}/summary").json()
    assert s["project"] == "ingest-test" and s["migration"]["target_version"] == "15"
    assert s["states"] == {"needed": 4, "upstream": 3, "obsolete": 1, "reverted": 2}
    assert s["forecasts"] == {"rewrite": 1, "clean": 1}
    assert s["rungs"]["identity"] == 1 and s["credits"] == 0
    assert client.get(f"/api/runs/{empty.id}/summary").json()["patches"] == 0
    assert client.get("/api/runs/999/summary").status_code == 404


def test_patches_and_patch_detail(client, project):
    *_, run, _ = project
    patches = client.get(f"/api/runs/{run.id}/patches").json()
    assert len(patches) == len(EXPECTED) and patches[0]["subject"] == "Add OEM power mode"
    p7 = next(p for p in patches if p["subject"] == "Enable auto brightness by default")
    assert (p7["state"], p7["rung"]) == ("upstream", "identity")
    detail = client.get(f"/api/runs/{run.id}/patches/{p7['id']}").json()
    assert detail["verdict"]["evidence"]["match"] == "patch-id"
    assert '+    <bool name="config_autoBrightness">true</bool>' in detail["diff"]
    assert detail["history"][0]["to"] == "upstream"
    assert client.get(f"/api/runs/{run.id + 1}/patches/{p7['id']}").status_code == 404


def test_clusters_knowledge_and_more(client, project):
    *_, run, _ = project
    clusters = client.get(f"/api/runs/{run.id}/clusters").json()
    first = clusters[0]
    assert first["forecast"] == "rewrite" and len(first["members"]) == 3
    assert first["projects"] == sorted([FRAMEWORKS, "packages/services/Car"])
    c = client.get(f"/api/runs/{run.id}/clusters/{first['id']}").json()
    assert {e["kind"] for e in c["edges"]} == {"jira", "symbol"}
    assert c["net_diff"].startswith("diff --git a/frameworks/base/")
    assert [m["seq"] for m in c["members"]] == sorted(m["seq"] for m in c["members"])
    k = client.get(f"/api/runs/{run.id}/knowledge").json()
    assert k["fate_counts"]["renamed"] == 1 and FRAMEWORKS in k["footprints"]
    assert client.get(f"/api/runs/{run.id}/agent-calls").json() == []
    activity = client.get(f"/api/runs/{run.id}/activity").json()
    assert activity[0]["entity"] == "cluster" and activity[0]["at"].endswith("+00:00")
    config = client.get(f"/api/runs/{run.id}/config").json()
    assert config["config"]["name"] == "ingest-test"


def test_ui_is_served(client):
    page = client.get("/")
    assert page.status_code == 200 and 'data-mode="live"' in page.text
    assert client.get("/static/app.js").status_code == 200
    assert client.get("/static/htm-preact.js").status_code == 200


def test_static_export_matches_the_api(client, project, tmp_path: Path):
    _, cfg, store, run, empty = project
    out = tmp_path / "site"
    n = export_site(cfg, store, out)
    assert 'data-mode="static"' in (out / "index.html").read_text()
    assert (out / "static" / "app.js").exists()
    for rel in ("runs", f"runs/{run.id}/summary", f"runs/{run.id}/clusters",
                f"runs/{run.id}/knowledge", f"runs/{empty.id}/patches"):  # fmt: skip
        exported = json.loads((out / "data" / f"{rel}.json").read_text())
        assert exported == client.get(f"/api/{rel}").json(), rel
    patches = client.get(f"/api/runs/{run.id}/patches").json()
    for p in patches:
        assert (out / "data" / f"runs/{run.id}/patches/{p['id']}.json").exists()
    # runs.json, 8 views per run, and one file per patch and per cluster.
    assert n == 1 + 2 * 8 + len(patches) + 6
    with pytest.raises(FileExistsError):
        export_site(cfg, store, out)


def test_cli_export_site_for_one_run(project, tmp_path: Path):
    path, _, _, run, _ = project
    out = tmp_path / "pages"
    result = CliRunner().invoke(
        cli, ["export-site", str(out), "-c", str(path), "--run", str(run.id)]
    )
    assert result.exit_code == 0, result.output
    assert "python -m http.server" in result.output
    runs = json.loads((out / "data" / "runs.json").read_text())
    assert [r["id"] for r in runs] == [run.id]


def test_review_view_reads_runs_of_other_versions(client, project):
    """A run's config snapshot may hold thresholds fields this version no longer knows."""
    from patchmig.ledger import Run

    _, _, store, run, _ = project
    with store.session() as s, s.begin():
        row = s.get(Run, run.id)
        thresholds = {**row.config_snapshot["thresholds"], "retired_field": 1}
        row.config_snapshot = {**row.config_snapshot, "thresholds": thresholds}
    response = client.get(f"/api/runs/{run.id}/review")
    assert response.status_code == 200 and "queues" in response.json()
