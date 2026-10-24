"""Web API and UI over one project's ledger, and the project's settings.

`migrate serve` runs this app; the UI in static/ calls the /api routes. Everything the
command line does can be done here: run the steps (as background jobs, see jobs.py, with
the same code as `migrate <step>`), take review decisions, edit the engineer notes, approve
memory lessons and edit the settings. Reading needs nothing; every write (and every
connection test, which uses the stored credentials) needs the admin token as a Bearer token.
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

from patchmig import steps
from patchmig.config import ConfigError, ProjectConfig, load_config
from patchmig.config.editor import SettingsEditor, SettingsError
from patchmig.ledger import LedgerStore, Run
from patchmig.web import data
from patchmig.web.jobs import Busy, JobManager

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


Name = Field(min_length=1, max_length=80)


class JobStart(BaseModel):
    steps: list[str] = Field(min_length=1)
    options: dict[str, dict] = Field(default_factory=dict)
    by: str = Name
    run_id: int | None = None


class ReviewAction(BaseModel):
    action: str  # approve, reject, retry, decide
    by: str = Name
    reason: str = ""
    choice: str = ""  # decide: migrate, upstream or obsolete


class NoteNew(BaseModel):
    run_id: int | None = None
    text: str = Field(min_length=1)
    by: str = Name
    scope: str = "all"
    target: str | int | None = None
    stages: list[str] | None = None


class NoteChange(BaseModel):
    by: str = Name
    text: str | None = None
    stages: list[str] | None = None


class ByOnly(BaseModel):
    by: str = Name


# Review actions wait while a job that moves clusters runs, so the two cannot cross.
CLUSTER_STEPS = frozenset({"analyse", "apply", "verify", "publish", "review_sync"})


def create_app(
    config: ProjectConfig | None,
    store: LedgerStore | None,
    editor: SettingsEditor | None = None,
    admin_token: str | None = None,
    hooks: dict | None = None,
) -> FastAPI:
    """`hooks` replaces the step context's factories (adapter, jira, review_host): for tests."""
    app = FastAPI(title="patchmig", docs_url="/api/docs", openapi_url="/api/openapi.json")
    ws = Workspace(config, store)

    def make_context(log: steps.Log) -> steps.Context:
        return steps.Context(editor.path, ws.config, ws.store, log, **(hooks or {}))

    folder = editor.path.parent / ".patchmig" / "jobs" if editor else None
    jobs = JobManager(folder, make_context)
    app.state.jobs = jobs

    def get(fn, *args):
        if ws.store is None:
            raise HTTPException(404, "no valid project config yet: open the settings")
        try:
            return fn(ws.store, *args)
        except data.NotFound as e:
            raise HTTPException(404, str(e)) from e

    def admin(authorization: str | None = Header(default=None)) -> None:
        if editor is None or not admin_token:
            raise HTTPException(403, "this server is read-only (started with --read-only)")
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

    # ---- actions: jobs, review decisions, notes, lessons (admin token) -----------------

    def configured() -> tuple[ProjectConfig, LedgerStore]:
        if ws.config is None or ws.store is None:
            raise HTTPException(409, "save a valid config first (Settings)")
        return ws.config, ws.store

    def busy_with(names: frozenset[str]) -> None:
        job = jobs.running()
        if job is not None and names & set(job.steps):
            raise HTTPException(409, f"job {job.id} ({', '.join(job.steps)}) is running; "
                                "try again when it has finished")  # fmt: skip

    def the_run(run_id: int) -> Run:
        _, store = configured()
        with store.session() as s:
            run = s.get(Run, run_id)
        if run is None:
            raise HTTPException(404, f"there is no run {run_id}")
        return run

    @app.get("/api/steps")
    def step_list() -> dict:
        return {"steps": steps.catalogue(), "pipeline": steps.PIPELINE,
                "actions": bool(editor and admin_token)}  # fmt: skip

    @app.get("/api/jobs")
    def job_list() -> list[dict]:
        return jobs.list()

    @app.get("/api/jobs/{job_id}")
    def job(job_id: int, after: int = 0) -> dict:
        found = jobs.get(job_id, after)
        if found is None:
            raise HTTPException(404, f"there is no job {job_id}")
        return found

    @app.post("/api/jobs", dependencies=[Depends(admin)], status_code=202)
    def job_start(body: JobStart) -> dict:
        configured()
        if body.run_id is not None:
            the_run(body.run_id)
        try:
            started = jobs.start(body.steps, body.options, body.by, body.run_id)
        except Busy as e:
            raise HTTPException(409, str(e)) from e
        except ValueError as e:
            raise HTTPException(422, str(e)) from e
        return started.view()

    @app.post("/api/jobs/{job_id}/stop", dependencies=[Depends(admin)])
    def job_stop(job_id: int) -> dict:
        if not jobs.stop(job_id):
            raise HTTPException(409, f"job {job_id} is not running")
        return {"ok": True}

    @app.post("/api/runs/{run_id}/clusters/{cluster_id}/review", dependencies=[Depends(admin)])
    def review_action(run_id: int, cluster_id: int, body: ReviewAction) -> dict:
        from patchmig.review import Reviewer, ReviewError

        busy_with(CLUSTER_STEPS)
        reviewer = Reviewer(ws.store, the_run(run_id))
        by, reason = body.by.strip(), body.reason.strip()
        actions = {
            "approve": lambda: reviewer.approve(cluster_id, by, reason, source="web"),
            "reject": lambda: reviewer.reject(cluster_id, by, reason, source="web"),
            "retry": lambda: reviewer.retry(cluster_id, by, reason),
            "decide": lambda: reviewer.decide(cluster_id, by, body.choice, reason),
        }
        if body.action not in actions:
            raise HTTPException(422, f"action must be one of {', '.join(actions)}")
        try:
            actions[body.action]()
        except ReviewError as e:
            raise HTTPException(409, str(e)) from e
        return data.cluster(ws.store, run_id, cluster_id)

    @app.get("/api/notes")
    def note_list(removed: bool = False) -> list[dict]:
        return data.notes(ws.store, removed) if ws.store else []

    @app.post("/api/notes", dependencies=[Depends(admin)])
    def note_add(body: NoteNew) -> dict:
        from patchmig import notes as engineer_notes

        _, store = configured()
        run = the_run(body.run_id) if body.run_id is not None else store.latest_run()
        if run is None and body.scope in ("patch", "cluster"):
            raise HTTPException(409, "no run yet: a note for a patch or cluster needs one")
        try:
            if run is None:
                n = engineer_notes.add(store, body.text, body.by.strip(), body.scope,
                                       str(body.target or ""), "", body.stages)  # fmt: skip
            else:
                n = engineer_notes.add_for_run(store, run, body.text, body.by.strip(),
                                               body.scope, body.target, body.stages)  # fmt: skip
        except engineer_notes.NoteError as e:
            raise HTTPException(422, str(e)) from e
        return next(x for x in data.notes(store) if x["id"] == n.id)

    @app.put("/api/notes/{note_id}", dependencies=[Depends(admin)])
    def note_edit(note_id: int, body: NoteChange) -> dict:
        from patchmig import notes as engineer_notes

        _, store = configured()
        try:
            engineer_notes.edit(store, note_id, body.by.strip(), body.text, body.stages)
        except engineer_notes.NoteError as e:
            raise HTTPException(404 if "not found" in str(e) else 422, str(e)) from e
        return next(x for x in data.notes(store) if x["id"] == note_id)

    @app.post("/api/notes/{note_id}/remove", dependencies=[Depends(admin)])
    def note_remove(note_id: int, body: ByOnly) -> dict:
        from patchmig import notes as engineer_notes

        _, store = configured()
        try:
            engineer_notes.remove(store, note_id, body.by.strip())
        except engineer_notes.NoteError as e:
            raise HTTPException(404, str(e)) from e
        return {"ok": True}

    @app.get("/api/runs/{run_id}/stale")
    def stale(run_id: int) -> dict:
        return get(data.stale, run_id)

    @app.get("/api/runs/{run_id}/report")
    def report(run_id: int) -> dict:
        from patchmig.report import run_report

        the_run(run_id)
        return {"markdown": run_report(ws.store, run_id)}

    @app.get("/api/memory")
    def memory() -> dict:
        return data.lessons(ws.config)

    @app.post("/api/memory/lessons/{name}/{decision}", dependencies=[Depends(admin)])
    def lesson_decide(name: str, decision: str, body: ByOnly) -> dict:
        from patchmig.memory import Memory, MemoryRepoError

        if decision not in ("approve", "reject"):
            raise HTTPException(404, "decide with approve or reject")
        cfg, _ = configured()
        busy_with(frozenset({"memory_record", "memory_curate"}))
        try:
            mem = Memory.open(cfg)
            (mem.approve if decision == "approve" else mem.reject)(name, body.by.strip())
        except MemoryRepoError as e:
            raise HTTPException(404, str(e)) from e
        return data.lessons(cfg)

    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    return app
