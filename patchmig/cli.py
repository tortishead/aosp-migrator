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

app = typer.Typer(help="Migrate OEM Android framework patches between Android versions.")
config_app = typer.Typer(help="Inspect and validate the project configuration.")
run_app = typer.Typer(help="Manage migration runs.")
app.add_typer(config_app, name="config")
app.add_typer(run_app, name="run")

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
        typer.echo(f"  Jira tickets fetched: {report.tickets_fetched}")
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
) -> None:
    """Write an intent statement for each patch (reusing earlier ones by fingerprint)."""
    cfg = _load(config)
    store = LedgerStore.open(cfg.ledger_path)
    rid = _latest_run_id(store, run_id)
    with store.session() as s:
        run = s.get(Run, rid)
    runner = None if dry_run else AgentRunner(_adapter(cfg), store, run, cfg.llm, cfg.budget)
    report = write_intents(cfg, store, run, runner, limit=limit)
    typer.echo(
        f"Run {rid}: {report.written} written, {report.reused} reused, "
        f"{report.skipped} already had one; estimated credits {report.credits:.1f}"
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
