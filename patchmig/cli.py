"""The `migrate` command line."""

from __future__ import annotations

from importlib.resources import files
from pathlib import Path
from typing import Annotated

import typer
from sqlalchemy import select

from patchmig.agents.adapter import AgentRuntimeError, AgentTask, ToolPolicy, extract_json
from patchmig.agents.copilot import CopilotCliAdapter
from patchmig.agents.intent import export_review_sheet, write_intents
from patchmig.agents.runner import AgentRunner
from patchmig.analysis import AnalysisError, analyse
from patchmig.config import ConfigError, ProjectConfig, dump_config, load_config
from patchmig.ingest import IngestError, ingest
from patchmig.jira_client import HttpJiraClient, JiraClient
from patchmig.ledger import Cluster, LedgerStore, Patch, Run, State
from patchmig.migrate import migrate
from patchmig.verify import VerifyError, check_host, verify

app = typer.Typer(help="Migrate OEM Android framework patches between Android versions.")
config_app = typer.Typer(help="Inspect and validate the project configuration.")
run_app = typer.Typer(help="Manage migration runs.")
review_app = typer.Typer(help="Review queues and human decisions on clusters.")
memory_app = typer.Typer(help="The migration memory: facts and lessons from earlier migrations.")
notes_app = typer.Typer(help="Engineer notes: extra knowledge for the agents, per scope and step.")
app.add_typer(config_app, name="config")
app.add_typer(run_app, name="run")
app.add_typer(review_app, name="review")
app.add_typer(memory_app, name="memory")
app.add_typer(notes_app, name="notes")

ConfigOption = Annotated[
    Path, typer.Option("--config", "-c", help="Project config file", envvar="PATCHMIG_CONFIG")
]
DEFAULT_CONFIG = Path("project.yaml")


def _load(path: Path) -> ProjectConfig:
    try:
        return load_config(path)
    except ConfigError as e:
        typer.secho(str(e), fg=typer.colors.RED, err=True)
        raise typer.Exit(1) from e


@app.command()
def init(
    directory: Annotated[Path, typer.Argument(help="Project folder")] = Path("."),
) -> None:
    """Create a project folder with an example config to edit."""
    target = directory / "project.yaml"
    if target.exists():
        typer.secho(f"{target} already exists; not overwriting it.", fg=typer.colors.RED, err=True)
        raise typer.Exit(1)
    directory.mkdir(parents=True, exist_ok=True)
    template = files("patchmig.templates").joinpath("project.example.yaml").read_text()
    target.write_text(template)
    typer.echo(f"Created {target}. Edit it, then run: migrate config validate -c {target}")


@config_app.command("validate")
def config_validate(config: ConfigOption = DEFAULT_CONFIG) -> None:
    """Check the config file and report every problem at once."""
    cfg = _load(config)
    typer.secho(f"{config} is valid.", fg=typer.colors.GREEN)
    typer.echo(
        f"  migration: Android {cfg.migration.source_version} -> {cfg.migration.target_version}"
    )
    typer.echo(f"  patch sources: {', '.join(s.type for s in cfg.patch_sources)}")
    typer.echo(f"  output: {cfg.output.target if cfg.output else 'not configured'}")
    missing = [name for name in ("jira", "build", "output") if getattr(cfg, name) is None]
    if missing:
        typer.echo(f"  not configured yet: {', '.join(missing)}")


@config_app.command("show")
def config_show(config: ConfigOption = DEFAULT_CONFIG) -> None:
    """Print the config with defaults filled in and paths resolved."""
    typer.echo(dump_config(_load(config)), nl=False)


@run_app.command("new")
def run_new(
    config: ConfigOption = DEFAULT_CONFIG,
    note: Annotated[str | None, typer.Option(help="Free text shown with the run")] = None,
) -> None:
    """Start a new run, recording a snapshot of the current config."""
    cfg = _load(config)
    run = LedgerStore.open(cfg.ledger_path).create_run(cfg, note=note)
    typer.echo(f"Created run {run.id} (config {run.config_hash[:12]}).")


@app.command()
def status(config: ConfigOption = DEFAULT_CONFIG) -> None:
    """Show patch counts per state for the latest run."""
    cfg = _load(config)
    if not cfg.ledger_path.exists():
        typer.echo("No runs yet. Start one with: migrate run new")
        return
    store = LedgerStore.open(cfg.ledger_path)
    run = store.latest_run()
    if run is None:
        typer.echo("No runs yet. Start one with: migrate run new")
        return
    counts = store.patch_counts(run)
    typer.echo(f"Run {run.id}, started {run.created_at:%Y-%m-%d %H:%M} UTC")
    for state in State:
        if counts[state]:
            typer.echo(f"  {state.value:<15} {counts[state]}")
    typer.echo(f"  {'total':<15} {sum(counts.values())}")


def _jira(cfg: ProjectConfig, enabled: bool) -> JiraClient | None:
    if not enabled or cfg.jira is None:
        return None
    try:
        return HttpJiraClient(cfg.jira)
    except (KeyError, OSError) as e:
        typer.secho(f"Jira token not available ({e}); use --no-jira to skip.", fg="red", err=True)
        raise typer.Exit(1) from e


