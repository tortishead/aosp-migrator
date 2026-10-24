"""Web API and UI over one project's ledger, and the project's settings.

`migrate serve` runs this app; the UI in static/ calls the /api routes. The ledger views are
read-only: review actions are taken with `migrate review` or on the published PRs. The
settings can be edited in the browser: every write (and every connection test, which uses
the stored credentials) needs the admin token as a Bearer token.
"""

from __future__ import annotations

import hmac
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from patchmig.config import ConfigError, ProjectConfig, load_config
from patchmig.config.editor import SettingsEditor, SettingsError
from patchmig.ledger import LedgerStore
from patchmig.web import data

STATIC = Path(str(files("patchmig.web").joinpath("static")))


def index_html(static: bool) -> str:
    html = (STATIC / "index.html").read_text()
    mode = "static" if static else "live"
    return html.replace('data-mode="live"', f'data-mode="{mode}"')


@dataclass
class Workspace:
    """What the app serves; replaced in place when the settings are saved."""

    config: ProjectConfig | None
    store: LedgerStore | None

    def reload(self, path: Path) -> str | None:
        try:
            config = load_config(path)
        except ConfigError as e:
            return str(e)
        if (
            self.store is None
            or self.config is None
            or config.ledger_path != (self.config.ledger_path)
        ):
            self.store = LedgerStore.open(config.ledger_path)
        self.config = config
        return None


class FormSave(BaseModel):
    version: str
    author: str = Field(min_length=1, max_length=80)
    values: dict
    secrets: dict[str, str] = Field(default_factory=dict)


class YamlSave(BaseModel):
    version: str
    author: str = Field(min_length=1, max_length=80)
    yaml: str


def create_app(
    config: ProjectConfig | None,
    store: LedgerStore | None,
    editor: SettingsEditor | None = None,
    admin_token: str | None = None,
) -> FastAPI:
    app = FastAPI(title="patchmig", docs_url="/api/docs", openapi_url="/api/openapi.json")
    ws = Workspace(config, store)

    def get(fn, *args):
        if ws.store is None:
            raise HTTPException(404, "no valid project config yet: open the settings")
        try:
            return fn(ws.store, *args)
        except data.NotFound as e:
            raise HTTPException(404, str(e)) from e

    def admin(authorization: str | None = Header(default=None)) -> None:
        if editor is None or not admin_token:
            raise HTTPException(403, "settings are read-only on this server")
        given = (authorization or "").removeprefix("Bearer ").strip()
        if not hmac.compare_digest(given.encode(), admin_token.encode()):
            raise HTTPException(401, "admin token missing or wrong")

    def settings_view(view: dict) -> dict:
        return {**view, "editable": bool(editor and admin_token)}

    def saved(view: dict) -> dict:
        problem = ws.reload(editor.path)
        return {**settings_view(view), "reload_error": problem}

    @app.get("/api/settings")
    def settings() -> dict:
        if editor is None:
            raise HTTPException(404, "settings are not served (static site)")
        return settings_view(editor.view())

    @app.get("/api/settings/auth", dependencies=[Depends(admin)])
    def settings_auth() -> dict:
        return {"ok": True}

    @app.put("/api/settings", dependencies=[Depends(admin)])
    def settings_save(body: FormSave) -> dict:
        try:
            return saved(editor.save(body.values, body.secrets, body.version, body.author))
        except SettingsError as e:
            raise HTTPException(409 if e.conflict else 422, {"problems": e.problems}) from e

    @app.put("/api/settings/yaml", dependencies=[Depends(admin)])
    def settings_save_yaml(body: YamlSave) -> dict:
        try:
            return saved(editor.save_yaml(body.yaml, body.version, body.author))
        except SettingsError as e:
            raise HTTPException(409 if e.conflict else 422, {"problems": e.problems}) from e

    @app.post("/api/settings/check/{name}", dependencies=[Depends(admin)])
    def settings_check(name: str) -> list[dict]:
        from patchmig.checks import CHECKS, run_check

        if name not in CHECKS:
            raise HTTPException(404, f"unknown check {name}; known: {', '.join(CHECKS)}")
        if ws.config is None:
            raise HTTPException(409, "save a valid config first")
        return [{"name": n, "ok": ok, "detail": d} for n, ok, d in run_check(name, ws.config)]

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    def index() -> str:
        return index_html(static=False)

    @app.get("/api/runs")
    def runs() -> list[dict]:
        return data.runs(ws.store) if ws.store else []

    @app.get("/api/runs/{run_id}/summary")
    def summary(run_id: int) -> dict:
        return get(data.summary, run_id)

    @app.get("/api/runs/{run_id}/patches")
    def patches(run_id: int) -> list[dict]:
        return get(data.patches, run_id)

    @app.get("/api/runs/{run_id}/patches/{patch_id}")
    def patch(run_id: int, patch_id: int) -> dict:
        return get(data.patch, ws.config, run_id, patch_id)

    @app.get("/api/runs/{run_id}/clusters")
    def clusters(run_id: int) -> list[dict]:
        return get(data.clusters, run_id)

    @app.get("/api/runs/{run_id}/clusters/{cluster_id}")
    def cluster(run_id: int, cluster_id: int) -> dict:
        return get(data.cluster, run_id, cluster_id)

    @app.get("/api/runs/{run_id}/review")
    def review(run_id: int) -> dict:
        return get(data.review, run_id)

    @app.get("/api/runs/{run_id}/knowledge")
    def knowledge(run_id: int) -> dict:
        return get(data.knowledge, run_id)

    @app.get("/api/runs/{run_id}/agent-calls")
    def agent_calls(run_id: int) -> list[dict]:
        return get(data.agent_calls, run_id)

    @app.get("/api/runs/{run_id}/activity")
    def activity(run_id: int) -> list[dict]:
        return get(data.recent_transitions, run_id)

    @app.get("/api/runs/{run_id}/config")
    def config_view(run_id: int) -> dict:
        return get(data.config_snapshot, run_id)

    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    return app
