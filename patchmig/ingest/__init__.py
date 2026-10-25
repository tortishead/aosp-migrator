"""Ingest: read the patch list from every configured source into the ledger."""

from __future__ import annotations

import contextlib
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import func, select

from patchmig.config import ProjectConfig
from patchmig.ingest.manifest import ManifestError, manifest_projects, tree_projects
from patchmig.ingest.normalize import (
    NormalizedPatch,
    link_patches,
    merge_duplicates,
    normalize,
)
from patchmig.ingest.sources import ADAPTERS, SourceContext
from patchmig.jira_client import JiraClient, JiraError
from patchmig.ledger import LedgerStore, Patch, Run, Ticket


class IngestError(Exception):
    pass


@dataclass
class IngestReport:
    run_id: int
    found_per_source: dict[str, int]
    total: int
    duplicates_merged: int
    roles: dict[str, int]
    tickets_fetched: int = 0
    linked_tickets_fetched: int = 0
    tickets_missing: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


_EPOCH = datetime.min.replace(tzinfo=UTC)
MAX_TICKET_FAILURES = 5


def collect(
    config: ProjectConfig, jira: JiraClient | None = None
) -> tuple[list[NormalizedPatch], dict[str, int], SourceContext]:
    """Read and normalise all sources; return the merged, linked, ordered patch set."""
    source = config.trees.source
    if source.path.is_dir():
        try:
            projects = tree_projects(source.path, source.manifest)
        except ManifestError as e:
            raise IngestError(str(e)) from e
        # A manifest can list projects the checkout leaves out (groups, partial syncs).
        absent = [p for p in projects if not (source.path / p).is_dir()]
        projects = [p for p in projects if p not in absent]
        if not projects:
            raise IngestError(f"no projects found in the source tree {source.path}")
        ctx = SourceContext(config=config, projects=projects, jira=jira)
        if absent:
            ctx.warn(f"{len(absent)} manifest projects are not checked out in the source tree "
                     f"and were skipped, e.g. {absent[0]}")  # fmt: skip
    else:
        ctx = _without_tree(config, jira)
    jira_projects = {config.jira.project} if config.jira else None

    found: Counter[str] = Counter()
    normalized: list[NormalizedPatch] = []
    for src in config.patch_sources:
        try:
            raws = ADAPTERS[src.type](src, ctx)
        except JiraError as e:  # e.g. the JQL query itself is rejected
            raise IngestError(f"{src.type}: {e}") from e
        found[src.type] += len(raws)
        normalized += [normalize(r, jira_projects) for r in raws]

    patches = merge_duplicates(normalized)
    # Oldest first; patches without a date (plain diffs) keep their order at the end.
    patches.sort(key=lambda p: p.authored_at or _EPOCH.replace(year=9999))
    _history_order(patches, ctx)
    link_patches(patches)
    return patches, dict(found), ctx


def _without_tree(config: ProjectConfig, jira: JiraClient | None) -> SourceContext:
    """Jira tickets need no source checkout: their patches come from attachments and links.

    Diffs are placed in projects by the manifest (if it can be read without the tree) and
    the hosts' repo_map. Every other source reads the tree.
    """
    source = config.trees.source
    if needs := sorted({s.type for s in config.patch_sources if s.type != "jira_query"}):
        raise IngestError(f"the source tree {source.path} is not a folder (trees.source.path; "
                          "a relative path is relative to the config file's folder); "
                          f"{', '.join(needs)} needs it, only jira_query works without "
                          "it")  # fmt: skip
    projects: list[str] = []
    if source.manifest:
        with contextlib.suppress(ManifestError):
            projects = manifest_projects(source.path / source.manifest)
    ctx = SourceContext(config=config, projects=projects, jira=jira, has_tree=False)
    ctx.warn(f"jira_query: no source tree at {source.path}: commits that mention a ticket are "
             "not searched, linked commits come from the code host, and diffs are placed by "
             + ("the manifest and " if projects else "") + "the hosts' repo_map")  # fmt: skip
    return ctx


