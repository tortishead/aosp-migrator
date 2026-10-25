"""Ingest (import): read tickets and patches from every configured source into the backlog.

A run's backlog lasts. The first import fills it; every later one refreshes it: tickets are
read again, patches not seen before are added (patches already there keep their analysis and
state), and backlog items are added for new tickets. `items` imports only some tickets again
(or a new one by its key), with what engineers added to them (links and files).

What fails for one ticket (Jira refuses it, rate-limits too long, a download breaks) does not
stop the import: it is kept with the item (`import_problems`), and `incomplete` lists the
items to import again once Jira answers.
"""

from __future__ import annotations

import contextlib
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import select

from patchmig.config import ProjectConfig
from patchmig.ingest.manifest import ManifestError, manifest_projects, tree_projects
from patchmig.ingest.normalize import (
    NormalizedPatch,
    link_patches,
    merge_duplicates,
    normalize,
)
from patchmig.ingest.sources import (
    ADAPTERS,
    SourceContext,
    attachment_path,
    from_additions,
    from_ticket,
)
from patchmig.jira_client import JiraClient, JiraError
from patchmig.jira_client import Ticket as JiraTicket
from patchmig.ledger import BacklogItem, LedgerStore, Patch, Run, Ticket


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
    tickets_without_patch: list[str] = field(default_factory=list)
    attachments_kept: int = 0
    new_patches: int = 0  # not in the run before
    known_patches: int = 0  # already there: refreshed, analysis kept
    items_new: list[str] = field(default_factory=list)
    items_refreshed: int = 0
    warnings: list[str] = field(default_factory=list)
    problems: dict[str, list[str]] = field(default_factory=dict)  # by ticket key
    items_incomplete: list[str] = field(default_factory=list)


_EPOCH = datetime.min.replace(tzinfo=UTC)
MAX_TICKET_FAILURES = 5


def collect(
    config: ProjectConfig,
    jira: JiraClient | None = None,
    items: list[str] | None = None,
    additions: dict[str, list[dict]] | None = None,
) -> tuple[list[NormalizedPatch], dict[str, int], SourceContext]:
    """Read and normalise all sources; return the merged, linked, ordered patch set.

    `items` reads only these tickets instead of the sources; `additions` are what engineers
    added to tickets (by key), read along with them.
    """
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
    if items is None:
        for src in config.patch_sources:
            try:
                raws = ADAPTERS[src.type](src, ctx)
            except JiraError as e:  # e.g. the JQL query itself is rejected
                raise IngestError(f"{src.type}: {e}") from e
            found[src.type] += len(raws)
            normalized += [normalize(r, jira_projects) for r in raws]
    else:
        for key in items:
            if not key.startswith("patch:"):  # a patch without a ticket has nothing to read
                raws = from_ticket(ctx, key)
                found["ticket"] += len(raws)
                normalized += [normalize(r, jira_projects) for r in raws]
    for key, added in (additions or {}).items():
        if items is None or key in items:
            raws = from_additions(ctx, key, added)
            found["added"] += len(raws)
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


def incomplete(store: LedgerStore, run: Run) -> dict[str, list[str]]:
    """The ticket items not imported completely, with why: what failed at their last import,
    or (from before that was kept) that Jira never gave the ticket."""
    with store.session() as s:
        rows = s.scalars(select(BacklogItem).where(
            BacklogItem.run_id == run.id, BacklogItem.kind == "ticket"
        ).order_by(BacklogItem.id)).all()  # fmt: skip
        read = set(s.scalars(select(Ticket.key).where(Ticket.key.in_([r.key for r in rows]))))
    out = {}
    for r in rows:
        why = list(r.import_problems or [])
        if r.key not in read and not why:
            why = ["not read from Jira"]
        if why:
            out[r.key] = why
    return out


def _fetch_tickets(
    config: ProjectConfig, store: LedgerStore, jira: JiraClient, keys: set[str],
    report: IngestReport, ctx: SourceContext | None = None,
) -> None:  # fmt: skip
    """Cache the tickets and, one level deep, the tickets they link to."""
    linked = _cache_tickets(config, store, jira, keys, report, ctx)
    _cache_tickets(config, store, jira, linked - keys, report, ctx, linked_only=True)


def _remote_links(jira: JiraClient, key: str, ctx: SourceContext | None, report) -> list[dict]:
    if ctx is not None and ctx.jira is jira:
        return ctx.remote_links(key)
    if not hasattr(jira, "remote_links"):
        return []
    try:
        return jira.remote_links(key)
    except JiraError as e:
        _problem(report, key, f"jira: {key} remote links: {e}")
        return []


def _problem(report: IngestReport, key: str, message: str) -> None:
    report.warnings.append(message)
    report.problems.setdefault(key, []).append(message)


