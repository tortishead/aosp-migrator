"""Several migration projects behind one server: a workspace folder of project folders.

`migrate serve --workspace DIR` serves every DIR/<folder>/project.yaml. Each project gets the
same app as a one-project server (api.create_app), mounted at /p/<folder>/; the page and its
API calls use relative URLs, so they work there unchanged. The page at / lists the projects and
creates new ones, and the header of every project page switches between them. A project's
ledger, jobs, settings and secrets stay in its own folder, so switching away saves nothing and
loses nothing: come back and carry on. One job runs at a time across all projects (they spend
the same credits and may share a target tree).
"""

from __future__ import annotations

import hmac
import re
import threading
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.routing import Mount

from patchmig.config import ConfigError, load_config, new_project
from patchmig.config.editor import SettingsEditor
from patchmig.ledger import LedgerStore
from patchmig.web import data
from patchmig.web.api import STATIC, create_app, index_html

CONFIG = "project.yaml"
# A folder name is part of every project URL: keep it plain.
FOLDER = re.compile(r"^[a-z0-9][a-z0-9._-]{0,62}$")


class ProjectNew(BaseModel):
    folder: str


class Projects:
    """The workspace folder and the app of each project in it, made on first use."""

    def __init__(self, root: Path, admin_token: str | None, hooks: dict | None,
                 token_required: bool = True):  # fmt: skip
        self.root = root.resolve()
        self.admin_token = admin_token
        self.token_required = token_required
        self.actions = bool(admin_token) or not token_required
        self.hooks = hooks
        self.gate = threading.Lock()
        self.lock = threading.Lock()
        self.apps: dict[str, FastAPI] = {}

    def folders(self) -> list[str]:
        if not self.root.is_dir():
            return []
        return sorted(
            p.name for p in self.root.iterdir() if FOLDER.match(p.name) and (p / CONFIG).is_file()
        )

    def app(self, folder: str) -> FastAPI | None:
        path = self.root / folder / CONFIG
        if not FOLDER.match(folder) or not path.is_file():
            return None
        with self.lock:
            if folder not in self.apps:
                try:
                    cfg = load_config(path)
                    store = LedgerStore.open(cfg.ledger_path)
                except ConfigError:
                    cfg, store = None, None  # settings only, to fix it (as `migrate serve`)
                self.apps[folder] = create_app(cfg, store, SettingsEditor(path), self.admin_token,
                                               self.hooks, folder=folder, gate=self.gate,
                                               token_required=self.token_required)  # fmt: skip
            return self.apps[folder]

    def entry(self, folder: str) -> dict:
        """What the project list and the switcher show of a project."""
        app = self.app(folder)
        ws = app.state.ws
        entry = {"folder": folder, "name": folder, "migration": None,
                 "valid": ws.config is not None, "runs": 0, "latest_run": None,
                 "job": None}  # fmt: skip
        if ws.config is not None:
            m = ws.config.migration
            entry["name"] = ws.config.name
            entry["migration"] = f"Android {m.source_version} → {m.target_version}"
        if ws.store is not None:
            runs = data.runs(ws.store)
            entry["runs"] = len(runs)
            entry["latest_run"] = runs[0] if runs else None
        job = app.state.jobs.running()
        if job is not None:
            entry["job"] = {"id": job.id, "steps": job.steps, "current": job.current}
        return entry


class _Dispatch:
    """ASGI app behind /p/{folder}: hands the request to that project's app."""

    def __init__(self, projects: Projects):
        self.projects = projects

    async def __call__(self, scope, receive, send):
        folder = scope["path_params"]["folder"]
        app = self.projects.app(folder)
        if app is None:
            response = JSONResponse({"detail": f"there is no project {folder}"}, 404)
            await response(scope, receive, send)
            return
        await app(scope, receive, send)


def create_workspace_app(
    root: Path,
    admin_token: str | None = None,
    read_only: bool = False,
    hooks: dict | None = None,
    token_required: bool = True,
) -> FastAPI:
    """`hooks` goes to every project's app (create_app): for tests.

    With `token_required` False (and not `read_only`), writes need no admin token.
    """
    projects = Projects(root, None if read_only else admin_token, hooks,
                        token_required or read_only)  # fmt: skip
    app = FastAPI(title="patchmig workspace", docs_url="/api/docs",
                  openapi_url="/api/openapi.json")  # fmt: skip
    app.state.projects = projects

    def admin(authorization: str | None = Header(default=None)) -> None:
        if not projects.actions:
            raise HTTPException(403, "this server is read-only (started with --read-only)")
        if not projects.token_required:
            return
        given = (authorization or "").removeprefix("Bearer ").strip()
        if not hmac.compare_digest(given.encode(), projects.admin_token.encode()):
            raise HTTPException(401, "admin token missing or wrong")

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    def index() -> str:
        return index_html(static=False, workspace="", token=projects.token_required)

    @app.get("/api/workspace")
    def workspace() -> dict:
        return {"root": str(projects.root), "actions": projects.actions,
                "projects": [projects.entry(f) for f in projects.folders()]}  # fmt: skip

    # The sign-in panel checks the token here, as on a project's settings.
    @app.get("/api/settings/auth", dependencies=[Depends(admin)])
    def settings_auth() -> dict:
        return {"ok": True}

    @app.post("/api/workspace/projects", dependencies=[Depends(admin)], status_code=201)
    def project_new(body: ProjectNew) -> dict:
        folder = body.folder.strip()
        if not FOLDER.match(folder):
            rule = "lowercase letters, digits, '.', '_' and '-', starting with a letter or digit"
            raise HTTPException(422, f"folder: {rule}, at most 63 characters")
        try:
            new_project(projects.root / folder, name=folder)
        except FileExistsError as e:
            raise HTTPException(409, f"{folder} already has a project") from e
        return projects.entry(folder)

    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    app.router.routes.append(Mount("/p/{folder}", app=_Dispatch(projects)))
    return app