@app.command("ingest")
def ingest_cmd(
    config: ConfigOption = DEFAULT_CONFIG,
    use_jira: Annotated[bool, typer.Option("--jira/--no-jira", help="Fetch Jira tickets")] = True,
    note: Annotated[str | None, typer.Option(help="Free text shown with the run")] = None,
) -> None:
    """Read the patch list from all configured sources into a new run."""
    cfg = _load(config)
    store = LedgerStore.open(cfg.ledger_path)
    jira = _jira(cfg, use_jira)
    run = store.create_run(cfg, note=note or "ingest")
    try:
        report = ingest(cfg, store, run, jira)
    except IngestError as e:
        typer.secho(f"Ingest failed: {e}", fg=typer.colors.RED, err=True)
        raise typer.Exit(1) from e
    typer.echo(f"Run {run.id}: {report.total} patches")
    for source, n in report.found_per_source.items():
        typer.echo(f"  from {source:<12} {n}")
    if report.duplicates_merged:
        typer.echo(f"  duplicates merged: {report.duplicates_merged}")
    typer.echo("  roles: " + ", ".join(f"{k} {v}" for k, v in sorted(report.roles.items())))
    if jira is not None:
        typer.echo(
            f"  Jira tickets fetched: {report.tickets_fetched} "
            f"(+{report.linked_tickets_fetched} linked)"
        )
        if report.tickets_missing:
            typer.echo(f"  Jira tickets not found: {', '.join(report.tickets_missing)}")
    for w in report.warnings:
        typer.secho(f"  warning: {w}", fg=typer.colors.YELLOW)


@app.command("patches")
def patches_cmd(
    config: ConfigOption = DEFAULT_CONFIG,
    run_id: Annotated[int | None, typer.Option("--run", help="Run id (default: latest)")] = None,
) -> None:
    """List the patches of a run in order."""
    cfg = _load(config)
    store = LedgerStore.open(cfg.ledger_path)
    run_id = _latest_run_id(store, run_id)
    with store.session() as s:
        rows = s.scalars(select(Patch).where(Patch.run_id == run_id).order_by(Patch.seq)).all()
    for p in rows:
        sha = (p.commit_sha or "")[:10] or "(diff)"
        keys = ",".join(p.jira_keys) or "-"
        typer.echo(
            f"{p.seq:>4} {p.state.value:<10} {p.chain_role or '':<7} {sha:<10} "
            f"{keys:<12} {p.project}: {p.subject}"
        )


def _memory(cfg: ProjectConfig):
    """The migration memory if configured and present; None otherwise."""
    from patchmig.memory import Memory, MemoryRepoError

    try:
        return Memory.open(cfg) if cfg.memory.path else None
    except MemoryRepoError:
        return None


def _latest_run_id(store: LedgerStore, run_id: int | None) -> int:
    run_id = run_id or getattr(store.latest_run(), "id", None)
    if run_id is None:
        typer.echo("No runs yet. Start one with: migrate ingest")
        raise typer.Exit(1)
    return run_id


def _adapter(cfg: ProjectConfig) -> CopilotCliAdapter:
    return CopilotCliAdapter(cfg.llm, scratch=cfg.workdir / "briefs")


@app.command("intents")
def intents_cmd(
    config: ConfigOption = DEFAULT_CONFIG,
    run_id: Annotated[int | None, typer.Option("--run", help="Run id (default: latest)")] = None,
    limit: Annotated[int | None, typer.Option(help="Ask the agent for at most N intents")] = None,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Only reuse earlier intents; no agent calls")
    ] = False,
    export: Annotated[
        Path | None, typer.Option(help="Also write a Markdown review sheet to this file")
    ] = None,
    patch: Annotated[
        list[int] | None,
        typer.Option("--patch", help="Write this patch's intent again (repeatable)"),
    ] = None,
    stale: Annotated[
        bool, typer.Option("--stale", help="Write again the intents whose notes changed")
    ] = False,
) -> None:
    """Write an intent for each needed patch (run after analyse; earlier intents are reused).

    After adding, editing or removing notes, `--stale` rewrites only the affected intents.
    """
    cfg = _load(config)
    store = LedgerStore.open(cfg.ledger_path)
    rid = _latest_run_id(store, run_id)
    with store.session() as s:
        run = s.get(Run, rid)
    runner = None if dry_run else AgentRunner(_adapter(cfg), store, run, cfg.llm, cfg.budget)
    report = write_intents(
        cfg, store, run, runner, limit=limit, memory=_memory(cfg), redo=patch, stale=stale
    )
    typer.echo(
        f"Run {rid}: {report.written} written, {report.reused} reused, "
        f"{report.skipped} already had one, {report.not_needed} not needed; "
        f"estimated credits {report.credits:.1f}"
    )
    for failure in report.failed:
        typer.secho(f"  failed: {failure}", fg=typer.colors.YELLOW)
    if report.stopped_by_budget:
        typer.secho(f"  stopped: {report.stopped_by_budget}", fg=typer.colors.YELLOW)
    if export:
        n = export_review_sheet(store, run, export)
        typer.echo(f"Review sheet with {n} intents written to {export}")


