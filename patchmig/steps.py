"""The migration steps, shared by the command line and the web front end.

Each step reads the config and the ledger, does its work and reports through `ctx.log`
(one line at a time, with a level). `migrate <step>` prints the lines; the front end's Run
page shows them live. Both call the same functions with the same options, so a step costs
the same Copilot credits whichever way it is started.

A step that cannot go on raises StepFailed. A step stopped by the credit budget sets
`ctx.stopped`, so a pipeline does not go on to spend more.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import select

from patchmig.config import ProjectConfig
from patchmig.ledger import LedgerStore, Patch, Run, State

Log = Callable[[str, str], None]  # (text, level): level is info, ok, warn or error


class StepFailed(Exception):
    """The step could not finish; the message (may be empty) says why."""


def _copilot(cfg: ProjectConfig):
    from patchmig.agents.copilot import CopilotCliAdapter

    return CopilotCliAdapter(cfg.llm, scratch=cfg.workdir / "briefs")


def _jira_client(cfg: ProjectConfig):
    from patchmig.jira_client import HttpJiraClient

    return HttpJiraClient(cfg.jira)


def _review_client(cfg: ProjectConfig):
    from patchmig.publish.hosts import client_for

    return client_for(cfg)


@dataclass
class Context:
    """What a step works on. The factories are replaced by fakes in tests."""

    config_path: Path
    config: ProjectConfig
    store: LedgerStore
    log: Log
    adapter: Callable[[ProjectConfig], object] = _copilot
    jira: Callable[[ProjectConfig], object] = _jira_client
    review_host: Callable[[ProjectConfig], object] = _review_client
    cancel: threading.Event = field(default_factory=threading.Event)
    stopped: str | None = None  # set when the credit budget stopped a step

    def info(self, text: str) -> None:
        self.log(text, "info")

    def warn(self, text: str) -> None:
        self.log(text, "warn")

    def run(self, run_id: int | None) -> Run:
        """The run named, else the latest."""
        if run_id is not None:
            with self.store.session() as s:
                run = s.get(Run, run_id)
            if run is None:
                raise StepFailed(f"There is no run {run_id}.")
            return run
        run = self.store.latest_run()
        if run is None:
            raise StepFailed("No runs yet. Start one with: migrate ingest")
        return run

    def runner(self, run: Run):
        from patchmig.agents.runner import AgentRunner

        cfg = self.config
        return AgentRunner(self.adapter(cfg), self.store, run, cfg.llm, cfg.budget)

    def memory(self, create: bool = False, required: bool = False):
        """The migration memory. Not required: None when not configured or not there."""
        from patchmig.memory import Memory, MemoryRepoError

        if not required and not self.config.memory.path:
            return None
        try:
            return Memory.open(self.config, create=create)
        except MemoryRepoError as e:
            if required:
                raise StepFailed(str(e)) from e
            return None

    def mcp(self, run_id: int) -> dict:
        from patchmig.mcp_server.server import server_config

        return server_config(str(self.config_path.resolve()), run_id)

    def jira_client(self):
        try:
            return self.jira(self.config)
        except (KeyError, OSError) as e:
            raise StepFailed(f"Jira token not available ({e}); use --no-jira to skip.") from e

    def host_client(self):
        from patchmig.publish.hosts import ReviewHostError

        try:
            return self.review_host(self.config)
        except ReviewHostError as e:
            raise StepFailed(str(e)) from e
        except (KeyError, OSError) as e:
            raise StepFailed(f"The output host's token is not available ({e}).") from e


def _patch_projects(store: LedgerStore, run_id: int | None) -> list[str]:
    if run_id is None:
        return []
    with store.session() as s:
        return sorted(set(s.scalars(select(Patch.project).where(Patch.run_id == run_id))))


# ---- the steps --------------------------------------------------------------------------


def ingest(ctx: Context, use_jira: bool = True, note: str | None = None) -> int:
    """Read the patch list into a new run; returns the run id."""
    from patchmig.ingest import IngestError
    from patchmig.ingest import ingest as do_ingest

    cfg, store = ctx.config, ctx.store
    jira = ctx.jira_client() if use_jira and cfg.jira is not None else None
    run = store.create_run(cfg, note=note or "ingest")
    try:
        report = do_ingest(cfg, store, run, jira)
    except IngestError as e:
        store.discard_run(run)  # else the empty run would become "the latest run"
        raise StepFailed(f"Ingest failed: {e}") from e
    ctx.info(f"Run {run.id}: {report.total} patches")
    for source, n in report.found_per_source.items():
        ctx.info(f"  from {source:<12} {n}")
    if report.duplicates_merged:
        ctx.info(f"  duplicates merged: {report.duplicates_merged}")
    ctx.info("  roles: " + ", ".join(f"{k} {v}" for k, v in sorted(report.roles.items())))
    if jira is not None:
        ctx.info(
            f"  Jira tickets fetched: {report.tickets_fetched} "
            f"(+{report.linked_tickets_fetched} linked)"
        )
        if report.tickets_missing:
            ctx.info(f"  Jira tickets not found: {', '.join(report.tickets_missing)}")
    for w in report.warnings:
        ctx.warn(f"  warning: {w}")
    return run.id


def history(ctx: Context, run_id: int | None = None, dry_run: bool = False) -> None:
    """Deepen shallow checkouts as far as the upstream checks need."""
    from patchmig.ingest.manifest import tree_projects
    from patchmig.knowledge.history import deepen_trees

    cfg, store = ctx.config, ctx.store
    if run_id is None:
        latest = store.latest_run()
        run_id = latest.id if latest else None
    patch_projects = _patch_projects(store, run_id)
    source = list(patch_projects)
    if any(src.type == "git_range" for src in cfg.patch_sources):
        source = tree_projects(cfg.trees.source.path, cfg.trees.source.manifest)
    if not patch_projects:
        ctx.info("No patches yet: only the source tree is checked. Run again after ingest.")
    results = deepen_trees(cfg, source, patch_projects, dry_run=dry_run)
    if not results:
        ctx.info("No shallow project to deepen.")
    failed = False
    for r in results:
        grown = f"{r.commits_before} -> {r.commits_after} commits, {r.fetches} fetch(es)"
        ctx.log(f"  {'ok  ' if r.ok else 'FAIL'} {r.side:<6} {r.project}: {grown}; {r.detail}",
                "ok" if r.ok else "error")  # fmt: skip
        failed |= not r.ok
    if failed:
        raise StepFailed("")


def analyse(ctx: Context, run_id: int | None = None) -> None:
    """Decide which patches are needed, cluster them and forecast each cluster. No credits."""
    from patchmig.analysis import AnalysisError
    from patchmig.analysis import analyse as do_analyse

    run = ctx.run(run_id)
    try:
        report = do_analyse(ctx.config, ctx.store, run)
    except AnalysisError as e:
        raise StepFailed(f"Analysis failed: {e}") from e
    ctx.info(f"Run {run.id}: {sum(report.states.values())} patches, {report.clusters} clusters")
    for state in State:
        if report.states.get(state.value):
            ctx.info(f"  {state.value:<15} {report.states[state.value]}")
    decided = {k: v for k, v in report.rungs.items() if k != "none"}
    if decided:
        ctx.info("  decided by: " + ", ".join(f"{k} {v}" for k, v in sorted(decided.items())))
    if report.forecasts:
        ctx.info(
            "  clusters to migrate: "
            + ", ".join(f"{k} {v}" for k, v in sorted(report.forecasts.items()))
        )
    for w in report.warnings:
        ctx.warn(f"  warning: {w}")


def intents(
    ctx: Context,
    run_id: int | None = None,
    limit: int | None = None,
    dry_run: bool = False,
    patches: list[int] | None = None,
    stale: bool = False,
    export: Path | None = None,
) -> None:
    """Write an intent for each needed patch (earlier intents are reused)."""
    from patchmig.agents.intent import export_review_sheet, write_intents

    run = ctx.run(run_id)
    rid = run.id
    runner = None if dry_run else ctx.runner(run)
    report = write_intents(ctx.config, ctx.store, run, runner, limit=limit, memory=ctx.memory(),
                           redo=patches, stale=stale)  # fmt: skip
    ctx.info(
        f"Run {rid}: {report.written} written, {report.reused} reused, "
        f"{report.skipped} already had one, {report.not_needed} not needed; "
        f"estimated credits {report.credits:.1f}"
    )
    for failure in report.failed:
        ctx.warn(f"  failed: {failure}")
    if report.stopped_by_budget:
        ctx.warn(f"  stopped: {report.stopped_by_budget}")
        ctx.stopped = report.stopped_by_budget
    if export:
        n = export_review_sheet(ctx.store, run, export)
        ctx.info(f"Review sheet with {n} intents written to {export}")


def apply(
    ctx: Context,
    run_id: int | None = None,
    clusters: list[int] | None = None,
    limit: int | None = None,
    no_agent: bool = False,
    stale: bool = False,
    redo: bool = False,
    by: str | None = None,
) -> None:
    """Migrate the needed clusters onto the target tree, one branch per cluster."""
    from patchmig import notes as engineer_notes
    from patchmig.confidence import describe
    from patchmig.migrate import migrate

    cfg, store = ctx.config, ctx.store
    run = ctx.run(run_id)
    rid = run.id
    if redo and not clusters:
        raise StepFailed("--redo needs --cluster")
    if stale or redo:
        actor = f"human:{by}" if by else "orchestrator"
        if redo:
            ids, why = list(clusters), "rerun requested"
        else:
            found = engineer_notes.stale_clusters(store, run)
            found = [x for x in found if not clusters or x.id in clusters]
            ids, why = [x.id for x in found], "knowledge changed"
            for x in found:
                ctx.info(f"  stale [{x.id}] {x.state:<14} {x.name}: {x.why}")
            if not ids:
                ctx.info(f"Run {rid}: no cluster is stale.")
                return
        try:
            reopened, messages = engineer_notes.rerun(store, run, ids, actor, why)
        except engineer_notes.NoteError as e:
            raise StepFailed(str(e)) from e
        for m in messages:
            ctx.warn(f"  note: {m}")
        clusters = reopened
    runner = ctx.runner(run) if not no_agent and cfg.migrate.agent else None
    report = migrate(cfg, store, run, runner, cluster_ids=clusters, limit=limit,
                     mcp=ctx.mcp(rid), memory=ctx.memory())  # fmt: skip
    if not report.outcomes:
        ctx.info(f"Run {rid}: no cluster to migrate (run migrate analyse first?)")
    for o in report.outcomes:
        conf = f" confidence {describe(o.confidence)}" if o.confidence is not None else ""
        ctx.info(f"  [{o.cluster_id}] {o.state:<14} {o.name}{conf}")
        if o.reason:
            ctx.info(f"        {o.reason}")
    if runner is not None:
        ctx.info(f"Estimated credits used in this run so far: {report.credits:.1f}")
    if report.stopped_by_budget:
        ctx.warn(f"Stopped: {report.stopped_by_budget}")
        ctx.stopped = report.stopped_by_budget


def verify(
    ctx: Context,
    run_id: int | None = None,
    clusters: list[int] | None = None,
    limit: int | None = None,
    no_fix: bool = False,
) -> None:
    """Build the affected modules of each migrated cluster, then test it."""
    from patchmig.verify import VerifyError
    from patchmig.verify import verify as do_verify

    run = ctx.run(run_id)
    rid = run.id
    runner = None if no_fix else ctx.runner(run)
    try:
        report = do_verify(ctx.config, ctx.store, run, runner, cluster_ids=clusters, limit=limit,
                           mcp=ctx.mcp(rid))  # fmt: skip
    except VerifyError as e:
        raise StepFailed(str(e)) from e
    if not report.outcomes:
        ctx.info(f"Run {rid}: no migrated cluster to verify (run migrate apply first?)")
    for o in report.outcomes:
        ctx.info(f"  [{o.cluster_id}] {o.state:<14} {o.name}")
        if o.reason:
            ctx.info(f"        {o.reason}")
    if report.stopped_by_budget:
        ctx.warn(f"Stopped: {report.stopped_by_budget}")
        ctx.stopped = report.stopped_by_budget


def publish(
    ctx: Context,
    run_id: int | None = None,
    clusters: list[int] | None = None,
    unverified: bool = False,
    dry_run: bool = False,
) -> None:
    """Push each verified cluster's branch and open (or refresh) one draft PR per repo."""
    from patchmig.publish import PublishError
    from patchmig.publish import publish as do_publish

    run = ctx.run(run_id)
    try:
        api = None if dry_run else ctx.host_client()
        report = do_publish(ctx.config, ctx.store, run, api, cluster_ids=clusters,
                            include_unverified=unverified, dry_run=dry_run)  # fmt: skip
    except PublishError as e:
        raise StepFailed(str(e)) from e
    if not report.outcomes:
        ctx.info(f"Run {run.id}: no verified cluster to publish")
    for o in report.outcomes:
        ctx.info(f"  [{o.cluster_id}] {o.status:<9} {o.name}")
        for url in o.urls:
            ctx.info(f"        {url}")
        if o.reason:
            ctx.info(f"        {o.reason}")


