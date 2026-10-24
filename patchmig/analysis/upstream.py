"""The deterministic rungs of the upstream check, plus the obsolete rule.

Rungs, cheapest and most conclusive first (design, stage 2):

1. identity: the same patch-id or Change-Id is in the target's upstream history.
2. provenance: the patch says it was cherry-picked from a commit that the target contains.
3. textual: every line the patch adds is present at the mapped location in the target,
   and every line it removes is gone. Strong evidence; a partial match is recorded but
   decides nothing (the semantic rung in M6 takes over).

Obsolete: every file the patch touches was deleted upstream with no successor.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field

from patchmig.analysis.diffs import FileDiff, normalize_line
from patchmig.analysis.patches import PatchView
from patchmig.gitutil import GitError, git, is_ancestor, resolve
from patchmig.knowledge.trees import Trees
from patchmig.knowledge.upstream import FileFate

_HAS_WORD = re.compile(r"\w")


@dataclass
class Check:
    rung: str
    conclusive: bool
    state: str | None = None  # the State value it concludes, if conclusive
    confidence: float | None = None
    evidence: dict = field(default_factory=dict)

    def to_json(self) -> dict:
        return {"rung": self.rung, "conclusive": self.conclusive, **self.evidence}


class IdentityIndex:
    """Patch-ids and Change-Ids of the target's upstream commits, per project, built once."""

    def __init__(self, trees: Trees, paths: dict[str, set[str]]):
        self.trees = trees
        self.paths = paths  # project -> paths whose history matters (source and target names)
        self._patch_ids: dict[str, dict[str, str]] = {}
        self._change_ids: dict[str, dict[str, str]] = {}

    def _range(self, project: str) -> list[str]:
        rng = self.trees.upstream_range(project)
        if rng:
            return [rng]
        base = self.trees.target_base(project)
        limit = self.trees.config.trees.history_commits
        return [f"--max-count={limit}", base] if base else []

    def patch_ids(self, project: str) -> dict[str, str]:
        if project not in self._patch_ids:
            ids: dict[str, str] = {}
            rng, paths = self._range(project), sorted(self.paths.get(project, ()))
            if rng and paths:
                # Path-limited: the patch-id covers only these files, so an upstream commit
                # that made the same change plus changes elsewhere still matches.
                log = self.trees.target_log(project, "-p", "--format=commit %H", *rng, "--", *paths)
                if log.strip():
                    out = git(None, "patch-id", "--stable", input=log)
                    for line in out.splitlines():
                        pid, sha = line.split()
                        ids.setdefault(pid, sha)
            self._patch_ids[project] = ids
        return self._patch_ids[project]

    def change_ids(self, project: str) -> dict[str, str]:
        if project not in self._change_ids:
            ids: dict[str, str] = {}
            rng = self._range(project)
            if rng:
                fmt = "--format=%H%x1f%(trailers:key=Change-Id,valueonly,separator=%x1f)%x1e"
                for record in self.trees.target_log(project, fmt, *rng).split("\x1e"):
                    sha, *cids = record.strip().split("\x1f")
                    for cid in cids:
                        if cid.strip():
                            ids.setdefault(cid.strip(), sha)
            self._change_ids[project] = ids
        return self._change_ids[project]

    def subject(self, project: str, sha: str) -> str:
        return self.trees.target_log(project, "-1", "--format=%s", sha).strip()


def identity(view: PatchView, index: IdentityIndex) -> Check:
    p = view.patch
    if p.fingerprint and (sha := index.patch_ids(p.project).get(p.fingerprint)):
        match = "patch-id"
    elif p.change_id and (sha := index.change_ids(p.project).get(p.change_id)):
        match = "change-id"
    else:
        return Check("identity", False)
    evidence = {"match": match, "target_commit": sha, "subject": index.subject(p.project, sha)}
    return Check("identity", True, "upstream", 1.0, evidence)


def provenance(view: PatchView, trees: Trees) -> Check:
    origin = view.patch.links.get("cherry_picked_from")
    repo = trees.target_repo(view.project)
    base = trees.target_base(view.project)
    if not origin or repo is None or base is None:
        return Check("provenance", False)
    sha = resolve(repo, origin)
    if sha is None:
        return Check("provenance", False, evidence={"origin": origin, "found": False})
    if not is_ancestor(repo, sha, base):
        return Check("provenance", False, evidence={"origin": sha, "in_target": False})
    try:
        subject = git(repo, "log", "-1", "--format=%s", sha).strip()
    except GitError:
        subject = ""
    evidence = {"origin": sha, "subject": subject}
    return Check("provenance", True, "upstream", 0.95, evidence)


def _meaningful(lines: list[str], min_chars: int) -> list[str]:
    out = []
    for line in lines:
        norm = normalize_line(line)
        if len(norm) >= min_chars and _HAS_WORD.search(norm):
            out.append(norm)
    return out


def _counts(text: str | None) -> Counter[str]:
    return Counter(normalize_line(line) for line in (text or "").splitlines())


def textual(
    project: str,
    files: list[FileDiff],
    fates: dict[tuple[str, str], FileFate],
    trees: Trees,
    min_chars: int,
) -> Check:
    """Are the patch's added lines in the target, and its removed lines gone?

    Counts are compared with the source base version of each file, so a line that already
    existed there doesn't count as "present" just because it is still in the target.
    """
    matched = total = added_total = 0
    missing: list[str] = []
    located: list[str] = []
    binary: list[str] = []
    for fd in files:
        if fd.binary:  # no lines to compare: such a patch is never proven upstream here
            binary.append(fd.path)
            continue
        fate = fates.get((project, fd.path))
        location = fate.target_location if fate else (project, fd.new_path or fd.path)
        if location is None:
            continue  # deleted upstream: no evidence either way (the obsolete rule decides)
        base = _counts(trees.source_base_file(project, fd.old_path) if fd.old_path else None)
        target = _counts(trees.target_file(*location))
        added = _meaningful(fd.added, min_chars)
        removed = _meaningful(fd.removed, min_chars)
        moved = set(added) & set(removed)  # re-indented or moved lines: no evidence
        for line in added:
            if line in moved:
                continue
            total += 1
            added_total += 1
            if target[line] > base[line]:
                matched += 1
            elif len(missing) < 5:
                missing.append(line)
        for line in removed:
            if line in moved:
                continue
            total += 1
            if target[line] < base[line] or target[line] == 0:
                matched += 1
            elif len(missing) < 5:
                missing.append(f"(still present) {line}")
        located.append(f"{location[0]}:{location[1]}")
    evidence = {"matched": matched, "total": total, "target_files": located}
    if missing:
        evidence["missing"] = missing
    if binary:
        evidence["binary"] = binary
    if added_total and matched == total and not binary:
        return Check("textual", True, "upstream", 0.9, evidence)
    return Check("textual", False, evidence=evidence)


def obsolete(view: PatchView, fates: dict[tuple[str, str], FileFate]) -> Check:
    own = [fates[k] for k in view.file_keys() if k in fates]
    deleted = [f for f in own if f.status == "deleted" and not f.candidates]
    if not own or len(deleted) < len(own):
        if deleted:
            return Check("obsolete", False, evidence={"deleted": [f.path for f in deleted]})
        return Check("obsolete", False)
    evidence = {
        "deleted": [f.path for f in deleted],
        "commits": [c for f in deleted for c in f.commits],
    }
    return Check("obsolete", True, "obsolete", 0.8, evidence)
