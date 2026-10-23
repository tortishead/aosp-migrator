"""Upstream change map: what happened to each file the OEM patches touch.

For every (project, path) in the OEM footprint, record its fate between the source base and
the target base: unchanged, modified, renamed (within the project), moved to another project,
or deleted. For changed files, also record the old-side line ranges upstream changed, the
upstream commits that touched it, and method signatures that changed or disappeared.

Only footprint files are examined, so the cost grows with the patch set, not the tree.
"""

from __future__ import annotations

import difflib
import posixpath
import re
from dataclasses import asdict, dataclass, field

from patchmig.gitutil import GitError, name_status
from patchmig.knowledge.trees import Trees

# A declaration line: modifiers, a return type and a name followed by "(".
_SIGNATURE = re.compile(
    r"^\s*(?:(?:public|protected|private|static|final|synchronized|abstract|override|virtual|"
    r"inline|suspend|fun|oneway|native|default)\s+)*[\w<>\[\],.?:*&]+(?:\s+[\w<>\[\],.?:*&]+)*?"
    r"\s+([a-zA-Z_]\w*)\s*\(([^;{}]*)\)\s*(?:const\s*)?(?:throws [\w.,\s]+)?\s*[{:]?\s*$"
)
_KEYWORDS = {"if", "for", "while", "switch", "catch", "return", "new", "else", "throw"}
MAX_COMMITS = 20


@dataclass
class FileFate:
    project: str
    path: str  # project-relative path in the source tree
    status: str  # unchanged, modified, renamed, moved_project, deleted, added_upstream, unknown
    new_project: str | None = None
    new_path: str | None = None
    candidates: list[str] = field(default_factory=list)  # possible successors, if ambiguous
    upstream_changed: list[tuple[int, int]] = field(default_factory=list)  # old-side ranges
    commits: list[str] = field(default_factory=list)  # "sha subject", newest first
    changed_signatures: dict[str, dict[str, list[str]]] = field(default_factory=dict)
    removed_symbols: list[str] = field(default_factory=list)
    exact: bool = True  # False: decided by comparing contents, without shared history

    @property
    def key(self) -> str:
        return f"{self.project}:{self.path}"

    @property
    def target_location(self) -> tuple[str, str] | None:
        if self.status in ("deleted", "unknown"):
            return None
        return (self.new_project or self.project, self.new_path or self.path)

    def to_json(self) -> dict:
        return asdict(self)

    @classmethod
    def from_json(cls, data: dict) -> FileFate:
        data = {**data, "upstream_changed": [tuple(r) for r in data["upstream_changed"]]}
        return cls(**data)


def signatures(text: str) -> dict[str, set[str]]:
    """Method name -> normalised declaration lines, for Java/Kotlin/C++/AIDL-like code."""
    found: dict[str, set[str]] = {}
    for line in text.splitlines():
        m = _SIGNATURE.match(line)
        if m and m.group(1) not in _KEYWORDS:
            decl = " ".join(line.strip().rstrip("{:").split())
            found.setdefault(m.group(1), set()).add(decl)
    return found


def changed_ranges(before: str, after: str) -> list[tuple[int, int]]:
    """Old-side line ranges (1-based, inclusive) that differ between two versions."""
    ranges: list[tuple[int, int]] = []
    matcher = difflib.SequenceMatcher(None, before.splitlines(), after.splitlines(), False)
    for tag, i1, i2, _, _ in matcher.get_opcodes():
        if tag == "equal":
            continue
        start, end = (i1 + 1, i2) if i2 > i1 else (max(i1, 1), i1 + 1)  # insertion
        ranges.append((start, end))
    return ranges