@app.command("analyse")
def analyse_cmd(
    config: ConfigOption = DEFAULT_CONFIG,
    run_id: Annotated[int | None, typer.Option("--run", help="Run id (default: latest)")] = None,
) -> None:
    """Decide which patches are still needed, cluster them and forecast each cluster.

    Deterministic: no agent calls, no credits.
    """
    cfg = _load(config)
    store = LedgerStore.open(cfg.ledger_path)
    rid = _latest_run_id(store, run_id)
    with store.session() as s:
        run = s.get(Run, rid)
    try:
        report = analyse(cfg, store, run)
    except AnalysisError as e:
        typer.secho(f"Analysis failed: {e}", fg=typer.colors.RED, err=True)
        raise typer.Exit(1) from e
    typer.echo(f"Run {rid}: {sum(report.states.values())} patches, {report.clusters} clusters")
    for state in State:
        if report.states.get(state.value):
            typer.echo(f"  {state.value:<15} {report.states[state.value]}")
    decided = {k: v for k, v in report.rungs.items() if k != "none"}
    if decided:
        typer.echo("  decided by: " + ", ".join(f"{k} {v}" for k, v in sorted(decided.items())))
    if report.forecasts:
        typer.echo(
            "  clusters to migrate: "
            + ", ".join(f"{k} {v}" for k, v in sorted(report.forecasts.items()))
        )
    for w in report.warnings:
        typer.secho(f"  warning: {w}", fg=typer.colors.YELLOW)


@app.command("clusters")
def clusters_cmd(
    config: ConfigOption = DEFAULT_CONFIG,
    run_id: Annotated[int | None, typer.Option("--run", help="Run id (default: latest)")] = None,
    show: Annotated[
        int | None, typer.Option("--show", help="Print one cluster's details and net diff")
    ] = None,
) -> None:
    """List the clusters of a run in migration order."""
    cfg = _load(config)
    store = LedgerStore.open(cfg.ledger_path)
    rid = _latest_run_id(store, run_id)
    with store.session() as s:
        clusters = s.scalars(
            select(Cluster).where(Cluster.run_id == rid).order_by(Cluster.order_index)
        ).all()
        patches = {p.id: p for p in s.scalars(select(Patch).where(Patch.run_id == rid))}
    if not clusters:
        typer.echo(f"Run {rid} has no clusters yet. Run: migrate analyse")
        return
    for c in clusters:
        if show is not None and c.id != show:
            continue
        level = (c.result.get("forecast") or {}).get("level", "-")
        members = c.result.get("members", [])
        typer.echo(
            f"{c.order_index + 1:>4}. [{c.id}] {c.state.value:<14} {level:<9} "
            f"{len(members)} patch(es): {c.name}"
        )
        if show is None:
            continue
        for pid in members:
            p = patches[pid]
            typer.echo(f"        {p.state.value:<14} {p.project}: {p.subject}")
        for e in c.result.get("edges", []):
            typer.echo(f"        link {e['from']} -> {e['to']}: {e['kind']} {e['detail']}")
        for reason in (c.result.get("forecast") or {}).get("reasons", []):
            typer.echo(f"        forecast: {reason}")
        for error in c.result.get("fold_errors", []):
            typer.secho(f"        fold: {error}", fg=typer.colors.YELLOW)
        if c.net_diff:
            typer.echo("")
            typer.echo(c.net_diff, nl=False)


@app.command("apply")
def apply_cmd(
    config: ConfigOption = DEFAULT_CONFIG,
    run_id: Annotated[int | None, typer.Option("--run", help="Run id (default: latest)")] = None,
    cluster: Annotated[
        list[int] | None, typer.Option("--cluster", help="Only this cluster id (repeatable)")
    ] = None,
    limit: Annotated[int | None, typer.Option(help="Migrate at most N clusters")] = None,
    no_agent: Annotated[
        bool, typer.Option("--no-agent", help="Only cherry-pick and net-diff; no agent calls")
    ] = False,
    stale: Annotated[
        bool, typer.Option("--stale", help="Migrate again the clusters whose notes changed")
    ] = False,
    redo: Annotated[
        bool, typer.Option("--redo", help="Migrate the --cluster clusters again, whatever state")
    ] = False,
    by: Annotated[
        str | None, typer.Option("--by", help="Who reruns (recorded as human:<name>)")
    ] = None,
) -> None:
    """Migrate the needed clusters onto the target tree, one branch per cluster.

    Cherry-pick and net-diff cost nothing; the cluster agent is called only when both fail.
    After a notes or intent change, `--stale` reruns only the clusters made with different
    knowledge (and the clusters built on them); `--redo --cluster N` reruns chosen ones.
    """
    from patchmig import notes as engineer_notes
    from patchmig.mcp_server.server import server_config

    cfg = _load(config)
    store = LedgerStore.open(cfg.ledger_path)
    rid = _latest_run_id(store, run_id)
    with store.session() as s:
        run = s.get(Run, rid)
    if redo and not cluster:
        typer.secho("--redo needs --cluster", fg=typer.colors.RED, err=True)
        raise typer.Exit(1)
    if stale or redo:
        actor = f"human:{by}" if by else "orchestrator"
        if redo:
            ids, why = list(cluster), "rerun requested"
        else:
            found = engineer_notes.stale_clusters(store, run)
            found = [x for x in found if not cluster or x.id in cluster]
            ids, why = [x.id for x in found], "knowledge changed"
            for x in found:
                typer.echo(f"  stale [{x.id}] {x.state:<14} {x.name}: {x.why}")
            if not ids:
                typer.echo(f"Run {rid}: no cluster is stale.")
                return
        try:
            reopened, messages = engineer_notes.rerun(store, run, ids, actor, why)
        except engineer_notes.NoteError as e:
            typer.secho(str(e), fg=typer.colors.RED, err=True)
            raise typer.Exit(1) from e
        for m in messages:
            typer.secho(f"  note: {m}", fg=typer.colors.YELLOW)
        cluster = reopened
    runner = None
    if not no_agent and cfg.migrate.agent:
        runner = AgentRunner(_adapter(cfg), store, run, cfg.llm, cfg.budget)
    report = migrate(
        cfg,
        store,
        run,
        runner,
        cluster_ids=cluster,
        limit=limit,
        mcp=server_config(str(config.resolve()), rid),
        memory=_memory(cfg),
    )
    if not report.outcomes:
        typer.echo(f"Run {rid}: no cluster to migrate (run migrate analyse first?)")
    for o in report.outcomes:
        conf = f" confidence {o.confidence:.2f}" if o.confidence is not None else ""
        typer.echo(f"  [{o.cluster_id}] {o.state:<14} {o.name}{conf}")
        if o.reason:
            typer.echo(f"        {o.reason}")
    if runner is not None:
        typer.echo(f"Estimated credits used in this run so far: {report.credits:.1f}")
    if report.stopped_by_budget:
        typer.secho(f"Stopped: {report.stopped_by_budget}", fg=typer.colors.YELLOW)


