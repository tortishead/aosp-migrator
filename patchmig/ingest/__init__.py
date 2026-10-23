"""Ingest: read the patch list from every configured source into the ledger."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import func, select

from patchmig.config import ProjectConfig
from patchmig.ingest.manifest import ManifestError, tree_projects
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


def collect(
    config: ProjectConfig, jira: JiraClient | None = None
) -> tuple[list[NormalizedPatch], dict[str, int], SourceContext]:
    """Read and normalise all sources; return the merged, linked, ordered patch set."""
    source = config.trees.source
    try:
        projects = tree_projects(source.path, source.manifest)
    except ManifestError as e:
        raise IngestError(str(e)) from e
    if not projects:
        raise IngestError(f"no projects found in the source tree {source.path}")
    ctx = SourceContext(config=config, projects=projects, jira=jira)
    jira_projects = {config.jira.project} if config.jira else None

    found: Counter[str] = Counter()
    normalized: list[NormalizedPatch] = []
    for src in config.patch_sources:
        raws = ADAPTERS[src.type](src, ctx)
        found[src.type] += len(raws)
        normalized += [normalize(r, jira_projects) for r in raws]

    patches = merge_duplicates(normalized)
    # Oldest first; patches without a date (plain diffs) keep their order at the end.
    patches.sort(key=lambda p: p.authored_at or _EPOCH.replace(year=9999))
    link_patches(patches)
    return patches, dict(found), ctx


def _fetch_tickets(store: LedgerStore, jira: JiraClient, keys: set[str], report: IngestReport):
    """Cache the patches' tickets and, one level deep, the tickets they link to."""
    linked = _cache_tickets(store, jira, keys, report)
    _cache_tickets(store, jira, linked - keys, report, linked_only=True)


def _cache_tickets(
    store: LedgerStore, jira: JiraClient, keys: set[str], report: IngestReport,
    linked_only: bool = False,
) -> set[str]:  # fmt: skip
    links: set[str] = set()
    with store.session() as s, s.begin():
        for key in sorted(keys):
            try:
                ticket = jira.get(key)
            except JiraError as e:
                report.warnings.append(f"jira: {e}")
                return links
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
