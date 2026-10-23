"""OEM footprint map: which files and symbols the patch set touches, per project."""

from __future__ import annotations

from collections import defaultdict

from patchmig.analysis.patches import PatchView


def footprint_map(views: list[PatchView], hot_spot_min: int) -> dict[str, dict]:
    """Project -> {patches, files: {path: [patch ids]}, hot_spots, symbols}."""
    files: dict[str, dict[str, list[int]]] = defaultdict(lambda: defaultdict(list))
    symbols: dict[str, set[str]] = defaultdict(set)
    patches: dict[str, set[int]] = defaultdict(set)
    for v in views:
        patches[v.project].add(v.id)
        symbols[v.project].update(v.patch.footprint.get("defined", []))
        for f in v.files:
            files[v.project][f.path].append(v.id)
    result = {}
    for project in sorted(files):
        by_file = {path: ids for path, ids in sorted(files[project].items())}
        hot = sorted(
            (p for p, ids in by_file.items() if len(ids) >= hot_spot_min),
            key=lambda p: -len(by_file[p]),
        )
        result[project] = {
            "patches": len(patches[project]),
            "files": by_file,
            "hot_spots": hot,
            "symbols_defined": sorted(symbols[project]),
        }
    return result
