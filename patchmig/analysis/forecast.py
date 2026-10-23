"""Difficulty forecast per cluster, from its net diff and the upstream change map.

Levels, from easy to hard: clean (upstream left the changed lines alone), conflict
(upstream changed the same lines), rewrite (a file moved to another project or was deleted,
or a method the patch uses changed its signature), obsolete (every file is gone). The
forecast steers scheduling and budgets; the cluster agent finds out what really happens.
"""

from __future__ import annotations

import re

from patchmig.analysis.diffs import FileDiff, overlaps
from patchmig.knowledge.upstream import FileFate

LEVELS = ("clean", "conflict", "rewrite", "obsolete")


def forecast(files: list[tuple[str, FileDiff]], fates: dict[tuple[str, str], FileFate]) -> dict:
    """files: (project, file diff) pairs of the cluster's net diff (base coordinates)."""
    reasons: list[str] = []
    level = "clean"

    def raise_to(new: str, reason: str) -> None:
        nonlocal level
        reasons.append(reason)
        if LEVELS.index(new) > LEVELS.index(level):
            level = new

    known = [(p, fd, fates[(p, fd.path)]) for p, fd in files if (p, fd.path) in fates]
    if known and all(f.status == "deleted" and not f.candidates for _, _, f in known):
        return {"level": "obsolete", "reasons": [f"{f.path} deleted upstream" for *_, f in known]}

    for _, fd, fate in known:
        if fate.status == "unknown":
            raise_to("rewrite", f"{fate.project}:{fate.path} could not be located in the target")
        elif fate.status == "deleted":
            hint = f" (candidates: {', '.join(fate.candidates[:3])})" if fate.candidates else ""
            raise_to("rewrite", f"{fate.path} deleted upstream{hint}")
        elif fate.status == "moved_project":
            raise_to("rewrite", f"{fate.path} moved to {fate.new_project}:{fate.new_path}")
        text = "\n".join(fd.added + fd.removed)
        for name in sorted({*fate.changed_signatures, *fate.removed_symbols}):
            if re.search(rf"\b{re.escape(name)}\s*\(", text):
                change = "was removed" if name in fate.removed_symbols else "changed signature"
                raise_to("rewrite", f"{name}() {change} upstream and the patch uses it")
        if fate.status in ("modified", "renamed") and overlaps(
            fd.changed, fate.upstream_changed, pad=1
        ):
            raise_to("conflict", f"upstream changed the same lines of {fate.path}")
        elif fate.status == "renamed":
            reasons.append(f"{fate.path} renamed to {fate.new_path}; rename-aware apply")
    return {"level": level, "reasons": reasons}
