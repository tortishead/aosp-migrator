"""Read-only web API and UI over one project's ledger.

`migrate serve` runs this app; the UI in static/ calls the /api routes. Review actions are
taken with `migrate review` or on the published PRs; editing in the browser needs sign-in.
"""

from __future__ import annotations

from importlib.resources import files
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from patchmig.config import ProjectConfig
from patchmig.ledger import LedgerStore
from patchmig.web import data

STATIC = Path(str(files("patchmig.web").joinpath("static")))


def index_html(static: bool) -> str:
    html = (STATIC / "index.html").read_text()
    mode = "static" if static else "live"
    return html.replace('data-mode="live"', f'data-mode="{mode}"')


def create_app(config: ProjectConfig | None, store: LedgerStore) -> FastAPI:
    app = FastAPI(title="patchmig", docs_url="/api/docs", openapi_url="/api/openapi.json")

    def get(fn, *args):
        try:
            return fn(*args)
        except data.NotFound as e:
            raise HTTPException(404, str(e)) from e

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    def index() -> str:
        return index_html(static=False)

    @app.get("/api/runs")
    def runs() -> list[dict]:
        return data.runs(store)

    @app.get("/api/runs/{run_id}/summary")
    def summary(run_id: int) -> dict:
        return get(data.summary, store, run_id)

    @app.get("/api/runs/{run_id}/patches")
    def patches(run_id: int) -> list[dict]:
        return get(data.patches, store, run_id)

    @app.get("/api/runs/{run_id}/patches/{patch_id}")
    def patch(run_id: int, patch_id: int) -> dict:
        return get(data.patch, store, config, run_id, patch_id)

    @app.get("/api/runs/{run_id}/clusters")
    def clusters(run_id: int) -> list[dict]:
        return get(data.clusters, store, run_id)

    @app.get("/api/runs/{run_id}/clusters/{cluster_id}")
    def cluster(run_id: int, cluster_id: int) -> dict:
        return get(data.cluster, store, run_id, cluster_id)

    @app.get("/api/runs/{run_id}/review")
    def review(run_id: int) -> dict:
        return get(data.review, store, run_id)

    @app.get("/api/runs/{run_id}/knowledge")
    def knowledge(run_id: int) -> dict:
        return get(data.knowledge, store, run_id)

    @app.get("/api/runs/{run_id}/agent-calls")
    def agent_calls(run_id: int) -> list[dict]:
        return get(data.agent_calls, store, run_id)

    @app.get("/api/runs/{run_id}/activity")
    def activity(run_id: int) -> list[dict]:
        return get(data.recent_transitions, store, run_id)

    @app.get("/api/runs/{run_id}/config")
    def config_view(run_id: int) -> dict:
        return get(data.config_snapshot, store, run_id)

    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    return app