def review_sync(ctx: Context, run_id: int | None = None) -> None:
    """Read the PRs: merges and approvals approve, change requests and closes reject."""
    from patchmig.review import sync_reviews

    run = ctx.run(run_id)
    if ctx.config.output is None:
        raise StepFailed("No output section in the config: nothing was published.")
    changes = sync_reviews(ctx.store, run, ctx.host_client(), ctx.config.output.target)
    ctx.info("\n".join(changes) if changes else "No changes.")


def jira_export(
    ctx: Context, run_id: int | None = None, dry_run: bool = False, create_missing: bool = False
) -> None:
    """Write the approved results to Jira: labels, PR links and one marker comment."""
    from patchmig.jira_export import export, plan

    cfg = ctx.config
    run = ctx.run(run_id)
    if cfg.jira is None:
        raise StepFailed("No jira section in the config.")
    if dry_run:
        tickets = plan(cfg, ctx.store, run)
        if not tickets:
            ctx.info("Nothing approved to export yet.")
        for t in tickets:
            closing = "  (would close)" if t.close else ""
            ctx.info(
                f"{t.key or 'NEW: ' + (t.summary or '')}  labels: {', '.join(t.labels)}{closing}"
            )
            for url, _, _ in t.links:
                ctx.info(f"  link: {url}")
            ctx.info("  " + t.comment.replace("\n", "\n  ").rstrip())
        return
    report = export(cfg, ctx.store, run, ctx.jira_client(), create_missing=create_missing)
    ctx.info(
        f"Updated {len(report.updated)} ticket(s), created {len(report.created)}, "
        f"closed {len(report.closed)}."
    )
    for line in report.skipped:
        hint = " (use --create-missing)" if line.startswith("untracked") else ""
        ctx.info(f"  skipped: {line}{hint}")
    for line in report.errors:
        ctx.log(f"  error: {line}", "error")
    if report.errors:
        raise StepFailed("")


