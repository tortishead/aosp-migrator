"""The `migrate` command line."""

from __future__ import annotations

from importlib.resources import files
from pathlib import Path
from typing import Annotated

import typer
from sqlalchemy import select

from patchmig.config import ConfigError, ProjectConfig, dump_config, load_config
from patchmig.ingest import IngestError, ingest
from patchmig.jira_client import HttpJiraClient, JiraClient
from patchmig.ledger import LedgerStore, Patch, State

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
    run_id = run_id or getattr(store.latest_run(), "id", None)
    if run_id is None:
        typer.echo("No runs yet. Start one with: migrate ingest")
        return
    with store.session() as s:
        rows = s.scalars(select(Patch).where(Patch.run_id == run_id).order_by(Patch.seq)).all()
    for p in rows:
        sha = (p.commit_sha or "")[:10] or "(diff)"
        keys = ",".join(p.jira_keys) or "-"
        typer.echo(
            f"{p.seq:>4} {p.state.value:<10} {p.chain_role or '':<7} {sha:<10} "
            f"{keys:<12} {p.project}: {p.subject}"
        )


if __name__ == "__main__":
    app()