@app.command("verify")
def verify_cmd(
    config: ConfigOption = DEFAULT_CONFIG,
    run_id: Annotated[int | None, typer.Option("--run", help="Run id (default: latest)")] = None,
    cluster: Annotated[
        list[int] | None, typer.Option("--cluster", help="Only this cluster id (repeatable)")
    ] = None,
    limit: Annotated[int | None, typer.Option(help="Verify at most N clusters")] = None,
    no_fix: Annotated[
        bool, typer.Option("--no-fix", help="Do not ask the agent to fix build errors")
    ] = False,
) -> None:
    """Build the affected modules of each migrated cluster on the build host, then test it."""
    from patchmig.mcp_server.server import server_config

    cfg = _load(config)
    store = LedgerStore.open(cfg.ledger_path)
    rid = _latest_run_id(store, run_id)
    with store.session() as s:
        run = s.get(Run, rid)
    runner = None if no_fix else AgentRunner(_adapter(cfg), store, run, cfg.llm, cfg.budget)
    try:
        report = verify(
            cfg, store, run, runner, cluster_ids=cluster, limit=limit,
            mcp=server_config(str(config.resolve()), rid),
        )  # fmt: skip
    except VerifyError as e:
        typer.secho(str(e), fg=typer.colors.RED, err=True)
        raise typer.Exit(1) from e
    if not report.outcomes:
        typer.echo(f"Run {rid}: no migrated cluster to verify (run migrate apply first?)")
    for o in report.outcomes:
        typer.echo(f"  [{o.cluster_id}] {o.state:<14} {o.name}")
        if o.reason:
            typer.echo(f"        {o.reason}")
    if report.stopped_by_budget:
        typer.secho(f"Stopped: {report.stopped_by_budget}", fg=typer.colors.YELLOW)


@app.command("build-check")
def build_check_cmd(config: ConfigOption = DEFAULT_CONFIG) -> None:
    """Check the build host: it answers over SSH and every slot is a prepared target tree."""
    cfg = _load(config)
    try:
        checks = check_host(cfg)
    except VerifyError as e:
        typer.secho(str(e), fg=typer.colors.RED, err=True)
        raise typer.Exit(1) from e
    failed = False
    for name, ok, detail in checks:
        typer.secho(f"  {'ok  ' if ok else 'FAIL'} {name}", fg="green" if ok else "red")
        if not ok and detail:
            typer.echo("       " + detail.replace("\n", "\n       "))
        failed |= not ok
    if failed:
        raise typer.Exit(1)


def _open_run(config: Path, run_id: int | None) -> tuple[ProjectConfig, LedgerStore, Run]:
    cfg = _load(config)
    store = LedgerStore.open(cfg.ledger_path)
    rid = _latest_run_id(store, run_id)
    with store.session() as s:
        return cfg, store, s.get(Run, rid)


def _github(cfg: ProjectConfig):
    from patchmig.publish.github import HttpGithub

    if cfg.output is None or cfg.output.github is None:
        typer.secho("No output.github section in the config.", fg=typer.colors.RED, err=True)
        raise typer.Exit(1)
    return HttpGithub(cfg.output.github)


RunOption = Annotated[int | None, typer.Option("--run", help="Run id (default: latest)")]
ByOption = Annotated[str, typer.Option("--by", help="Who decides (recorded as human:<name>)")]


@app.command("publish")
def publish_cmd(
    config: ConfigOption = DEFAULT_CONFIG,
    run_id: RunOption = None,
    cluster: Annotated[
        list[int] | None, typer.Option("--cluster", help="Only this cluster id (repeatable)")
    ] = None,
    unverified: Annotated[
        bool, typer.Option("--unverified", help="Also publish migrated, not yet verified clusters")
    ] = False,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Show what would be pushed")] = False,
) -> None:
    """Push each verified cluster's branch and open (or refresh) one draft PR per repository."""
    from patchmig.publish import PublishError, publish

    cfg, store, run = _open_run(config, run_id)
    try:
        report = publish(cfg, store, run, None if dry_run else _github(cfg), cluster_ids=cluster,
                         include_unverified=unverified, dry_run=dry_run)  # fmt: skip
    except PublishError as e:
        typer.secho(str(e), fg=typer.colors.RED, err=True)
        raise typer.Exit(1) from e
    if not report.outcomes:
        typer.echo(f"Run {run.id}: no verified cluster to publish")
    for o in report.outcomes:
        typer.echo(f"  [{o.cluster_id}] {o.status:<9} {o.name}")
        for url in o.urls:
            typer.echo(f"        {url}")
        if o.reason:
            typer.echo(f"        {o.reason}")