def memory_record(ctx: Context, run_id: int | None = None) -> None:
    """Write the run's approved results, code mappings and decisions to the memory repo."""
    run = ctx.run(run_id)
    mem = ctx.memory(create=True, required=True)
    r = mem.record(ctx.config, ctx.store, run)
    ctx.info(f"Recorded {r.patches} patch(es), {r.mappings} mapping(s), {r.decisions} "
             f"decision(s) in {mem.path}" + (f" (commit {r.commit[:12]})" if r.commit else
                                             " (nothing new)"))  # fmt: skip


def memory_curate(ctx: Context, run_id: int | None = None) -> None:
    """Ask the memory curator for lessons from the run's feedback and fixes (one agent call)."""
    from patchmig.agents.curator import curate
    from patchmig.agents.runner import AgentFailed, BudgetExceeded

    run = ctx.run(run_id)
    mem = ctx.memory(create=True, required=True)
    try:
        r = curate(ctx.config, ctx.store, run, ctx.runner(run), mem)
    except (AgentFailed, BudgetExceeded) as e:
        raise StepFailed(f"Curator: {e}") from e
    if not r.signals:
        ctx.info("Nothing to learn from in this run (no feedback, decisions or fixes).")
        return
    ctx.info(f"{len(r.proposed)} lesson(s) proposed from {r.signals} cluster(s); review them "
             "on the Memory page or with: migrate memory list --proposed")  # fmt: skip
    for name in r.proposed:
        ctx.info(f"  {name}")


