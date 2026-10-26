"""Triage label: one machine verdict per item from its diff scans, worst diff first.

A diff's label comes from its mapping, presence and apply results. `likely_present`
and `unknown` stand on their own (their apply result says little: a patch already in
conflicts, and unknown ranks above conflicts anyway); `partial` takes the worse of
itself and the apply result of the files still missing. The reason is a short text
for the backlog tooltip.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal

from aosp_migrator.db.models import DiffScan

Label = Literal["error", "unmapped", "unknown", "conflicts", "partial", "3way", "applies", "likely_present", "present"]

# Worst first.
ORDER: list[Label] = ["error", "unmapped", "unknown", "conflicts", "partial", "3way", "applies", "likely_present", "present"]

# Paths or commits a reason names before it says "+n more".
SHOWN = 3


@dataclass(frozen=True)
class Triage:
    label: Label
    reason: str


def _names(values: list[str]) -> str:
    shown = ", ".join(values[:SHOWN])
    return f"{shown} +{len(values) - SHOWN} more" if len(values) > SHOWN else shown


def _paths(detail: dict | None, key: str, result: str) -> list[str]:
    return [path for path, value in (detail or {}).get(key, {}).items() if value == result]


def diff_triage(scan: DiffScan, filename: str) -> Triage:
    if scan.project is None:
        if scan.candidates:
            return Triage("unmapped", f"{filename}: one of {_names(scan.candidates)}")
        return Triage("unmapped", f"{filename}: no matching project")
    presence = scan.presence_detail or {}
    if scan.presence == "present":
        matched = [f"{h['sha'][:12]} ({h['match']})" for h in presence.get("history", []) if h.get("sha")]
        return Triage("present", f"{scan.project}: in commit {_names(matched)}" if matched else f"{scan.project}: every file reverse-applies")
    if scan.presence == "likely_present":
        return Triage("likely_present", f"{scan.project}: lines of {_names(_paths(presence, 'files', 'likely_present'))} found in the target")
    if scan.presence == "unknown":
        reason = presence.get("reason") or f"cannot check {_names(_paths(presence, 'files', 'unknown'))}"
        return Triage("unknown", f"{scan.project}: {reason}")
    apply = _apply_triage(scan)
    if scan.presence == "partial":
        partial = Triage("partial", f"{scan.project}: {_names(_paths(presence, 'files', 'present'))} already in")
        return min(partial, apply, key=lambda t: ORDER.index(t.label))
    return apply


def _apply_triage(scan: DiffScan) -> Triage:
    if scan.apply == "conflicts":
        return Triage("conflicts", f"{scan.project}: {_names(_paths(scan.apply_detail, 'files', 'conflicts'))} conflict")
    if scan.apply == "3way":
        return Triage("3way", f"{scan.project}: {_names(_paths(scan.apply_detail, 'files', '3way'))} need a 3-way merge")
    if scan.apply == "applies":
        return Triage("applies", f"{scan.project}: applies as is")
    return Triage("unknown", f"{scan.project}: not apply-checked")


def item_triage(diffs: Iterable[Triage]) -> Triage | None:
    """The worst diff's label; reasons of every diff with that label. None without diffs."""
    diffs = list(diffs)
    if not diffs:
        return None
    worst = min(diffs, key=lambda t: ORDER.index(t.label))
    reasons = list(dict.fromkeys(t.reason for t in diffs if t.label == worst.label))
    return Triage(worst.label, "; ".join(reasons))
