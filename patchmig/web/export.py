"""Static export: the UI plus every API answer as JSON files, for any static web host.

The result is read-only and frozen at export time: for example, a private GitHub Pages site
for reviewers without access to the orchestrator machine. It must be served over HTTP
(browsers block fetch() from file:// pages); `python -m http.server` works for a local look.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from patchmig.config import ProjectConfig
from patchmig.ledger import LedgerStore
from patchmig.web import data
from patchmig.web.api import STATIC, index_html


def _write(root: Path, rel: str, value) -> None:
    path = root / "data" / f"{rel}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, separators=(",", ":")))


def export_site(
    config: ProjectConfig | None, store: LedgerStore, out: Path, run_ids: list[int] | None = None
) -> int:
    """Write the site to `out`; returns the number of data files written."""
    if out.exists() and any(out.iterdir()):
        raise FileExistsError(f"{out} is not empty")
    out.mkdir(parents=True, exist_ok=True)
    shutil.copytree(STATIC, out / "static")
    (out / "index.html").write_text(index_html(static=True))

    all_runs = data.runs(store)
    selected = [r for r in all_runs if run_ids is None or r["id"] in run_ids]
    count = 0

    def write(rel: str, value) -> None:
        nonlocal count
        _write(out, rel, value)
        count += 1

    write("runs", selected)
    for r in selected:
        rid = r["id"]
        base = f"runs/{rid}"
        write(f"{base}/summary", data.summary(store, rid))
        write(f"{base}/knowledge", data.knowledge(store, rid))
        write(f"{base}/agent-calls", data.agent_calls(store, rid))
        write(f"{base}/activity", data.recent_transitions(store, rid))
        write(f"{base}/config", data.config_snapshot(store, rid))
        write(f"{base}/review", data.review(store, rid))
        patches = data.patches(store, rid)
        write(f"{base}/patches", patches)
        for p in patches:
            write(f"{base}/patches/{p['id']}", data.patch(store, config, rid, p["id"]))
        clusters = data.clusters(store, rid)
        write(f"{base}/clusters", clusters)
        for c in clusters:
            write(f"{base}/clusters/{c['id']}", data.cluster(store, rid, c["id"]))
    return count