# ---- the catalogue, for the front end ---------------------------------------------------


@dataclass(frozen=True)
class Option:
    name: str  # the step function's keyword
    kind: str  # bool, int, ints (cluster or patch ids), text
    label: str
    help: str = ""
    default: bool | None = None  # for bool options that are on unless turned off


@dataclass(frozen=True)
class Step:
    name: str
    title: str
    help: str
    fn: Callable
    credits: str  # none, maybe (only when cheaper means fail), yes
    options: tuple[Option, ...] = ()
    run_scoped: bool = True  # takes run_id
    in_pipeline: bool = False


CLUSTERS = Option("clusters", "ints", "Only these clusters", "cluster ids, comma-separated")
LIMIT = Option("limit", "int", "At most N")

STEPS: dict[str, Step] = {s.name: s for s in (
    Step("ingest", "Ingest patches", "Read the patch list from all sources into a new run.",
         ingest, "none", (Option("use_jira", "bool", "Fetch Jira tickets", default=True),
                          Option("note", "text", "Note for the run")),
         run_scoped=False, in_pipeline=True),
    Step("history", "Deepen shallow checkouts", "Fetch the history the upstream checks need "
         "(repo sync --depth=1 trees). Before ingest for a git range, and after it.", history,
         "none", (Option("dry_run", "bool", "Only report"),), in_pipeline=True),
    Step("analyse", "Analyse", "Decide which patches are still needed, cluster and forecast "
         "them. Deterministic.", analyse, "none", in_pipeline=True),
    Step("intents", "Write intents", "One intent per needed patch; earlier ones are reused.",
         intents, "yes", (LIMIT, Option("stale", "bool", "Only intents whose notes changed"),
                          Option("patches", "ints", "Write these patches' intents again",
                                 "patch ids"),
                          Option("dry_run", "bool", "Only reuse earlier intents")),
         in_pipeline=True),
    Step("apply", "Migrate clusters", "Cherry-pick, then net diff, then the cluster agent only "
         "when both fail.", apply, "maybe",
         (CLUSTERS, LIMIT, Option("no_agent", "bool", "No agent calls"),
          Option("stale", "bool", "Rerun the clusters whose notes changed"),
          Option("redo", "bool", "Rerun the chosen clusters, whatever state")), in_pipeline=True),
    Step("verify", "Build and test", "Build the affected modules on the build host, then run "
         "the tests; the agent fixes build breaks.", verify, "maybe",
         (CLUSTERS, LIMIT, Option("no_fix", "bool", "Do not ask the agent to fix builds")),
         in_pipeline=True),
    Step("publish", "Publish PRs", "Push each verified cluster and open one draft PR per repo.",
         publish, "none", (CLUSTERS, Option("unverified", "bool", "Also unverified clusters"),
                           Option("dry_run", "bool", "Only show what would be pushed")),
         in_pipeline=True),
    Step("review_sync", "Sync PR reviews", "Approvals and merges approve; change requests and "
         "closes send the cluster back.", review_sync, "none"),
    Step("jira_export", "Export to Jira", "Labels, PR links and one marker comment per "
         "approved ticket.", jira_export, "none",
         (Option("dry_run", "bool", "Preview only"),
          Option("create_missing", "bool", "Create tickets for untracked patches"))),
    Step("memory_record", "Record in memory", "Write approved results and decisions to the "
         "migration memory.", memory_record, "none"),
    Step("memory_curate", "Propose lessons", "One curator call: lessons from feedback and "
         "fixes, for you to approve.", memory_curate, "yes"),
)}  # fmt: skip