@review_app.command("list")
def review_list(config: ConfigOption = DEFAULT_CONFIG, run_id: RunOption = None) -> None:
    """Clusters by review queue."""
    from patchmig.review import queues

    cfg, store, run = _open_run(config, run_id)
    for queue, clusters in queues(store, run.id, cfg.thresholds).items():
        if not clusters:
            continue
        typer.echo(f"{queue.replace('_', ' ')} ({len(clusters)})")
        for c in clusters:
            conf = f"{c.confidence:.2f}" if c.confidence is not None else "  – "
            prs = [e["url"] for e in (c.result.get("publish") or {}).get("projects", {}).values()]
            typer.echo(f"  [{c.id}] {conf} {c.state.value:<14} {c.name}")
            for url in prs:
                typer.echo(f"        {url}")
            question = (c.result.get("migration") or {}).get("question")
            if queue == "needs_decision" and question:
                typer.echo(f"        question: {question}")


def _review_action(config: Path, run_id: int | None, action) -> None:
    from patchmig.review import Reviewer, ReviewError

    _, store, run = _open_run(config, run_id)
    try:
        c = action(Reviewer(store, run))
    except ReviewError as e:
        typer.secho(str(e), fg=typer.colors.RED, err=True)
        raise typer.Exit(1) from e
    review = (c.result.get("review") or {}).get("decision", "")
    typer.echo(f"Cluster {c.id}: {c.state.value}{f' ({review})' if review else ''}")


@review_app.command("approve")
def review_approve(
    cluster: int, by: ByOption, reason: str = "", config: ConfigOption = DEFAULT_CONFIG,
    run_id: RunOption = None,
) -> None:  # fmt: skip
    """Approve a verified (or dropped) cluster; approved clusters go to the Jira export."""
    _review_action(config, run_id, lambda r: r.approve(cluster, by, reason))


@review_app.command("reject")
def review_reject(
    cluster: int, by: ByOption,
    reason: Annotated[str, typer.Option(help="What is wrong; the cluster agent gets it")],
    config: ConfigOption = DEFAULT_CONFIG, run_id: RunOption = None,
) -> None:  # fmt: skip
    """Send a cluster back to the agent (for a dropped cluster: it is needed after all)."""
    _review_action(config, run_id, lambda r: r.reject(cluster, by, reason))


@review_app.command("retry")
def review_retry(
    cluster: int, by: ByOption, reason: str = "", config: ConfigOption = DEFAULT_CONFIG,
    run_id: RunOption = None,
) -> None:  # fmt: skip
    """Retry a failed cluster, optionally with a hint for the agent."""
    _review_action(config, run_id, lambda r: r.retry(cluster, by, reason))


@review_app.command("decide")
def review_decide(
    cluster: int,
    choice: Annotated[str, typer.Argument(help="migrate, upstream or obsolete")],
    by: ByOption,
    reason: Annotated[str, typer.Option(help="The answer to the cluster's question")],
    config: ConfigOption = DEFAULT_CONFIG, run_id: RunOption = None,
) -> None:  # fmt: skip
    """Answer a needs_decision cluster."""
    _review_action(config, run_id, lambda r: r.decide(cluster, by, choice, reason))


@review_app.command("sync")
def review_sync(config: ConfigOption = DEFAULT_CONFIG, run_id: RunOption = None) -> None:
    """Read the PRs: merges and approvals approve, change requests and closes reject."""
    from patchmig.review import sync_github

    cfg, store, run = _open_run(config, run_id)
    changes = sync_github(store, run, _github(cfg))
    typer.echo("\n".join(changes) if changes else "No changes.")


@app.command("jira-export")
def jira_export_cmd(
    config: ConfigOption = DEFAULT_CONFIG,
    run_id: RunOption = None,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Print what would be written")] = False,
    create_missing: Annotated[
        bool, typer.Option("--create-missing", help="Create tracking tickets for untracked patches")
    ] = False,
) -> None:
    """Write the approved results to Jira: labels, PR links and one marker comment per ticket."""
    from patchmig.jira_export import export, plan

    cfg, store, run = _open_run(config, run_id)
    if cfg.jira is None:
        typer.secho("No jira section in the config.", fg=typer.colors.RED, err=True)
        raise typer.Exit(1)
    if dry_run:
        tickets = plan(cfg, store, run)
        if not tickets:
            typer.echo("Nothing approved to export yet.")
        for t in tickets:
            closing = "  (would close)" if t.close else ""
            typer.echo(
                f"{t.key or 'NEW: ' + (t.summary or '')}  labels: {', '.join(t.labels)}{closing}"
            )
            for url, _, _ in t.links:
                typer.echo(f"  link: {url}")
            typer.echo("  " + t.comment.replace("\n", "\n  ").rstrip())
        return
    report = export(cfg, store, run, HttpJiraClient(cfg.jira), create_missing=create_missing)
    typer.echo(
        f"Updated {len(report.updated)} ticket(s), created {len(report.created)}, "
        f"closed {len(report.closed)}."
    )
    for line in report.skipped:
        hint = " (use --create-missing)" if line.startswith("untracked") else ""
        typer.echo(f"  skipped: {line}{hint}")
    for line in report.errors:
        typer.secho(f"  error: {line}", fg=typer.colors.RED)
    if report.errors:
        raise typer.Exit(1)


