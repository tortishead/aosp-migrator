from typing import Any

import pytest

from aosp_migrator.db.models import DiffScan
from aosp_migrator.prescan.triage import ORDER, Triage, diff_triage, item_triage


def scan(presence: str | None = None, apply: str | None = None, project: str | None = "system/core", **detail: Any) -> DiffScan:
    return DiffScan(
        project=project,
        candidates=detail.pop("candidates", []),
        presence=presence,
        presence_detail=detail.pop("presence_detail", {}),
        apply=apply,
        apply_detail=detail.pop("apply_detail", {}),
    )


def label(s: DiffScan) -> str:
    return diff_triage(s, "fix.patch").label


def test_order_is_worst_first() -> None:
    assert ORDER == ["error", "unmapped", "unknown", "conflicts", "partial", "3way", "applies", "likely_present", "present"]


@pytest.mark.parametrize(
    ("presence", "apply", "expected"),
    [
        ("present", None, "present"),
        ("not_present", "applies", "applies"),
        ("not_present", "3way", "3way"),
        ("not_present", "conflicts", "conflicts"),
        # The rest of a partial diff decides when it is worse.
        ("partial", "applies", "partial"),
        ("partial", "3way", "partial"),
        ("partial", "conflicts", "conflicts"),
        # A patch that is already in conflicts; that says nothing new.
        ("likely_present", "conflicts", "likely_present"),
        # A patch that applies is not in the working tree, whatever the history could not tell.
        ("unknown", "applies", "applies"),
        ("unknown", "3way", "3way"),
        # It may conflict because it is already in.
        ("unknown", "conflicts", "unknown"),
        ("not_present", None, "unknown"),
    ],
)
def test_diff_label(presence: str, apply: str | None, expected: str) -> None:
    assert label(scan(presence, apply)) == expected


def test_unmapped_reason_names_candidates() -> None:
    assert diff_triage(scan(project=None, candidates=["a", "b", "c", "d"]), "fix.patch") == Triage("unmapped", "fix.patch: one of a, b, c +1 more")
    assert diff_triage(scan(project=None), "fix.patch") == Triage("unmapped", "fix.patch: no matching project")


def test_reasons() -> None:
    history = {"history": [{"subject": "Fix", "match": "change_id", "sha": "0123456789abcdef"}, {"subject": "Other", "match": None, "sha": None}]}
    assert diff_triage(scan("present", presence_detail=history), "p").reason == "system/core: in commit 0123456789ab (change_id)"
    assert diff_triage(scan("present"), "p").reason == "system/core: every file reverse-applies"
    conflicts = scan("not_present", "conflicts", apply_detail={"files": {"a.c": "conflicts", "b.c": "applies"}})
    assert diff_triage(conflicts, "p").reason == "system/core: a.c conflict"
    partial = scan("partial", "3way", presence_detail={"files": {"a.c": "present", "b.c": "not_present"}}, apply_detail={"files": {"b.c": "3way"}})
    assert diff_triage(partial, "p").reason == "system/core: a.c already in"
    unknown = {"reason": "history unavailable"}
    assert diff_triage(scan("unknown", "conflicts", presence_detail=unknown), "p").reason == "system/core: history unavailable"
    applies = diff_triage(scan("unknown", "applies", presence_detail=unknown), "p")
    assert applies.reason == "system/core: applies as is; presence unknown: history unavailable"
    likely = scan("likely_present", "conflicts", presence_detail={"files": {"a.c": "likely_present"}})
    assert diff_triage(likely, "p").reason == "system/core: lines of a.c found in the target"


def test_item_takes_the_worst_diff() -> None:
    assert item_triage([]) is None
    mixed = [Triage("present", "x: in"), Triage("3way", "y: merge"), Triage("applies", "z: ok"), Triage("3way", "w: merge")]
    assert item_triage(mixed) == Triage("3way", "y: merge; w: merge")
    assert item_triage([Triage("likely_present", "a"), Triage("present", "b")]) == Triage("likely_present", "a")
    assert item_triage([Triage("conflicts", "a"), Triage("unmapped", "b")]) == Triage("unmapped", "b")
