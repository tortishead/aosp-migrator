"""The human side: review queues, decisions, and the GitHub review round-trip.

Every cluster lands in one queue:

- quick_approve: verified, confidence at or above thresholds.quick_approve
- careful_review: verified, lower confidence
- failed / needs_decision: the agent or the checks gave up, or a human must choose
- dropped: every patch is upstream, obsolete or reverted; a human confirms
- in_progress: still to migrate or verify
- approved: a human approved it (directly, or by approving or merging its PR)

A rejection sends the cluster back to `needed` with the reviewer's words, which the cluster
agent gets on the next `migrate apply`. Decisions are recorded in cluster.result["review"]
and cluster.result["feedback"], and every state change goes through LedgerStore.transition
with the human as actor.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select

from patchmig.config.schema import ThresholdsConfig
from patchmig.ledger import Cluster, LedgerStore, Patch, Run, State
from patchmig.ledger.states import DROPPED, MIGRATED

QUEUES = (
    "quick_approve",
    "careful_review",
    "needs_decision",
    "failed",
    "dropped",
    "in_progress",
    "approved",
)
APPROVABLE = frozenset({State.VERIFIED, *DROPPED})


class ReviewError(Exception):
    pass


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def is_approved(cluster: Cluster) -> bool:
    """A human approved the cluster (and it was not reopened since): it is done."""
    review = cluster.result.get("review") or {}
    return review.get("decision") == "approved" and cluster.state in APPROVABLE


def below_quick(thresholds: ThresholdsConfig) -> float:
    """The highest confidence that still keeps a cluster out of the quick-approve queue."""
    return round(max(thresholds.quick_approve - 0.01, 0.0), 2)


def queue_of(cluster: Cluster, thresholds: ThresholdsConfig) -> str:
    if is_approved(cluster):
        return "approved"
    if cluster.state == State.VERIFIED:
        conf = cluster.confidence or 0.0
        return "quick_approve" if conf >= thresholds.quick_approve else "careful_review"
    if cluster.state == State.FAILED:
        return "failed"
    if cluster.state == State.NEEDS_DECISION:
        return "needs_decision"
    if cluster.state in DROPPED:
        return "dropped"
    return "in_progress"


def queues(store: LedgerStore, run_id: int, thresholds: ThresholdsConfig) -> dict[str, list]:
    with store.session() as s:
        clusters = s.scalars(
            select(Cluster).where(Cluster.run_id == run_id).order_by(Cluster.order_index)
        ).all()
    out: dict[str, list] = {q: [] for q in QUEUES}
    for c in clusters:
        out[queue_of(c, thresholds)].append(c)
    return out


@dataclass
class Reviewer:
    store: LedgerStore
    run: Run

    def _cluster(self, cluster_id: int) -> Cluster:
        with self.store.session() as s:
            c = s.get(Cluster, cluster_id)
        if c is None or c.run_id != self.run.id:
            raise ReviewError(f"cluster {cluster_id} is not in run {self.run.id}")
        return c

    def _members(self, cluster: Cluster, states) -> list[Patch]:
        with self.store.session() as s:
            return list(
                s.scalars(
                    select(Patch)
                    .where(Patch.cluster_id == cluster.id, Patch.state.in_(list(states)))
                    .order_by(Patch.seq)
                )
            )

    def _update(self, cluster: Cluster, **fields) -> None:
        with self.store.session() as s, s.begin():
            row = s.get(Cluster, cluster.id)
            row.result = {**row.result, **fields}

    def _move(self, cluster: Cluster, states, target: State, actor: str, reason: str) -> None:
        for m in self._members(cluster, states):
            if m.state != target:
                self.store.transition(m, target, actor=actor, reason=reason)
        self.store.transition(cluster, target, actor=actor, reason=reason)

    def approve(self, cluster_id: int, by: str, reason: str = "", source: str = "cli") -> Cluster:
        c = self._cluster(cluster_id)
        if c.state not in APPROVABLE:
            raise ReviewError(
                f"cluster {cluster_id} is {c.state}; only verified or dropped clusters can be "
                "approved"
            )
        review = {"decision": "approved", "by": by, "at": _now(), "reason": reason,
                  "source": source}  # fmt: skip
        self._update(c, review=review)
        return self._cluster(cluster_id)

    def reject(self, cluster_id: int, by: str, reason: str, source: str = "cli") -> Cluster:
        """Back to `needed`, with the reason as feedback for the next migration attempt.

        For a dropped cluster this means: the patches are needed after all.
        """
        if not reason.strip():
            raise ReviewError("a rejection needs a reason; the agent works from it")
        c = self._cluster(cluster_id)
        if c.state not in MIGRATED | APPROVABLE:
            raise ReviewError(f"cluster {cluster_id} is {c.state}; nothing to reject")
        feedback = [*c.result.get("feedback", []),
                    {"by": by, "at": _now(), "text": reason, "source": source}]  # fmt: skip
        self._update(c, feedback=feedback, review={"decision": "rejected", "by": by,
                                                   "at": _now(), "reason": reason,
                                                   "source": source})  # fmt: skip
        self._move(c, MIGRATED | APPROVABLE, State.NEEDED, f"human:{by}", f"rejected: {reason}")
        return self._cluster(cluster_id)

    def retry(self, cluster_id: int, by: str, reason: str = "") -> Cluster:
        c = self._cluster(cluster_id)
        if c.state != State.FAILED:
            raise ReviewError(f"cluster {cluster_id} is {c.state}; only failed clusters retry")
        if reason:
            feedback = [*c.result.get("feedback", []),
                        {"by": by, "at": _now(), "text": reason, "source": "retry"}]  # fmt: skip
            self._update(c, feedback=feedback)
        self._move(c, {State.FAILED}, State.NEEDED, f"human:{by}", reason or "retry")
        return self._cluster(cluster_id)

    def decide(self, cluster_id: int, by: str, choice: str, reason: str) -> Cluster:
        """Answer a needs_decision cluster: 'migrate', 'upstream' or 'obsolete'."""
        targets = {"migrate": State.NEEDED, "upstream": State.UPSTREAM,
                   "obsolete": State.OBSOLETE}  # fmt: skip
        if choice not in targets:
            raise ReviewError(f"choice must be one of {', '.join(targets)}")
        if not reason.strip():
            raise ReviewError("a decision needs a reason")
        c = self._cluster(cluster_id)
        if c.state != State.NEEDS_DECISION:
            raise ReviewError(f"cluster {cluster_id} is {c.state}, not needs_decision")
        question = (c.result.get("migration") or {}).get("question")
        decisions = [*c.result.get("decisions", []),
                     {"by": by, "at": _now(), "choice": choice, "reason": reason,
                      "question": question}]  # fmt: skip
        fields = {"decisions": decisions}
        if choice == "migrate":
            fields["feedback"] = [*c.result.get("feedback", []),
                                  {"by": by, "at": _now(), "source": "decision",
                                   "text": f"Decision on '{question or 'the open question'}': "
                                           f"{reason}"}]  # fmt: skip
        else:  # the human confirms the drop; that is the approval
            fields["review"] = {"decision": "approved", "by": by, "at": _now(),
                                "reason": reason, "source": "decision"}  # fmt: skip
        self._update(c, **fields)
        self._move(
            c, {State.NEEDS_DECISION, State.NEEDED}, targets[choice], f"human:{by}",
            f"decision: {choice}: {reason}",
        )  # fmt: skip
        return self._cluster(cluster_id)


def sync_reviews(store: LedgerStore, run: Run, api, source: str = "github") -> list[str]:
    """Bring PR outcomes into the ledger; returns one line per change.

    Clusters are approved in patchmig before they are published, so a PR is a second look in
    the target's own review. A merge is recorded on the PR (publish never pushes to it again);
    a review requesting changes (Bitbucket: needs work) rejects the cluster with the review
    text as feedback, and so does a PR closed without merge: the cluster goes back to the
    cluster agent, and after the rework and a new approval publish updates the same PR. An
    approving review or a merge approves a cluster not approved yet. Each review, and each
    close, is acted on once. A cluster whose PRs cannot be read is reported and skipped.
    `source` names the host in the record.
    """
    from patchmig.publish.hosts import ReviewHostError  # patchmig.publish imports this module

    reviewer = Reviewer(store, run)
    changes: list[str] = []
    with store.session() as s:
        ids = s.scalars(
            select(Cluster.id).where(Cluster.run_id == run.id).order_by(Cluster.order_index)
        ).all()
    for cid in ids:
        c = reviewer._cluster(cid)
        prs = (c.result.get("publish") or {}).get("projects", {})
        if not prs:
            continue
        seen = set((c.result.get("publish") or {}).get("seen_reviews", []))
        events: list[tuple[str, str, str, str | None]] = []  # (kind, by, text, seen key)
        merged: dict[str, str] = {}  # project -> who merged its PR, first seen now
        try:
            states = []
            for project, entry in prs.items():
                pr = api.get_pr(entry["repo"], entry["number"])
                states.append(pr)
                if pr.get("merged"):
                    by = (pr.get("merged_by") or {}).get("login", source)
                    events.append(("approve", by, f"merged {entry['url']}", None))
                    if not entry.get("merged"):
                        merged[project] = by
                for rv in api.reviews(entry["repo"], entry["number"]):
                    key = f"{entry['repo']}#{rv['id']}"
                    if key in seen:
                        continue
                    by = (rv.get("user") or {}).get("login", source)
                    if rv["state"] == "APPROVED":
                        events.append(("approve", by, rv.get("body") or "approved", key))
                    elif rv["state"] == "CHANGES_REQUESTED":
                        text = rv.get("body") or "changes requested (see the PR)"
                        events.append(("reject", by, f"{text} ({entry['url']})", key))
                    else:
                        seen.add(key)  # a plain comment: nothing to act on
        except ReviewHostError as e:
            changes.append(f"cluster {cid}: PRs not readable, skipped: {e}")
            continue
        closed = [p for p in states if p.get("state") == "closed" and not p.get("merged")]
        if states and len(closed) == len(states):
            # Once per set of PRs: after the rework, publishing opens new ones.
            key = "closed:" + ",".join(sorted(p["html_url"] for p in closed))
            if key not in seen:
                events.append(("reject", source, "PR closed without merge", key))
        for kind, by, text, key in events:
            c = reviewer._cluster(cid)
            if key:
                seen.add(key)
            try:
                if kind == "approve" and not is_approved(c):
                    reviewer.approve(cid, by, text, source=source)
                    changes.append(f"cluster {cid}: approved by {by}")
                elif kind == "reject" and c.state in MIGRATED | APPROVABLE:
                    reviewer.reject(cid, by, text, source=source)
                    changes.append(f"cluster {cid}: rejected by {by}: {text}")
            except ReviewError as e:  # e.g. approved on the PR before it was verified
                changes.append(f"cluster {cid}: {kind} by {by} not applied: {e}")
        c = reviewer._cluster(cid)
        publish = {**(c.result.get("publish") or {}), "seen_reviews": sorted(seen),
                   "synced_at": _now()}  # fmt: skip
        if merged:
            publish["projects"] = {
                p: {**e, "merged": True, "merged_by": merged[p]} if p in merged else e
                for p, e in publish.get("projects", {}).items()
            }
            changes += [f"cluster {cid}: {p} PR merged by {by}" for p, by in merged.items()]
        reviewer._update(c, publish=publish)
    return changes


sync_github = sync_reviews
