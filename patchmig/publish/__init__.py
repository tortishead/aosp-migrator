"""Publish: one PR per approved cluster and repository (GitHub, GitLab, Bitbucket).

Nothing leaves the orchestrator before a human approved it: only clusters that are verified
(built and tested) and approved in the review are pushed. For each, every target project with
migrated commits is pushed to its repo on the
output host (output.<target>.repo_map) as `<branch_prefix><project name>/r<run>-c<cluster>`,
and a PR (GitLab: merge request) is opened against the target branch, or updated if it
exists. A cluster spanning several repos gets one PR per repo, linked to each other. The PR
body carries what a reviewer needs: intent, original patches, how each project was migrated,
what the agents said, build and test results, and who approved it. Publishing again skips
clusters whose pushed commits are unchanged; a cluster reworked and approved again (after a
change request on its PR) is force-pushed to the same PR. A merged PR is never pushed to.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import select

from patchmig.confidence import describe
from patchmig.config import ProjectConfig
from patchmig.gitutil import GitError
from patchmig.knowledge.trees import Trees
from patchmig.ledger import Cluster, LedgerStore, Patch, Run, State
from patchmig.migrate.workspace import namespace
from patchmig.publish.hosts import (
    ReviewApi,
    ReviewHostError,
    git_url_for,
    output_of,
    push,
    push_env,
)
from patchmig.review import is_approved, queue_of

MARKER = "<!-- patchmig:cluster -->"


class PublishError(Exception):
    pass


@dataclass
class PublishOutcome:
    cluster_id: int
    name: str
    status: str  # published, updated, skipped, failed
    urls: list[str] = field(default_factory=list)
    reason: str = ""


@dataclass
class PublishReport:
    run_id: int
    outcomes: list[PublishOutcome] = field(default_factory=list)


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def branch_for(config: ProjectConfig, run_id: int, cluster_id: int) -> str:
    return f"{config.output.branch_prefix}{namespace(config.name)}/r{run_id}-c{cluster_id}"


def pr_title(config: ProjectConfig, cluster: Cluster) -> str:
    m = config.migration
    return f"[A{m.source_version}→A{m.target_version}] {cluster.name}"


def _members(store: LedgerStore, cluster: Cluster) -> list[Patch]:
    with store.session() as s:
        return list(
            s.scalars(select(Patch).where(Patch.cluster_id == cluster.id).order_by(Patch.seq))
        )


def pr_body(
    config: ProjectConfig,
    cluster: Cluster,
    members: list[Patch],
    project: str,
    siblings: dict[str, str],
) -> str:
    r = cluster.result
    mig = r.get("migration") or {}
    ver = r.get("verification") or {}
    queue = queue_of(cluster, config.thresholds).replace("_", " ")
    conf = describe(cluster.confidence)
    m = config.migration
    review = r.get("review") or {}
    approved = (f"**Approved by {review.get('by')}** in patchmig"
                + (f": {review['reason']}" if review.get("reason") else "")
                if is_approved(cluster) else f"**Review queue: {queue}**")  # fmt: skip
    lines = [
        f"Migration of an OEM patch cluster from Android {m.source_version} to Android "
        f"{m.target_version} (`{m.label}`), prepared by patchmig. {approved}, "
        f"confidence {conf}, state `{cluster.state}`.",
        "",
        "## Original patches",
        "",
        "| Patch | Project | Jira | State |",
        "| --- | --- | --- | --- |",
    ]
    for p in members:
        sha = (p.commit_sha or "diff")[:12]
        lines.append(
            f"| `{sha}` {p.subject} | {p.project} | {', '.join(p.jira_keys) or '–'} | {p.state} |"
        )
    intents = []
    for p in members:
        if p.intent:  # problem, change and how to verify; components and sources stay out
            intents += [f"- **{p.subject}**", *(f"  - {x}" for x in p.intent.splitlines()[:3])]
    if intents:
        lines += ["", "## Intent", "", *intents]
    lines += ["", "## How it was migrated", ""]
    for proj, info in mig.get("projects", {}).items():
        mark = " (this PR)" if proj == project else ""
        lines.append(
            f"- `{proj}`{mark}: {(info.get('strategy') or 'not changed').replace('_', '-')}"
        )
    final = (mig.get("agent") or {}).get("final")
    if final:
        lines += ["", f"Cluster agent ({final['outcome']}, confidence "
                  f"{describe(final['confidence'])}): {final['summary']}"]  # fmt: skip
        lines += [f"- Risk to check: {risk}" for risk in final.get("risks", [])]
    soft = (mig.get("guardrails") or {}).get("soft", [])
    lines += [f"- Note: {s}" for s in soft]
    review = mig.get("review")
    if review and not review.get("error"):
        lines += ["", f"Reviewer agent ({review['verdict']}, confidence "
                  f"{describe(review['confidence'])}): {review['summary']}"]  # fmt: skip
        lines += [f"- {issue}" for issue in review.get("issues", [])]
    if ver:
        sel = ver.get("selection") or {}
        builds = ver.get("builds", [])
        tests = ver.get("tests")
        lines += [
            "",
            "## Build and test",
            "",
            f"- Modules: {', '.join(f'`{x}`' for x in sel.get('modules', [])) or '–'}",
            f"- Build: {'ok' if builds and builds[-1]['ok'] else 'failed'}"
            + (f" after {len(builds) - 1} agent fix(es)" if len(builds) > 1 else ""),
            f"- Tests: {', '.join(f'`{x}`' for x in sel.get('tests', [])) or 'none selected'}"
            + (f": {'passed' if tests['ok'] else 'failed'}" if tests else ""),
            f"- Result: {ver.get('reason', '')}",
        ]
    feedback = r.get("feedback", [])
    if feedback:
        lines += ["", "## Earlier review feedback", ""]
        lines += [f"- {f['by']}: {f['text']}" for f in feedback]
    others = {p: u for p, u in siblings.items() if p != project}
    if others:
        lines += ["", "## Same cluster in other repositories", ""]
        lines += [f"- `{p}`: {u}" for p, u in others.items()]
    lines += [
        "",
        "Merge this PR to finish the cluster; request changes (or close it) to send it back to "
        "the cluster agent with your comments.",
        "",
        f"{MARKER} run {cluster.run_id}, cluster {cluster.id}",
    ]
    return "\n".join(lines) + "\n"


class Publisher:
    def __init__(self, config: ProjectConfig, store: LedgerStore, run: Run, api: ReviewApi):
        try:
            self.kind, self.gh = output_of(config)
        except ReviewHostError as e:
            raise PublishError(str(e)) from e
        self.config = config
        self.store = store
        self.run = run
        self.api = api
        self.trees = Trees(config)

    def _clusters(self, cluster_ids) -> list[Cluster]:
        with self.store.session() as s:
            rows = s.scalars(
                select(Cluster).where(Cluster.run_id == self.run.id).order_by(Cluster.order_index)
            ).all()
        # A human approves first: only then does a cluster leave the orchestrator.
        return [
            c
            for c in rows
            if c.state == State.VERIFIED
            and is_approved(c)
            and (cluster_ids is None or c.id in cluster_ids)
        ]

    def publish(self, cluster_ids=None, dry_run=False) -> PublishReport:
        report = PublishReport(run_id=self.run.id)
        for cluster in self._clusters(cluster_ids):
            report.outcomes.append(self._publish(cluster, dry_run))
        return report

    def _publish(self, cluster: Cluster, dry_run: bool) -> PublishOutcome:
        projects = {
            p: info
            for p, info in (cluster.result.get("migration") or {}).get("projects", {}).items()
            if info.get("head") and info["head"] != self.trees.target_base(p)
        }
        if not projects:
            return PublishOutcome(cluster.id, cluster.name, "skipped", reason="no commits")
        pushed = (cluster.result.get("publish") or {}).get("projects", {})
        if all((pushed.get(p) or {}).get("head") == info["head"] for p, info in projects.items()):
            urls = [pushed[p]["url"] for p in projects]
            return PublishOutcome(cluster.id, cluster.name, "skipped", urls,
                                  "already published, unchanged since")  # fmt: skip
        unmapped = [p for p in projects if p not in self.gh.repo_map]
        if unmapped:
            return PublishOutcome(
                cluster.id, cluster.name, "failed",
                reason=f"no repo for {', '.join(unmapped)} in output.{self.kind}.repo_map",
            )  # fmt: skip
        branch = branch_for(self.config, self.run.id, cluster.id)
        if dry_run:
            repos = ", ".join(f"{self.gh.repo_map[p]}:{branch}" for p in projects)
            return PublishOutcome(cluster.id, cluster.name, "dry run", reason=f"would push {repos}")
        members = _members(self.store, cluster)
        record = dict(cluster.result.get("publish") or {"projects": {}})
        record["projects"] = dict(record.get("projects", {}))
        env = push_env(self.config)
        status = "updated" if record["projects"] else "published"
        title = pr_title(self.config, cluster)
        merged_on_host: set[str] = set()
        try:
            for project, info in projects.items():
                repo = self.gh.repo_map[project]
                url = git_url_for(self.config, repo)
                base = self.gh.base_branch_map.get(repo, self.gh.base_branch)
                pr = self.api.find_pr(repo, branch)
                merged = pr is not None and (pr.get("merged") or pr.get("merged_at"))
                if merged:  # done on the host: leave it as it is
                    merged_on_host.add(project)
                    continue
                push(self.trees.target_repo(project), info["head"], url, branch, env)
                if pr is None or pr.get("state") == "closed":
                    body = pr_body(self.config, cluster, members, project, {})
                    pr = self.api.create_pr(
                        repo, title, body, branch, base, self.config.output.draft
                    )
                    self.api.add_labels(repo, pr["number"], self.gh.labels)
                    self.api.request_reviewers(repo, pr["number"], self.gh.reviewers)
                entry = record["projects"].get(project, {})
                if entry.get("number") == pr["number"] and entry.get("head") != info["head"]:
                    # Reworked after review: ask the reviewers to look again.
                    self.api.request_reviewers(repo, pr["number"], self.gh.reviewers)
                record["projects"][project] = {
                    **entry, "repo": repo, "branch": branch, "base": base,
                    "number": pr["number"], "url": pr["html_url"], "head": info["head"],
                }  # fmt: skip
            urls = {p: e["url"] for p, e in record["projects"].items() if p in projects}
            for project in projects:
                if project in merged_on_host or project not in record["projects"]:
                    continue
                e = record["projects"][project]
                body = pr_body(self.config, cluster, members, project, urls)
                self.api.update_pr(e["repo"], e["number"], title, body)
        except (GitError, ReviewHostError) as err:
            self._save(cluster, record | {"error": str(err), "at": _now()})
            return PublishOutcome(cluster.id, cluster.name, "failed", reason=str(err)[:500])
        record.pop("error", None)
        record["at"] = _now()
        self._save(cluster, record)
        return PublishOutcome(cluster.id, cluster.name, status, list(urls.values()))

    def _save(self, cluster: Cluster, record: dict) -> None:
        with self.store.session() as s, s.begin():
            row = s.get(Cluster, cluster.id)
            row.result = {**row.result, "publish": record}


def publish(
    config: ProjectConfig,
    store: LedgerStore,
    run: Run,
    api: ReviewApi,
    *,
    cluster_ids: list[int] | None = None,
    dry_run: bool = False,
) -> PublishReport:
    return Publisher(config, store, run, api).publish(cluster_ids, dry_run)