@app.command("jira-check")
def jira_check(config: ConfigOption = DEFAULT_CONFIG) -> None:
    """Check the Jira connection: sign-in, the project, and JQL search (read-only)."""
    from patchmig.jira_client import JiraError

    cfg = _load(config)
    if cfg.jira is None:
        typer.echo("The config has no 'jira' section.")
        raise typer.Exit(1)
    client = _jira(cfg, True)
    try:
        for line in client.check(cfg.jira.project):
            typer.secho(f"  ok   {line}", fg="green")
    except JiraError as e:
        typer.secho(f"  FAIL {e}", fg="red")
        raise typer.Exit(1) from e


@app.command("report")
def report_cmd(
    config: ConfigOption = DEFAULT_CONFIG,
    run_id: RunOption = None,
    out: Annotated[Path | None, typer.Option(help="Write the Markdown to this file")] = None,
) -> None:
    """A Markdown summary of a run: outcomes, strategies, queues, builds, credits."""
    from patchmig.report import run_report

    _, store, run = _open_run(config, run_id)
    text = run_report(store, run.id)
    if out:
        out.write_text(text)
        typer.echo(f"Report written to {out}")
    else:
        typer.echo(text, nl=False)


def _open_memory(cfg: ProjectConfig, create: bool = False):
    from patchmig.memory import Memory, MemoryRepoError

    try:
        return Memory.open(cfg, create=create)
    except MemoryRepoError as e:
        typer.secho(str(e), fg=typer.colors.RED, err=True)
        raise typer.Exit(1) from e


@memory_app.command("record")
def memory_record(config: ConfigOption = DEFAULT_CONFIG, run_id: RunOption = None) -> None:
    """Write the run's approved results, code mappings and decisions to the memory repo."""
    cfg, store, run = _open_run(config, run_id)
    mem = _open_memory(cfg, create=True)
    r = mem.record(cfg, store, run)
    typer.echo(f"Recorded {r.patches} patch(es), {r.mappings} mapping(s), {r.decisions} "
               f"decision(s) in {mem.path}" + (f" (commit {r.commit[:12]})" if r.commit else
                                               " (nothing new)"))  # fmt: skip


@memory_app.command("curate")
def memory_curate(config: ConfigOption = DEFAULT_CONFIG, run_id: RunOption = None) -> None:
    """Ask the memory curator for lessons from this run's feedback and fixes (one agent call)."""
    from patchmig.agents.curator import curate

    cfg, store, run = _open_run(config, run_id)
    mem = _open_memory(cfg, create=True)
    runner = AgentRunner(_adapter(cfg), store, run, cfg.llm, cfg.budget)
    r = curate(cfg, store, run, runner, mem)
    if not r.signals:
        typer.echo("Nothing to learn from in this run (no feedback, decisions or fixes).")
        return
    typer.echo(f"{len(r.proposed)} lesson(s) proposed from {r.signals} cluster(s); review them "
               "with: migrate memory list --proposed")  # fmt: skip
    for name in r.proposed:
        typer.echo(f"  {name}")


@memory_app.command("list")
def memory_list(
    config: ConfigOption = DEFAULT_CONFIG,
    proposed: Annotated[bool, typer.Option("--proposed", help="Show proposed lessons")] = False,
) -> None:
    """Lessons in the memory (approved by default)."""
    mem = _open_memory(_load(config))
    lessons = mem.lessons("proposed" if proposed else "approved")
    if not lessons:
        typer.echo("No lessons.")
    for lesson in lessons:
        typer.echo(f"{lesson.name}: {lesson.title}")
        typer.echo(f"    applies to: {', '.join(lesson.applies_to) or 'everything'}")
        typer.echo("    " + lesson.text.replace("\n", "\n    "))


@memory_app.command("approve")
def memory_approve(name: str, by: ByOption, config: ConfigOption = DEFAULT_CONFIG) -> None:
    """Approve a proposed lesson: from now on the cluster agent gets it where it applies."""
    from patchmig.memory import MemoryRepoError

    mem = _open_memory(_load(config))
    try:
        mem.approve(name, by)
    except MemoryRepoError as e:
        typer.secho(str(e), fg=typer.colors.RED, err=True)
        raise typer.Exit(1) from e
    typer.echo(f"Lesson {name} approved.")


@memory_app.command("reject")
def memory_reject(name: str, by: ByOption, config: ConfigOption = DEFAULT_CONFIG) -> None:
    """Drop a proposed lesson."""
    from patchmig.memory import MemoryRepoError

    mem = _open_memory(_load(config))
    try:
        mem.reject(name, by)
    except MemoryRepoError as e:
        typer.secho(str(e), fg=typer.colors.RED, err=True)
        raise typer.Exit(1) from e
    typer.echo(f"Lesson {name} dropped.")


StageOption = Annotated[
    list[str] | None,
    typer.Option("--for", help="analyse (intent writer) or migrate (cluster agent, reviewer, "
                 "build fixer); repeatable; default both"),
]  # fmt: skip


def _note_error(e: Exception):
    typer.secho(str(e), fg=typer.colors.RED, err=True)
    return typer.Exit(1)


def _note_line(n) -> str:
    from patchmig import notes as engineer_notes

    edited = f", edited {len([r for r in n.revisions if 'text' in r])}x" if n.revisions else ""
    return (f"[{n.id}] for {engineer_notes.where(n)} · {'+'.join(n.stages)} · {n.author}"
            f"{edited}\n    " + n.text.replace("\n", "\n    "))  # fmt: skip