def _keep_attachments(
    config: ProjectConfig, jira: JiraClient, ticket: JiraTicket, report: IngestReport
) -> list[dict]:
    """Download every attachment of the ticket up to jira.attachment_max_kb, for the analysis.

    A copy already there (same size) is not fetched again. Larger attachments and those that
    fail are listed with the reason, so the analysis knows they exist.
    """
    max_kb = config.jira.attachment_max_kb if config.jira else 2048
    kept = []
    for a in ticket.attachments:
        entry = {**a, "path": None, "note": ""}
        path = attachment_path(config.workdir / "downloads", ticket.key, a["filename"])
        if (a.get("size") or 0) > max_kb * 1024:
            entry["note"] = f"larger than {max_kb} kB: listed only"
        elif path.is_file() and (not a.get("size") or path.stat().st_size == a["size"]):
            entry["path"] = str(path)
        elif not hasattr(jira, "download"):
            entry["note"] = "not downloaded: this Jira connection cannot download"
        else:
            try:
                data = jira.download(a["url"], max_kb * 1024)
            except JiraError as e:
                _problem(report, ticket.key, f"jira: {ticket.key} attachment {a['filename']}: {e}")
                entry["note"] = f"download failed: {e}"
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)
                entry["path"] = str(path)
        report.attachments_kept += entry["path"] is not None
        kept.append(entry)
    return kept


def _cache_tickets(
    config: ProjectConfig, store: LedgerStore, jira: JiraClient, keys: set[str],
    report: IngestReport, ctx: SourceContext | None = None, linked_only: bool = False,
) -> set[str]:  # fmt: skip
    links: set[str] = set()
    failures = 0
    todo = sorted(keys)
    for n, key in enumerate(todo):
        try:
            ticket = jira.get(key)
        except JiraError as e:
            # One unreadable ticket (say, a 403) must not cost the others; a run of
            # failures means Jira itself is down or the token is wrong.
            _problem(report, key, f"jira: {key}: {e}")
            failures += 1
            if failures >= MAX_TICKET_FAILURES:
                report.warnings.append(f"jira: stopped after {failures} failed tickets in a row; "
                                       f"{len(todo) - n - 1} not read")  # fmt: skip
                for rest in todo[n + 1 :]:
                    report.problems.setdefault(rest, []).append(
                        "jira: not read (the import stopped after repeated failures)"
                    )
                return links
            continue
        failures = 0
        if ticket is None:
            if not linked_only:
                report.tickets_missing.append(key)
            continue
        links |= set(ticket.issue_links)
        if linked_only:  # context for the analysis: listed, not downloaded
            attachments = [{**a, "path": None, "note": "linked ticket: listed only"}
                           for a in ticket.attachments]  # fmt: skip
            remote: list[dict] = []
        else:
            attachments = _keep_attachments(config, jira, ticket, report)
            remote = _remote_links(jira, key, ctx, report)
        with store.session() as s, s.begin():
            s.merge(
                Ticket(
                    key=ticket.key,
                    summary=ticket.summary,
                    description=ticket.description,
                    status=ticket.status,
                    issue_links=ticket.issue_links,
                    comments=ticket.comments,
                    attachments=attachments,
                    remote_links=remote,
                    fetched_at=datetime.now(UTC),
                )
            )
        if linked_only:
            report.linked_tickets_fetched += 1
        else:
            report.tickets_fetched += 1
    return links


def ingest(
    config: ProjectConfig,
    store: LedgerStore,
    run: Run,
    jira: JiraClient | None = None,
    items: list[str] | None = None,
) -> IngestReport:
    """Import into the run's backlog: a first import, a refresh, or only `items` again."""
    with store.session() as s:
        backlog = {i.key: i for i in s.scalars(select(BacklogItem).where(
            BacklogItem.run_id == run.id))}  # fmt: skip
    additions = {k: i.added for k, i in backlog.items() if i.kind == "ticket" and i.added}
    unknown = [k for k in items or [] if k.startswith("patch:") and k not in backlog]
    if unknown:
        raise IngestError(f"not in the backlog of run {run.id}: {', '.join(unknown)}")

    patches, found, ctx = collect(config, jira, items, additions)
    report = IngestReport(
        run_id=run.id,
        found_per_source=found,
        total=len(patches),
        duplicates_merged=sum(found.values()) - len(patches),
        roles=dict(Counter(p.chain_role for p in patches)),
        warnings=ctx.warnings,
        problems=ctx.problems,
    )
    _merge_into_run(store, run, patches, report)
    with_patch = {k for p in patches for k in p.jira_keys}
    if jira is not None:
        report.tickets_without_patch = sorted(set(ctx.tickets) - with_patch)
        _fetch_tickets(config, store, jira, with_patch | set(ctx.tickets), report, ctx)
    if items is None:
        wanted = list(ctx.tickets)
    else:
        wanted = [k for k in items if not k.startswith("patch:")
                  and (k in ctx.tickets or k in with_patch or k in backlog)]  # fmt: skip
        if missing := [k for k in items if not k.startswith("patch:") and k not in wanted]:
            report.warnings.append(f"no ticket and no patch for {', '.join(missing)}: not added")
    _sync_backlog(store, run, wanted, report, refresh_only=items)
    return report


