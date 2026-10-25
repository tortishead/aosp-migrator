"""The `migrate` command line."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer
from sqlalchemy import select

from patchmig import steps
from patchmig.agents.adapter import AgentRuntimeError, AgentTask, ToolPolicy, extract_json
from patchmig.agents.copilot import CopilotCliAdapter
from patchmig.confidence import describe
from patchmig.config import ConfigError, ProjectConfig, dump_config, load_config, new_project
from patchmig.jira_client import HttpJiraClient, JiraClient
from patchmig.ledger import Cluster, LedgerStore, Patch, Run, State
from patchmig.verify import VerifyError, check_host

app = typer.Typer(help="Migrate OEM Android framework patches between Android versions.")
config_app = typer.Typer(help="Inspect and validate the project configuration.")
run_app = typer.Typer(help="Manage migration runs.")
review_app = typer.Typer(help="Review queues and human decisions on clusters.")
memory_app = typer.Typer(help="The migration memory: facts and lessons from earlier migrations.")
notes_app = typer.Typer(help="Engineer notes: extra knowledge for the agents, per scope and step.")
backlog_app = typer.Typer(help="The backlog: one item per Jira ticket (or patch without one).")
app.add_typer(config_app, name="config")
app.add_typer(run_app, name="run")
app.add_typer(review_app, name="review")
app.add_typer(memory_app, name="memory")
app.add_typer(notes_app, name="notes")
app.add_typer(backlog_app, name="backlog")

ConfigOption = Annotated[
    Path, typer.Option("--config", "-c", help="Project config file", envvar="PATCHMIG_CONFIG")
]
DEFAULT_CONFIG = Path("project.yaml")
WorkspaceOption = Annotated[
    Path | None,
    typer.Option(help="Serve every <folder>/project.yaml in this folder (instead of --config)"),
]
RunOption = Annotated[int | None, typer.Option("--run", help="Run id (default: latest)")]
ByOption = Annotated[str, typer.Option("--by", help="Who decides (recorded as human:<name>)")]


def _load(path: Path) -> ProjectConfig:
    try:
        return load_config(path)
    except ConfigError as e:
        typer.secho(str(e), fg=typer.colors.RED, err=True)
        raise typer.Exit(1) from e


_COLORS = {"ok": "green", "warn": "yellow", "error": "red", "step": "cyan"}


def _echo(text: str, level: str) -> None:
    typer.secho(text, fg=_COLORS.get(level))


def _step(config: Path, fn, **options):
    """Run a step (patchmig.steps, shared with the web front end) and print what it says."""
    cfg = _load(config)
    ctx = steps.Context(config, cfg, LedgerStore.open(cfg.ledger_path), _echo)
    try:
        return fn(ctx, **options)
    except steps.StepFailed as e:
        if str(e):
            typer.secho(str(e), fg=typer.colors.RED, err=True)
        raise typer.Exit(1) from e


@app.command()
def init(
    directory: Annotated[Path, typer.Argument(help="Project folder")] = Path("."),
) -> None:
    """Create a project folder with an example config to edit."""
    try:
        target = new_project(directory)
    except FileExistsError as e:
        typer.secho(f"{e} already exists; not overwriting it.", fg=typer.colors.RED, err=True)
        raise typer.Exit(1) from e
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
    note: Annotated[str | None, typer.Option(help="Free text shown with a new run")] = None,
    item: Annotated[
        list[str] | None,
        typer.Option("--item", help="Import only this ticket again, or a new one (repeatable)"),
    ] = None,
    new_run: Annotated[
        bool, typer.Option("--new-run", help="Start a new backlog instead of refreshing it")
    ] = False,
    run_id: RunOption = None,
) -> None:
    """Import Jira tickets and patches into the backlog (the first time: a new run).

    Again: refreshes the backlog; patches already there keep their analysis.
    """
    _step(config, steps.ingest, run_id=run_id, use_jira=use_jira, note=note, new_run=new_run,
          items=item)  # fmt: skip


@app.command("patches")
def patches_cmd(
    config: ConfigOption = DEFAULT_CONFIG,
    run_id: RunOption = None,
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
    if run_id is not None:
        with store.session() as s:
            if s.get(Run, run_id) is None:
                typer.secho(f"There is no run {run_id}.", fg=typer.colors.RED, err=True)
                raise typer.Exit(1)
        return run_id
    latest = store.latest_run()
    if latest is None:
        typer.echo("No runs yet. Start one with: migrate ingest")
        raise typer.Exit(1)
    return latest.id


def _open_run(config: Path, run_id: int | None) -> tuple[ProjectConfig, LedgerStore, Run]:
    """The config, the ledger and the run (the latest unless --run names one)."""
    cfg = _load(config)
    store = LedgerStore.open(cfg.ledger_path)
    rid = _latest_run_id(store, run_id)
    with store.session() as s:
        return cfg, store, s.get(Run, rid)


def _adapter(cfg: ProjectConfig) -> CopilotCliAdapter:
    return CopilotCliAdapter(cfg.llm, scratch=cfg.workdir / "briefs")


@app.command("intents")
def intents_cmd(
    config: ConfigOption = DEFAULT_CONFIG,
    run_id: RunOption = None,
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
    _step(config, steps.intents, run_id=run_id, limit=limit, dry_run=dry_run, patches=patch,
          stale=stale, export=export)  # fmt: skip


@app.command("history")
def history_cmd(
    config: ConfigOption = DEFAULT_CONFIG,
    run_id: RunOption = None,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Only report; fetch nothing")] = False,
) -> None:
    """Deepen shallow (repo sync --depth=1) checkouts as far as the upstream checks need.

    Source: every project of a git_range source (else the run's patch projects) gets its OEM
    range. Target: the run's patch projects get their history back to the source base, or
    trees.history_commits commits. Run before ingest for git_range, and after ingest.
    """
    _step(config, steps.history, run_id=run_id, dry_run=dry_run)


@app.command("analyse")
def analyse_cmd(
    config: ConfigOption = DEFAULT_CONFIG,
    run_id: RunOption = None,
) -> None:
    """Decide which patches are still needed, cluster them and forecast each cluster.

    Deterministic: no agent calls, no credits.
    """
    _step(config, steps.analyse, run_id=run_id)


@app.command("clusters")
def clusters_cmd(
    config: ConfigOption = DEFAULT_CONFIG,
    run_id: RunOption = None,
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
    run_id: RunOption = None,
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
    _step(config, steps.apply, run_id=run_id, clusters=cluster, limit=limit, no_agent=no_agent,
          stale=stale, redo=redo, by=by)  # fmt: skip


@app.command("verify")
def verify_cmd(
    config: ConfigOption = DEFAULT_CONFIG,
    run_id: RunOption = None,
    cluster: Annotated[
        list[int] | None, typer.Option("--cluster", help="Only this cluster id (repeatable)")
    ] = None,
    limit: Annotated[int | None, typer.Option(help="Verify at most N clusters")] = None,
    no_fix: Annotated[
        bool, typer.Option("--no-fix", help="Do not ask the agent to fix build errors")
    ] = False,
) -> None:
    """Build the affected modules of each migrated cluster on the build host, then test it."""
    _step(config, steps.verify, run_id=run_id, clusters=cluster, limit=limit, no_fix=no_fix)


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
    _step(config, steps.publish, run_id=run_id, clusters=cluster, unverified=unverified,
          dry_run=dry_run)  # fmt: skip


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
            conf = describe(c.confidence) if c.confidence is not None else "  – "
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
    _step(config, steps.review_sync, run_id=run_id)


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
    _step(config, steps.jira_export, run_id=run_id, dry_run=dry_run,
          create_missing=create_missing)  # fmt: skip


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
    _step(config, steps.memory_record, run_id=run_id)


@memory_app.command("curate")
def memory_curate(config: ConfigOption = DEFAULT_CONFIG, run_id: RunOption = None) -> None:
    """Ask the memory curator for lessons from this run's feedback and fixes (one agent call)."""
    _step(config, steps.memory_curate, run_id=run_id)


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

    given = [(k, v) for k, v in (("path", path), ("patch", patch), ("cluster", cluster),
                                 ("ticket", ticket)) if v is not None]  # fmt: skip
    if len(given) > 1:
        raise _note_error(ValueError("give at most one of --path, --patch, --cluster, --ticket"))
    scope, value = given[0] if given else ("all", None)
    cfg, store, run = _open_run(config, run_id)
    try:
        n = engineer_notes.add_for_run(store, run, text, by, scope, value, stage)
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


@backlog_app.command("list")
def backlog_list(config: ConfigOption = DEFAULT_CONFIG, run_id: RunOption = None) -> None:
    """The backlog items in import order, with their patches and status."""
    from patchmig.web import data

    _, store, run = _open_run(config, run_id)
    rows = data.backlog(store, run.id)
    if not rows:
        typer.echo(f"Run {run.id}: the backlog is empty. Fill it with: migrate ingest")
    for i in rows:
        states = ", ".join(f"{k} {v}" for k, v in sorted(i["states"].items())) or "no patch"
        added = f", {i['added']} added" if i["added"] else ""
        typer.echo(f"{i['key']:<14} {i['status']:<20} {states}{added}  {i['title']}")


@backlog_app.command("show")
def backlog_show(
    key: Annotated[str, typer.Argument(help="Jira key, or patch:<id>")],
    config: ConfigOption = DEFAULT_CONFIG,
    run_id: RunOption = None,
) -> None:
    """Everything the item holds: ticket, comments, attachments, patches, additions, analysis."""
    from patchmig.web import data

    _, store, run = _open_run(config, run_id)
    try:
        i = data.backlog_item(store, run.id, key)
    except data.NotFound as e:
        raise _note_error(e) from e
    typer.echo(f"{i['key']}: {i['title']} ({i['status']})")
    if t := i["ticket"]:
        typer.echo(f"  Jira status: {t['status']}; linked: "
                   f"{', '.join(x['key'] for x in t['linked']) or '-'}")  # fmt: skip
        for c in t["comments"]:
            typer.echo(f"  comment: {c[:200]}")
        for a in t["attachments"]:
            typer.echo(f"  attachment: {a['filename']} "
                       f"({a.get('path') and 'kept' or a.get('note') or 'listed'})")  # fmt: skip
        for r in t["remote_links"]:
            typer.echo(f"  link: {r['url']} {r.get('title') or ''}".rstrip())
    for p in i["patches"]:
        typer.echo(f"  patch #{p['id']} {p['state']:<10} {p['project']}: {p['subject']}")
    for a in i["added"]:
        what = a.get("url") or a.get("filename")
        gone = " (removed)" if a.get("removed_at") else ""
        typer.echo(f"  added [{a['id']}] {a['kind']}: {what} by {a['by']}{gone}")
    for n in i["notes"]:
        typer.echo(f"  note [{n['id']}] {n['author']}: {n['text']}")
    if i["analysis"]:
        typer.echo(f"  analysed {i['analysed_at']} ({i['earlier_analyses']} earlier)")


@backlog_app.command("add")
def backlog_add(
    key: Annotated[str, typer.Argument(help="Jira key, or patch:<id>")],
    by: ByOption,
    text: Annotated[str | None, typer.Option(help="Text: becomes a note for the agents")] = None,
    link: Annotated[str | None, typer.Option(help="A commit/PR link, or any other link")] = None,
    file: Annotated[Path | None, typer.Option(help="A .diff/.patch, a log, a document")] = None,
    stage: StageOption = None,
    config: ConfigOption = DEFAULT_CONFIG,
    run_id: RunOption = None,
) -> None:
    """Add information to a backlog item; then import and analyse it again."""
    from patchmig import backlog as bl
    from patchmig.notes import NoteError

    cfg, store, run = _open_run(config, run_id)
    try:
        content = file.read_bytes() if file else None
        done = bl.add_info(cfg, store, run, key, by, text=text, link=link,
                           filename=file.name if file else None, content=content,
                           stages=stage)  # fmt: skip
    except (bl.BacklogError, NoteError, OSError) as e:
        raise _note_error(e) from e
    typer.echo(f"Added to {key}: " + (f"note {done['note']}" if "note" in done
                                      else f"[{done['id']}] {done['kind']}"))  # fmt: skip
    typer.echo(f"Rerun it with: migrate ingest --item {key} && migrate analyse --item {key}")


@backlog_app.command("remove")
def backlog_remove(
    key: Annotated[str, typer.Argument(help="Jira key, or patch:<id>")],
    addition: Annotated[int, typer.Argument(help="Id of the addition (see backlog show)")],
    by: ByOption,
    config: ConfigOption = DEFAULT_CONFIG,
    run_id: RunOption = None,
) -> None:
    """Mark a link or file added to an item as removed (it stays in the history)."""
    from patchmig import backlog as bl

    _, store, run = _open_run(config, run_id)
    try:
        bl.remove_info(store, run, key, addition, by)
    except bl.BacklogError as e:
        raise _note_error(e) from e
    typer.echo(f"Removed addition {addition} from {key}.")


@app.command("serve")
def serve_cmd(
    config: ConfigOption = DEFAULT_CONFIG,
    host: Annotated[str, typer.Option(help="Interface to listen on")] = "127.0.0.1",
    port: Annotated[int, typer.Option(help="Port")] = 8765,
    read_only: Annotated[
        bool, typer.Option("--read-only", help="Only show results: no actions, no settings edits")
    ] = False,
    admin_token: Annotated[
        bool | None,
        typer.Option(
            "--admin-token/--no-admin-token",
            help="Require the admin token for actions and settings edits "
            "(default: off, unless PATCHMIG_ADMIN_TOKEN is set)",
        ),
    ] = None,
    secrets_dir: Annotated[
        Path | None,
        typer.Option(
            help="Where tokens typed in the browser are stored "
            "(default: .patchmig/secrets next to the config)"
        ),
    ] = None,  # fmt: skip
    workspace: WorkspaceOption = None,
) -> None:
    """Serve the web front end: run every step, review, notes, lessons and settings there.

    Actions need no admin token by default: anyone who can reach the server can run steps
    and edit the settings. With --admin-token (or PATCHMIG_ADMIN_TOKEN set) they need it:
    the one in PATCHMIG_ADMIN_TOKEN, or the one printed at start.
    To start it at boot, see `migrate service-file`.
    Without a valid config the server starts with only the settings, to create one.
    With --workspace it serves every <folder>/project.yaml in that folder instead of one
    config: switch between them in the header, and create new ones in the browser.
    """
    import os
    import secrets

    import uvicorn

    from patchmig.config.editor import SettingsEditor
    from patchmig.web.api import create_app

    if admin_token is None:
        admin_token = "PATCHMIG_ADMIN_TOKEN" in os.environ
    token = None
    if not read_only and admin_token:
        token = os.environ.get("PATCHMIG_ADMIN_TOKEN") or secrets.token_urlsafe(18)
    if workspace is not None:
        from patchmig.web.workspace import create_workspace_app

        if secrets_dir is not None:
            message = ("--secrets-dir needs one config: in a workspace each project keeps its own "
                       "(.patchmig/secrets in its folder).")  # fmt: skip
            typer.secho(message, fg=typer.colors.RED, err=True)
            raise typer.Exit(1)
        workspace.mkdir(parents=True, exist_ok=True)
        web = create_workspace_app(workspace, token, read_only, token_required=admin_token)
        name = f"workspace {workspace.resolve()}"
    else:
        try:
            cfg = load_config(config)
            store = LedgerStore.open(cfg.ledger_path)
            name = cfg.name
        except ConfigError as e:
            if read_only:
                typer.secho(str(e), fg=typer.colors.RED, err=True)
                raise typer.Exit(1) from e
            cfg, store, name = None, None, str(config)
            typer.secho(f"{e}\nStarting with the settings only: fix the config in the browser.",
                        fg=typer.colors.YELLOW)  # fmt: skip
        web = create_app(cfg, store, SettingsEditor(config, secrets_dir), token,
                         token_required=admin_token)  # fmt: skip
    typer.echo(f"Serving {name} on http://{host}:{port}/ (Ctrl+C to stop)")
    if read_only:
        typer.echo("Read-only: results only, no actions and no settings edits.")
    elif not admin_token:
        typer.echo("Actions and settings: no admin token needed (--admin-token to require one).")
        if host not in ("127.0.0.1", "localhost", "::1"):
            typer.secho(f"Warning: anyone who can reach {host}:{port} can run steps and edit "
                        "the settings.", fg=typer.colors.YELLOW)  # fmt: skip
    elif "PATCHMIG_ADMIN_TOKEN" in os.environ:
        typer.echo("Actions and settings: admin token from PATCHMIG_ADMIN_TOKEN.")
    else:
        typer.echo(f"Actions and settings: admin token for this session: {token}")
    uvicorn.run(web, host=host, port=port, log_level="warning")


@app.command("service-file")
def service_file_cmd(
    config: ConfigOption = DEFAULT_CONFIG,
    host: Annotated[str, typer.Option(help="Interface to listen on")] = "127.0.0.1",
    port: Annotated[int, typer.Option(help="Port")] = 8765,
    user: Annotated[str | None, typer.Option(help="Run as this user (default: you)")] = None,
    workspace: WorkspaceOption = None,
) -> None:
    """Print a systemd unit that starts `migrate serve` at boot and restarts it if it stops.

    Save it as /etc/systemd/system/patchmig.service, then: systemctl enable --now patchmig.
    The admin token and the tokens given as env: references go in the environment file.
    """
    import getpass
    import shlex
    import shutil
    import sys

    if workspace is not None:
        path, folder = workspace.resolve(), workspace.resolve()
        served = ["--workspace", str(path)]
    else:
        path = config.resolve()
        folder, served = path.parent, ["--config", str(path)]
    env_file = folder / ".patchmig" / "serve.env"
    exe = shutil.which("migrate")
    start = [exe, "serve"] if exe else [sys.executable, "-m", "patchmig.cli", "serve"]
    start += [*served, "--host", host, "--port", str(port), "--admin-token"]
    typer.echo(f"""# patchmig web front end for {path}
# 1. Save as /etc/systemd/system/patchmig.service
# 2. Put PATCHMIG_ADMIN_TOKEN=<a long random string> in {env_file}, with any tokens the
#    config names as env: references (chmod 600 the file; tokens typed in the browser are
#    stored in .patchmig/secrets and need nothing here).
# 3. sudo systemctl daemon-reload && sudo systemctl enable --now patchmig
[Unit]
Description=Patch migration front end ({folder.name})
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User={user or getpass.getuser()}
WorkingDirectory={folder}
EnvironmentFile=-{env_file}
ExecStart={shlex.join(start)}
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target""")


@app.command("check")
def check_cmd(
    what: Annotated[
        list[str] | None, typer.Argument(help="trees, hosts, copilot, jira, build (default: all)")
    ] = None,
    config: ConfigOption = DEFAULT_CONFIG,
) -> None:
    """Connection tests: trees, git host tokens, Copilot sign-in, Jira, build host. No credits."""
    from patchmig.checks import CHECKS, run_check

    cfg = _load(config)
    unknown = sorted(set(what or []) - set(CHECKS))
    if unknown:
        typer.secho(f"unknown checks {unknown}; known: {', '.join(CHECKS)}", fg="red", err=True)
        raise typer.Exit(2)
    failed = False
    for name in what or CHECKS:
        typer.echo(name)
        for title, ok, detail in run_check(name, cfg):
            typer.secho(
                f"  {'ok  ' if ok else 'FAIL'} {title}: {detail}", fg="green" if ok else "red"
            )
            failed |= not ok
    if failed:
        raise typer.Exit(1)


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