@notes_app.command("add")
def notes_add(
    text: Annotated[str, typer.Argument(help="What the agents should know")],
    by: ByOption,
    path: Annotated[str | None, typer.Option(help="Glob over <project>/<file>, e.g. "
                                             "'frameworks/base/services/*/power/*'")] = None,
    patch: Annotated[int | None, typer.Option(help="Patch id; follows the patch to later runs")]
    = None,
    cluster: Annotated[int | None, typer.Option(help="Cluster id of this run")] = None,
    ticket: Annotated[str | None, typer.Option(help="Jira key: every patch carrying it")] = None,
    stage: StageOption = None,
    config: ConfigOption = DEFAULT_CONFIG,
    run_id: RunOption = None,
) -> None:  # fmt: skip
    """Add a note; with no scope option it is for every patch."""
    from patchmig import notes as engineer_notes
    from patchmig.ledger import Ticket
    from patchmig.memory import patch_key

    given = [(k, v) for k, v in (("path", path), ("patch", patch), ("cluster", cluster),
                                 ("ticket", ticket)) if v is not None]  # fmt: skip
    if len(given) > 1:
        raise _note_error(ValueError("give at most one of --path, --patch, --cluster, --ticket"))
    scope, value = given[0] if given else ("all", None)
    cfg, store, run = _open_run(config, run_id)
    target, label = "", ""
    with store.session() as s:
        if scope == "path":
            target = value
        elif scope == "patch":
            p = s.get(Patch, value)
            if p is None or p.run_id != run.id or not (target := patch_key(p) or ""):
                raise _note_error(LookupError(f"patch {value} not in run {run.id}"))
            label = f"#{p.id} {p.subject}"[:200]
        elif scope == "cluster":
            c = s.get(Cluster, value)
            if c is None or c.run_id != run.id:
                raise _note_error(LookupError(f"cluster {value} not in run {run.id}"))
            target, label = str(c.id), c.name
        elif scope == "ticket":
            t = s.get(Ticket, value)
            target, label = value, t.summary if t else ""
    try:
        n = engineer_notes.add(store, text, by, scope, target, label, stage)
    except engineer_notes.NoteError as e:
        raise _note_error(e) from e
    typer.echo(_note_line(n))
    typer.echo("Rerun what it affects with: migrate intents --stale / migrate apply --stale")


@notes_app.command("list")
def notes_list(
    config: ConfigOption = DEFAULT_CONFIG,
    run_id: RunOption = None,
    patch: Annotated[int | None, typer.Option(help="Only notes that reach this patch")] = None,
    cluster: Annotated[int | None, typer.Option(help="Only notes that reach this cluster")]
    = None,
    removed: Annotated[bool, typer.Option("--removed", help="Also removed notes")] = False,
) -> None:  # fmt: skip
    """The notes, or the notes an agent would get for one patch or cluster."""
    from patchmig import notes as engineer_notes
    from patchmig.ledger import Note

    cfg, store, run = _open_run(config, run_id)
    with store.session() as s:
        notes = list(s.scalars(select(Note).order_by(Note.id)))
        p = s.get(Patch, patch) if patch is not None else None
        c = s.get(Cluster, cluster) if cluster is not None else None
    if not removed or p or c:
        notes = [n for n in notes if n.removed_at is None]
    if p is not None:
        notes = [n for n in notes if engineer_notes.applies_to_patch(n, p)]
    if c is not None:
        members = [m for m in run_patches(store, run) if m.cluster_id == c.id]
        paths = engineer_notes.cluster_paths(c, members)
        notes = [
            n
            for n in notes
            if any(engineer_notes.for_cluster([n], c, members, paths, st) for st in n.stages)
        ]
    if not notes:
        typer.echo("No notes.")
    for n in notes:
        typer.echo(_note_line(n) + (" (removed)" if n.removed_at else ""))


def run_patches(store: LedgerStore, run: Run) -> list[Patch]:
    with store.session() as s:
        return list(s.scalars(select(Patch).where(Patch.run_id == run.id)))


@notes_app.command("edit")
def notes_edit(
    note: int,
    by: ByOption,
    text: Annotated[str | None, typer.Option(help="The new text")] = None,
    stage: StageOption = None,
    config: ConfigOption = DEFAULT_CONFIG,
) -> None:
    """Change a note's text or steps (the earlier text is kept in its history)."""
    from patchmig import notes as engineer_notes

    store = LedgerStore.open(_load(config).ledger_path)
    try:
        n = engineer_notes.edit(store, note, by, text, stage)
    except engineer_notes.NoteError as e:
        raise _note_error(e) from e
    typer.echo(_note_line(n))
    typer.echo("Rerun what it affects with: migrate intents --stale / migrate apply --stale")


@notes_app.command("remove")
def notes_remove(note: int, by: ByOption, config: ConfigOption = DEFAULT_CONFIG) -> None:
    """Remove a note; agents stop getting it."""
    from patchmig import notes as engineer_notes

    store = LedgerStore.open(_load(config).ledger_path)
    try:
        engineer_notes.remove(store, note, by)
    except engineer_notes.NoteError as e:
        raise _note_error(e) from e
    typer.echo(f"Note {note} removed.")
    typer.echo("Rerun what it affects with: migrate intents --stale / migrate apply --stale")


