"""Revert folding: patches that a later revert in the same patch set cancels out.

A patch is effective unless an effective patch reverts it. Reverting a revert therefore
brings the original back: for X <- Y <- Z (Y reverts X, Z reverts Y), Y and Z cancel and X
stays. A revert whose target is not in the patch set stays: it undoes something in the base.
"""

from __future__ import annotations

from functools import cache

from patchmig.analysis.patches import PatchView


def cancelled(views: list[PatchView]) -> dict[str, str]:
    """Key of each cancelled patch -> key of the patch it cancels with."""
    by_key = {v.key: v for v in views}

    @cache
    def effective(key: str) -> bool:
        reverters = by_key[key].patch.links.get("reverted_by", [])
        return not any(effective(r) for r in reverters if r in by_key)

    result: dict[str, str] = {}
    for v in views:
        if not effective(v.key):
            partner = next(r for r in v.patch.links.get("reverted_by", []) if r in by_key)
            result[v.key] = partner
        target = v.patch.links.get("reverts")
        if target in by_key and not effective(target):
            result[v.key] = target
    return result
