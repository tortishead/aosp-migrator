"""States of a patch or cluster and the transitions allowed between them.

The table encodes the design's flow. Agents and the orchestrator must follow it; a human
may override any transition, which is recorded with a reason (see LedgerStore.transition).
"""

from __future__ import annotations

from enum import StrEnum


class State(StrEnum):
    INGESTED = "ingested"
    UPSTREAM = "upstream"  # intent already met in target: dropped
    OBSOLETE = "obsolete"  # code path removed or replaced in target: dropped
    NEEDED = "needed"
    APPLIED = "applied"  # clean cherry-pick
    RESOLVED = "resolved"  # applied after conflict resolution
    REWRITTEN = "rewritten"  # re-implemented for the same intent
    VERIFIED = "verified"  # built and tests passed
    FAILED = "failed"
    NEEDS_DECISION = "needs_decision"


DROPPED = frozenset({State.UPSTREAM, State.OBSOLETE})
MIGRATED = frozenset({State.APPLIED, State.RESOLVED, State.REWRITTEN})

TRANSITIONS: dict[State, frozenset[State]] = {
    State.INGESTED: frozenset({*DROPPED, State.NEEDED, State.NEEDS_DECISION}),
    # An agent may still discover a patch is upstream or obsolete while migrating it.
    State.NEEDED: frozenset({*MIGRATED, *DROPPED, State.FAILED, State.NEEDS_DECISION}),
    # Back to NEEDED = retry with another strategy.
    State.APPLIED: frozenset({State.VERIFIED, State.FAILED, State.NEEDED, State.NEEDS_DECISION}),
    State.RESOLVED: frozenset({State.VERIFIED, State.FAILED, State.NEEDED, State.NEEDS_DECISION}),
    State.REWRITTEN: frozenset({State.VERIFIED, State.FAILED, State.NEEDED, State.NEEDS_DECISION}),
    # Reopened when a reviewer rejects the result.
    State.VERIFIED: frozenset({State.NEEDED}),
    State.FAILED: frozenset({State.NEEDED, State.NEEDS_DECISION}),
    State.NEEDS_DECISION: frozenset({State.NEEDED, *DROPPED}),
    # A human may revive a dropped patch.
    State.UPSTREAM: frozenset({State.NEEDED}),
    State.OBSOLETE: frozenset({State.NEEDED}),
}


class InvalidTransition(Exception):
    def __init__(self, current: State, target: State):
        self.current = current
        self.target = target
        allowed = ", ".join(sorted(TRANSITIONS[current])) or "none"
        super().__init__(f"cannot go from '{current}' to '{target}' (allowed: {allowed})")


def can_transition(current: State, target: State) -> bool:
    return target in TRANSITIONS[current]