def _union(old: list, new: list) -> list:
    return list(dict.fromkeys([*(old or []), *(new or [])]))


def _merge_links(old: dict, new: dict) -> dict:
    out = dict(old or {})
    for k, v in (new or {}).items():
        out[k] = _union(out.get(k, []), v) if isinstance(v, list) else out.get(k, v)
    return out


def _merge_into_run(
    store: LedgerStore, run: Run, patches: list[NormalizedPatch], report: IngestReport
) -> None:
    """Add the patches the run does not have yet; refresh sources, keys and links of the rest.

    Identity as in merge_duplicates: the commit, else the patch-id when one side has no
    commit. A patch already in the run keeps its state, verdict, intent and cluster.
    """
    with store.session() as s, s.begin():
        rows = s.scalars(select(Patch).where(Patch.run_id == run.id)).all()
        by_sha = {r.commit_sha: r for r in rows if r.commit_sha}
        by_fp: dict[str, Patch] = {}
        for r in rows:
            if r.fingerprint:
                by_fp.setdefault(r.fingerprint, r)
        seq = max((r.seq for r in rows), default=-1) + 1
        for p in patches:
            row = by_sha.get(p.commit_sha) if p.commit_sha else None
            if row is None and p.fingerprint in by_fp:
                candidate = by_fp[p.fingerprint]
                if candidate.commit_sha is None or p.commit_sha is None:
                    row = candidate
            if row is not None:
                row.sources = _union(row.sources, p.sources)
                row.jira_keys = _union(row.jira_keys, p.jira_keys)
                row.links = _merge_links(row.links, p.links)
                row.diff_path = row.diff_path or p.diff_path
                report.known_patches += 1
                continue
            row = Patch(
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
            s.add(row)
            seq += 1
            report.new_patches += 1
            if p.commit_sha:
                by_sha[p.commit_sha] = row
            if p.fingerprint:
                by_fp.setdefault(p.fingerprint, row)


def _sync_backlog(
    store: LedgerStore,
    run: Run,
    tickets: list[str],
    report: IngestReport,
    refresh_only: list[str] | None = None,
) -> None:
    """One backlog item per ticket and per patch without a ticket.

    Ticket items: the tickets the sources found, and the keys of patches no item covers yet
    (a commit saying "Bug: OEM-12"). A patch that came in through a ticket carries that key,
    so linked implementation tickets do not become items of their own.
    """
    now = datetime.now(UTC)
    with store.session() as s, s.begin():
        items = {i.key: i for i in s.scalars(
            select(BacklogItem).where(BacklogItem.run_id == run.id))}  # fmt: skip
        patches = s.scalars(select(Patch).where(Patch.run_id == run.id).order_by(Patch.seq)).all()
        wanted: dict[str, tuple[str, list[str]]] = {k: ("ticket", ["jira"]) for k in tickets}
        covered = {k for k, i in items.items() if i.kind == "ticket"} | set(wanted)
        for p in patches:
            keys = p.jira_keys or []
            if not keys:
                wanted.setdefault(f"patch:{p.id}", ("patch", p.sources))
            elif not covered.intersection(keys):
                for k in keys:
                    wanted.setdefault(k, ("ticket", p.sources))
                covered |= set(keys)
            for k in keys:
                if k in wanted and wanted[k][0] == "ticket":
                    wanted[k] = ("ticket", _union(wanted[k][1], p.sources))
        if refresh_only is not None:
            wanted = {k: v for k, v in wanted.items() if k in refresh_only}
        cached = {t.key: t for t in s.scalars(select(Ticket).where(Ticket.key.in_(
            [k for k, (kind, _) in wanted.items() if kind == "ticket"])))}  # fmt: skip
        subjects = {f"patch:{p.id}": p.subject for p in patches}
        for p in reversed(patches):  # the first patch of a ticket names it when Jira cannot
            for k in p.jira_keys or []:
                subjects[k] = p.subject
        for key, (kind, sources) in wanted.items():
            title = cached[key].summary if key in cached else subjects.get(key, key)
            item = items.get(key)
            if item is None:
                item = BacklogItem(run_id=run.id, key=key, kind=kind, title=title,
                                   sources=sources, added=[], analyses=[])  # fmt: skip
                s.add(item)
                report.items_new.append(key)
            else:
                item.title, item.refreshed_at = title or item.title, now
                item.sources = _union(item.sources, sources)
                report.items_refreshed += 1
            # Each import says afresh: an item read completely this time has no problems.
            item.import_problems = list(dict.fromkeys(report.problems.get(key, [])))
            if item.import_problems:
                report.items_incomplete.append(key)
        # A patch that has gained a ticket belongs to the ticket's item from now on.
        for p in patches:
            item = items.get(f"patch:{p.id}")
            if p.jira_keys and item is not None and not item.analysis and not item.added:
                s.delete(item)