@notes_app.command("stale")
def notes_stale(config: ConfigOption = DEFAULT_CONFIG, run_id: RunOption = None) -> None:
    """Intents and migrations made with notes (or intents) that have changed since."""
    from patchmig import notes as engineer_notes

    _, store, run = _open_run(config, run_id)
    intents = engineer_notes.stale_intents(store, run)
    clusters = engineer_notes.stale_clusters(store, run)
    if not intents and not clusters:
        typer.echo(f"Run {run.id}: nothing is stale.")
        return
    if intents:
        typer.echo(f"{len(intents)} intent(s); rerun with: migrate intents --stale")
        for x in intents:
            typer.echo(f"  #{x.id} {x.state:<14} {x.name}")
    if clusters:
        typer.echo(f"{len(clusters)} cluster(s); rerun with: migrate apply --stale")
        for x in clusters:
            typer.echo(f"  [{x.id}] {x.state:<14} {x.name}: {x.why}")
    if intents and clusters:
        typer.echo("Rewrite the intents first: the clusters' briefs include them.")


@app.command("serve")
def serve_cmd(
    config: ConfigOption = DEFAULT_CONFIG,
    host: Annotated[str, typer.Option(help="Interface to listen on")] = "127.0.0.1",
    port: Annotated[int, typer.Option(help="Port")] = 8765,
) -> None:
    """Serve the read-only web front end for this project's ledger."""
    import uvicorn

    from patchmig.web.api import create_app

    cfg = _load(config)
    store = LedgerStore.open(cfg.ledger_path)
    typer.echo(f"Serving {cfg.name} on http://{host}:{port}/ (read-only; Ctrl+C to stop)")
    uvicorn.run(create_app(cfg, store), host=host, port=port, log_level="warning")


@app.command("export-site")
def export_site_cmd(
    out: Annotated[Path, typer.Argument(help="Empty or new folder for the site")],
    config: ConfigOption = DEFAULT_CONFIG,
    run_ids: Annotated[
        list[int] | None, typer.Option("--run", help="Runs to include (default: all)")
    ] = None,
) -> None:
    """Write a static, read-only copy of the front end (e.g. for private GitHub Pages)."""
    from patchmig.web.export import export_site

    cfg = _load(config)
    store = LedgerStore.open(cfg.ledger_path)
    try:
        n = export_site(cfg, store, out, run_ids or None)
    except FileExistsError as e:
        typer.secho(str(e), fg=typer.colors.RED, err=True)
        raise typer.Exit(1) from e
    typer.echo(f"Wrote {n} data files to {out}. Serve it over HTTP, e.g.: "
               f"python -m http.server -d {out}")  # fmt: skip


@app.command("doctor")
def doctor_cmd(
    config: ConfigOption = DEFAULT_CONFIG,
    mcp: Annotated[bool, typer.Option("--mcp", help="Also test the MCP tool server")] = False,
) -> None:
    """Check that Copilot CLI runs headless with our flags (and, with --mcp, our tools)."""
    import subprocess

    from pydantic import BaseModel

    from patchmig.mcp_server.server import SERVER_NAME, server_config

    cfg = _load(config)
    try:
        version = subprocess.run(
            [cfg.llm.executable, "--version"], capture_output=True, text=True, timeout=60
        ).stdout.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        typer.secho(f"Copilot CLI not runnable as '{cfg.llm.executable}': {e}", fg="red")
        raise typer.Exit(1) from e
    typer.echo(f"Copilot CLI: {version or '(no version output)'}")

    class Sum(BaseModel):
        ok: bool
        sum: int

    class PatchCount(BaseModel):
        count: int
        first_subject: str

    cfg.workdir.mkdir(parents=True, exist_ok=True)
    adapter = _adapter(cfg)
    checks: list[tuple[str, AgentTask, object]] = [
        (
            "plain JSON answer",
            AgentTask(
                agent="doctor", key="doctor:sum", workdir=cfg.workdir, schema=Sum,
                brief="Add 17 and 25. Do not use any tools. End your answer with a fenced json "
                'block exactly like: ```json\n{"ok": true, "sum": <the sum>}\n```',
            ),
            lambda d: d.sum == 42,
        )
    ]  # fmt: skip
    if mcp:
        store = LedgerStore.open(cfg.ledger_path)
        rid = _latest_run_id(store, None)
        with store.session() as s:
            rows = s.scalars(select(Patch).where(Patch.run_id == rid).order_by(Patch.seq)).all()
        if not rows:
            typer.echo("The latest run has no patches; run migrate ingest first.")
            raise typer.Exit(1)
        expected_count, expected_first = len(rows), rows[0].subject
        tools = ToolPolicy(
            allow=[SERVER_NAME],
            mcp_servers={SERVER_NAME: server_config(str(config.resolve()), rid)},
        )
        checks.append(
            (
                "MCP tool call",
                AgentTask(
                    agent="doctor", key="doctor:mcp", workdir=cfg.workdir, schema=PatchCount,
                    tools=tools,
                    brief=f"Call the {SERVER_NAME} tool list_patches once. End your answer with "
                    'a fenced json block: {"count": <number of patches>, "first_subject": '
                    "<subject of the first patch>}",
                ),
                lambda d: d.count == expected_count and d.first_subject == expected_first,
            )
        )  # fmt: skip
    failed = False
    for name, task, check in checks:
        try:
            answer = adapter.run(task, 1)
            data = task.schema.model_validate(extract_json(answer.text))
            good = check(data)
        except (AgentRuntimeError, ValueError) as e:
            typer.secho(f"  FAIL {name}: {e}", fg=typer.colors.RED)
            failed = True
            continue
        status = "ok  " if good else "WRONG"
        typer.secho(f"  {status} {name} ({answer.duration_s:.1f}s)", fg="green" if good else "red")
        failed |= not good
    if failed:
        typer.echo("See docs/spike-m3.md for what to adjust (llm.mcp_flag, llm.extra_args).")
        raise typer.Exit(1)


if __name__ == "__main__":
    app()