def _history_order(patches: list[NormalizedPatch], ctx: SourceContext) -> None:
    """Put each project's commits in history order, within the places the date sort gave them.

    Author dates survive rebases and cherry-picks, so on a rebased OEM branch a fix can carry
    an older date than the change it fixes, or a revert than the commit it reverts. The order
    on the branch is the truth: chains and reverts are linked along it.
    """
    slots: dict[str, list[int]] = {}
    for i, p in enumerate(patches):
        if p.commit_sha:
            slots.setdefault(p.project, []).append(i)
    for project, indexes in slots.items():
        if len(indexes) < 2:
            continue
        rank = {sha: n for n, (sha, _) in enumerate(ctx.range_log(project))}
        ranked = [i for i in indexes if patches[i].commit_sha in rank]
        in_order = sorted((patches[i] for i in ranked), key=lambda p: rank[p.commit_sha])
        for i, p in zip(ranked, in_order, strict=True):
            patches[i] = p


def _fetch_tickets(store: LedgerStore, jira: JiraClient, keys: set[str], report: IngestReport):
    """Cache the patches' tickets and, one level deep, the tickets they link to."""
    linked = _cache_tickets(store, jira, keys, report)
    _cache_tickets(store, jira, linked - keys, report, linked_only=True)


def _cache_tickets(
    store: LedgerStore, jira: JiraClient, keys: set[str], report: IngestReport,
    linked_only: bool = False,
) -> set[str]:  # fmt: skip
    links: set[str] = set()
    failures = 0
    with store.session() as s, s.begin():
        for key in sorted(keys):
            try:
                ticket = jira.get(key)
            except JiraError as e:
                # One unreadable ticket (say, a 403) must not cost the others; a run of
                # failures means Jira itself is down or the token is wrong.
                report.warnings.append(f"jira: {key}: {e}")
                failures += 1
                if failures >= MAX_TICKET_FAILURES:
                    report.warnings.append(
                        f"jira: stopped after {failures} failed tickets in a row"
                    )
                    return links
                continue
            failures = 0
            if ticket is None:
                if not linked_only:
                    report.tickets_missing.append(key)
                continue
            links |= set(ticket.issue_links)
            s.merge(
                Ticket(
                    key=ticket.key,
                    summary=ticket.summary,
                    description=ticket.description,
                    status=ticket.status,
                    issue_links=ticket.issue_links,
                    comments=ticket.comments,
                    fetched_at=datetime.now(UTC),
                )
            )
            if linked_only:
                report.linked_tickets_fetched += 1
            else:
                report.tickets_fetched += 1
    return links


def ingest(
    config: ProjectConfig, store: LedgerStore, run: Run, jira: JiraClient | None = None
) -> IngestReport:
    with store.session() as s:
        existing = s.scalar(select(func.count()).select_from(Patch).where(Patch.run_id == run.id))
    if existing:
        raise IngestError(f"run {run.id} already has {existing} patches; start a new run")

    patches, found, ctx = collect(config, jira)
    report = IngestReport(
        run_id=run.id,
        found_per_source=found,
        total=len(patches),
        duplicates_merged=sum(found.values()) - len(patches),
        roles=dict(Counter(p.chain_role for p in patches)),
        warnings=ctx.warnings,
    )
    with store.session() as s, s.begin():
        for seq, p in enumerate(patches):
            s.add(
                Patch(
                    run_id=run.id,
                    seq=seq,
                    sources=p.sources,
                    project=p.project,
                    commit_sha=p.commit_sha,
                    diff_path=p.diff_path,
                    change_id=p.change_id,
                    fingerprint=p.fingerprint,
                    subject=p.subject,
                    message=p.message,
                    author=p.author,
                    authored_at=p.authored_at,
                    jira_keys=p.jira_keys,
                    links=p.links,
                    footprint=p.footprint.to_json(),
                    chain_role=p.chain_role,
                )
            )
    if jira is not None:
        _fetch_tickets(store, jira, {k for p in patches for k in p.jira_keys}, report)
    return report
