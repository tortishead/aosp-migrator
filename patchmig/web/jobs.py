"""Steps started from the browser: run in the background, one at a time, with a live log.

A job is one step or a pipeline of steps (patchmig.steps, the same code the command line
runs). The server runs at most one job at a time, so two people cannot spend credits twice
or move the same clusters at once. Each job is kept as JSON in the jobs folder next to the
config, so the history survives a restart; a job that was running when the server stopped
is marked interrupted.
"""

from __future__ import annotations

import json
import threading
import traceback
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import func, select

from patchmig import steps
from patchmig.ledger.models import AgentCall

MAX_LINES = 5000
FINISHED = ("done", "failed", "stopped", "interrupted")


class Busy(Exception):
    pass


def _now() -> datetime:
    return datetime.now(UTC)


@dataclass
class Job:
    id: int
    steps: list[str]
    options: dict
    by: str
    run_id: int | None  # the run the job works on; set by ingest when it makes one
    state: str = "queued"  # running, done, failed, stopped (by budget or by hand), interrupted
    created_at: str = ""
    started_at: str | None = None
    finished_at: str | None = None
    current: str | None = None
    error: str | None = None
    stop_requested: bool = False
    credits: float = 0.0  # agent calls made while it ran (estimated, as in the ledger)
    agent_calls: int = 0
    log: list[dict] = field(default_factory=list)  # {"n", "at", "level", "text"}
    dropped_lines: int = 0

    def view(self, after: int = 0) -> dict:
        data = {k: v for k, v in asdict(self).items() if k != "log"}
        data["lines"] = self.dropped_lines + len(self.log)
        data["log"] = [line for line in self.log if line["n"] > after]
        return data


class JobManager:
    """Starts jobs and keeps them. `make_context(log)` gives a step context for a new job."""

    def __init__(self, folder: Path | None, make_context: Callable[[steps.Log], steps.Context]):
        self.folder = folder
        self.make_context = make_context
        self.jobs: dict[int, Job] = {}
        self.lock = threading.Lock()
        self.active: tuple[Job, steps.Context] | None = None
        self.thread: threading.Thread | None = None
        self._load()

    # ---- storage ----

    def _load(self) -> None:
        if self.folder is None or not self.folder.is_dir():
            return
        for path in sorted(self.folder.glob("*.json")):
            try:
                job = Job(**json.loads(path.read_text()))
            except (OSError, ValueError, TypeError):
                continue
            if job.state not in FINISHED:
                job.state, job.error = "interrupted", "the server stopped while it ran"
                job.finished_at = job.finished_at or job.started_at
                self._save(job)
            self.jobs[job.id] = job

    def _save(self, job: Job) -> None:
        if self.folder is None:
            return
        self.folder.mkdir(parents=True, exist_ok=True)
        tmp = self.folder / f".{job.id}.json.tmp"
        tmp.write_text(json.dumps(asdict(job), indent=1))
        tmp.replace(self.folder / f"{job.id:05d}.json")

    # ---- queries ----

    def list(self) -> list[dict]:
        with self.lock:
            jobs = sorted(self.jobs.values(), key=lambda j: j.id, reverse=True)
            return [{**j.view(), "log": []} for j in jobs]

    def get(self, job_id: int, after: int = 0) -> dict | None:
        with self.lock:
            job = self.jobs.get(job_id)
            if job is None:
                return None
            self._count_credits(job)
            return job.view(after)

    def running(self) -> Job | None:
        return self.active[0] if self.active else None

    def _count_credits(self, job: Job) -> None:
        if job.state != "running" or self.active is None:
            return
        store = self.active[1].store
        start = datetime.fromisoformat(job.started_at)
        with store.session() as s:
            n, credits = s.execute(
                select(func.count(), func.coalesce(func.sum(AgentCall.credits), 0.0)).where(
                    AgentCall.started_at >= start.replace(tzinfo=None)
                )
            ).one()
        job.agent_calls, job.credits = int(n), round(float(credits), 2)

    # ---- running ----

    def start(self, names: list[str], options: dict, by: str, run_id: int | None) -> Job:
        unknown = [n for n in names if n not in steps.STEPS]
        if not names or unknown:
            raise ValueError(f"unknown steps: {', '.join(unknown) or '(none given)'}")
        checked = {n: steps.coerce(steps.STEPS[n], options.get(n) or {}) for n in names}
        with self.lock:
            if self.active is not None:
                raise Busy(f"job {self.active[0].id} is still running")
            job = Job(
                id=max(self.jobs, default=0) + 1,
                steps=list(names),
                options=checked,
                by=by,
                run_id=run_id,
                created_at=_now().isoformat(timespec="seconds"),
            )
            ctx = self.make_context(lambda text, level: self._log(job, text, level))
            self.jobs[job.id] = job
            self.active = (job, ctx)
            job.state, job.started_at = "running", _now().isoformat(timespec="seconds")
            self._save(job)
        self.thread = threading.Thread(target=self._run, args=(job, ctx), daemon=True,
                                       name=f"patchmig-job-{job.id}")  # fmt: skip
        self.thread.start()
        return job

    def stop(self, job_id: int) -> bool:
        """Ask the running job to stop before its next step (a step is not cut off)."""
        with self.lock:
            if self.active is None or self.active[0].id != job_id:
                return False
            self.active[0].stop_requested = True
            self.active[1].cancel.set()
            return True

    def wait(self, timeout: float | None = None) -> None:
        if self.thread is not None:
            self.thread.join(timeout)

    def _log(self, job: Job, text: str, level: str) -> None:
        with self.lock:
            for line in text.split("\n"):
                n = job.dropped_lines + len(job.log) + 1
                job.log.append({"n": n, "at": _now().isoformat(timespec="seconds"),
                                "level": level, "text": line})  # fmt: skip
            if len(job.log) > MAX_LINES:
                cut = len(job.log) - MAX_LINES
                job.log, job.dropped_lines = job.log[cut:], job.dropped_lines + cut

    def _run(self, job: Job, ctx: steps.Context) -> None:
        def on_step(name: str) -> None:
            with self.lock:
                job.current = name
                self._save(job)

        state, error = "done", None
        try:
            job.run_id = steps.run_pipeline(ctx, job.steps, job.options, job.run_id, on_step)
            if ctx.stopped or ctx.cancel.is_set():
                state = "stopped"
        except steps.StepFailed as e:
            state, error = "failed", str(e) or "the step reported failures (see the log)"
            if str(e):
                self._log(job, str(e), "error")
        except Exception as e:  # noqa: BLE001  a bug must not take the server down
            state, error = "failed", f"{type(e).__name__}: {e}"
            self._log(job, traceback.format_exc().rstrip(), "error")
        with self.lock:
            self._count_credits(job)
            job.state, job.error, job.current = state, error, None
            job.finished_at = _now().isoformat(timespec="seconds")
            self.active = None
            self._save(job)