PIPELINE = [s.name for s in STEPS.values() if s.in_pipeline]


def catalogue() -> list[dict]:
    return [
        {"name": s.name, "title": s.title, "help": s.help, "credits": s.credits,
         "run_scoped": s.run_scoped, "in_pipeline": s.in_pipeline,
         "options": [{"name": o.name, "kind": o.kind, "label": o.label, "help": o.help,
                      "default": o.default} for o in s.options]}
        for s in STEPS.values()
    ]  # fmt: skip


def coerce(step: Step, raw: dict) -> dict:
    """Options from the front end, checked against the step's catalogue entry."""
    known = {o.name: o for o in step.options}
    out = {}
    for name, value in raw.items():
        opt = known.get(name)
        if opt is None:
            raise ValueError(f"{step.name} has no option {name}")
        if value is None or value == "" or value == []:
            continue
        if opt.kind == "bool":
            if not isinstance(value, bool):
                raise ValueError(f"{name} must be true or false")
        elif opt.kind == "int":
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{name} must be a positive whole number")
        elif opt.kind == "ints":
            if isinstance(value, str):
                try:
                    value = [int(v) for v in value.replace(",", " ").split()]
                except ValueError as e:
                    raise ValueError(f"{name} must be ids separated by commas") from e
            if not isinstance(value, list) or not all(
                isinstance(v, int) and not isinstance(v, bool) for v in value
            ):
                raise ValueError(f"{name} must be a list of ids")
        elif not isinstance(value, str):
            raise ValueError(f"{name} must be text")
        out[name] = value
    return out


def run_pipeline(
    ctx: Context, names: list[str], options: dict[str, dict], run_id: int | None,
    on_step: Callable[[str], None] = lambda name: None,
) -> int | None:  # fmt: skip
    """Run steps in pipeline order; stop at the first failure, a budget stop or a cancel.

    Returns the run worked on (a new one when the steps include ingest). With a git range
    source, history runs both before and after ingest, as the history step asks.
    """
    order = [n for n in PIPELINE if n in names] + [n for n in names if n not in PIPELINE]
    if (
        "ingest" in order
        and "history" in order
        and any(src.type == "git_range" for src in ctx.config.patch_sources)
    ):
        order.insert(order.index("ingest"), "history")
    for name in order:
        if ctx.cancel.is_set():
            ctx.warn(f"Stopped before {STEPS[name].title.lower()}: stop requested.")
            return run_id
        step = STEPS[name]
        on_step(name)
        ctx.log(f"== {step.title}", "step")
        kwargs = dict(options.get(name) or {})
        if step.run_scoped:
            kwargs["run_id"] = run_id
        result = step.fn(ctx, **kwargs)
        if name == "ingest":
            run_id = result
        if ctx.stopped:
            ctx.warn("Pipeline stopped by the credit budget.")
            return run_id
    return run_id