class ChangeMapBuilder:
    def __init__(self, trees: Trees):
        self.trees = trees
        self._status: dict[str, dict[str, tuple[str, str]]] = {}  # project -> old -> (st, new)
        self._warned: set[str] = set()

    def _upstream_status(self, project: str) -> dict[str, tuple[str, str]] | None:
        if project in self._status:
            return self._status[project]
        rng = self.trees.upstream_range(project)
        if rng is None:
            return None
        old, new = rng.split("..")
        try:
            changes = name_status(self.trees.target_repo(project), old, new)
        except GitError as e:
            self.trees.warnings.append(f"{project}: {e}")
            return None
        status = {o: (st, n) for st, o, n in changes if st in "MDR"}
        self._status[project] = status
        return status

    def _find_elsewhere(self, project: str, path: str) -> list[tuple[str, str]]:
        """Target files with the same name, in this project first, then in all others."""
        name = posixpath.basename(path)
        hits = [(project, p) for p in self.trees.target_files(project) if p.endswith("/" + name)]
        if hits:
            return hits
        for other in self.trees.target_projects():
            if other != project:
                files = self.trees.target_files(other)
                hits += [(other, p) for p in files if p == name or p.endswith("/" + name)]
        return hits

    def _warn_once(self, project: str, message: str) -> None:
        if project not in self._warned:
            self._warned.add(project)
            self.trees.warnings.append(message)

    def _unknown(self, project: str, path: str) -> FileFate | None:
        """A fate we can't determine because a project or base ref is missing, else None.

        Never guess "deleted" here: that would make patches look obsolete.
        """
        trees = self.trees
        if trees.source_base(project) is None:
            self._warn_once(project, f"{project}: {trees.source.base_ref} not found in source")
            return FileFate(project=project, path=path, status="unknown", exact=False)
        if trees.target_repo(project) is None:
            hits = self._find_elsewhere(project, path)
            if len(hits) == 1:
                new_project, new_path = hits[0]
                return FileFate(project, path, "moved_project", new_project, new_path, exact=False)
            self._warn_once(project, f"{project}: no such project in the target tree")
            return FileFate(project=project, path=path, status="unknown", exact=False)
        if trees.target_base(project) is None:
            self._warn_once(project, f"{project}: {trees.target.base_ref} not found in target")
            return FileFate(project=project, path=path, status="unknown", exact=False)
        return None

    def fate(self, project: str, path: str) -> FileFate:
        if unknown := self._unknown(project, path):
            return unknown
        status = self._upstream_status(project)
        fate = FileFate(project=project, path=path, status="unchanged", exact=status is not None)
        before = self.trees.source_base_file(project, path)
        if before is None:
            # Added by the OEM patches; it only matters if upstream added the same path.
            if self.trees.target_file(project, path) is not None:
                fate.status = "added_upstream"
            return fate

        if status is not None:
            st, new = status.get(path, ("U", path))
            if st == "M":
                fate.status = "modified"
            elif st == "R":
                fate.status, fate.new_path = "renamed", new
            elif st == "D":
                fate.status = "deleted"
        else:
            after = self.trees.target_file(project, path)
            if after is None:
                fate.status = "deleted"
            elif after != before:
                fate.status = "modified"

        if fate.status == "deleted":
            # Maybe it moved where rename detection can't see it (another project/module).
            hits = [h for h in self._find_elsewhere(project, path) if h != (project, path)]
            if len(hits) == 1:
                other_project, other_path = hits[0]
                fate.new_project = other_project if other_project != project else None
                fate.new_path = other_path
                fate.status = "moved_project" if fate.new_project else "renamed"
                fate.exact = False
            else:
                fate.candidates = [f"{p}:{f}" for p, f in hits]

        location = fate.target_location
        after = self.trees.target_file(*location) if location else None
        if after is not None and after != before:
            fate.upstream_changed = changed_ranges(before, after)
            old_sigs, new_sigs = signatures(before), signatures(after)
            for name, decls in old_sigs.items():
                if name not in new_sigs:
                    fate.removed_symbols.append(name)
                elif decls != new_sigs[name]:
                    fate.changed_signatures[name] = {
                        "before": sorted(decls), "after": sorted(new_sigs[name])
                    }  # fmt: skip
        fate.commits = self._commits(project, path, fate)
        return fate

    def _commits(self, project: str, path: str, fate: FileFate) -> list[str]:
        if fate.status == "unchanged" or not fate.exact:
            return []
        rng = self.trees.upstream_range(project)
        paths = [path] + ([fate.new_path] if fate.new_path else [])
        out = self.trees.target_log(
            project, f"--max-count={MAX_COMMITS}", "--format=%h %s", rng, "--", *paths
        )
        return out.splitlines()


def build_change_map(trees: Trees, files: set[tuple[str, str]]) -> dict[tuple[str, str], FileFate]:
    builder = ChangeMapBuilder(trees)
    return {(project, path): builder.fate(project, path) for project, path in sorted(files)}
