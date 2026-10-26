"""Already-present check: is a diff already in the mapped target project?

First `git apply --reverse --check` per file, in the project's working tree; all files
reversible means present, some means partial. When that is not conclusive, the project
history is searched for each format-patch commit by Change-Id, then subject, then
`git patch-id`. The subject only counts for upstream authors (Google/AOSP): OEM subjects
like "Fix crash" match too easily. When neither proves presence, a content heuristic
catches context drift: a file is likely present when its added lines are in the target
file and its removed lines are gone (lines without letters or digits ignored). Evidence
only, never proof. A shallow or partial clone, or a plain tree, whose history cannot
answer gives `unknown` ("history unavailable") unless the heuristic says likely.
Read-only: see `prescan.git`.
"""

import re
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from aosp_migrator.config import DEFAULT_UPSTREAM_DOMAINS
from aosp_migrator.importer.diffparse import Commit, FileChange
from aosp_migrator.prescan import git
from aosp_migrator.prescan.patch import FileSections, Patch

PresenceStatus = Literal["present", "likely_present", "not_present", "partial", "unknown"]
FileResult = Literal["present", "likely_present", "not_present", "unknown"]
HistoryMatch = Literal["change_id", "subject", "patch_id"]

HISTORY_UNAVAILABLE = "history unavailable"
NOTHING_CHECKABLE = "no file could be checked"

_CHANGE_ID = re.compile(r"^Change-Id:\s*(I[0-9a-f]{40})\s*$", re.MULTILINE)
_PATCH_TAG = re.compile(r"^\[PATCH[^\]]*\]\s*")
_EMAIL = re.compile(r"<([^>]+)>")
_SIGNIFICANT = re.compile(r"\w")
EVIDENCE_LINES = 5


@dataclass
class Presence:
    status: PresenceStatus
    # files: {path: present / likely_present / not_present / unknown}; history:
    # [{subject, match, sha}] per format-patch commit (match/sha None when not found);
    # heuristic: {path: evidence} per file the content heuristic checked; reason when
    # history was unavailable or nothing could be checked.
    detail: dict[str, Any] = field(default_factory=dict)

    def files(self, result: FileResult) -> list[str]:
        return [path for path, value in self.detail.get("files", {}).items() if value == result]


def check_presence(project_dir: Path, diff_text: str, upstream_domains: Iterable[str] = DEFAULT_UPSTREAM_DOMAINS) -> Presence:
    patch = Patch.parse(diff_text)
    files = _reverse_check_files(project_dir, patch.files)
    detail: dict[str, Any] = {"files": files}
    known = [r for r in files.values() if r != "unknown"]
    if known and all(r == "present" for r in known):
        return Presence("present", detail)
    some_files = "present" in known

    found = 0
    if patch.headers:
        try:
            history = [
                _find_in_history(project_dir, header, changes, _is_upstream(header.author, upstream_domains))
                for header, changes in patch.headers
            ]
            shallow = git.is_shallow(project_dir)
        except git.GitError:
            detail["reason"] = HISTORY_UNAVAILABLE
        else:
            detail["history"] = history
            found = sum(h["sha"] is not None for h in history)
            if found == len(history):
                return Presence("present", detail)
            if shallow:
                detail["reason"] = HISTORY_UNAVAILABLE

    if heuristic := _heuristic(project_dir, patch.files, files):
        detail["heuristic"] = heuristic
        if all(r in ("present", "likely_present") for r in files.values() if r != "unknown"):
            return Presence("likely_present", detail)
    if found or some_files:
        return Presence("partial", detail)
    if not known:
        return Presence("unknown", {"reason": NOTHING_CHECKABLE, **detail})
    return Presence("unknown" if "reason" in detail else "not_present", detail)


def _reverse_check_files(project_dir: Path, sections: list[FileSections]) -> dict[str, FileResult]:
    """Reverse-apply each file on its own, with its sections from every commit of a series.

    Sections go in series order: `git apply --reverse` undoes them newest first itself.
    """
    results: dict[str, FileResult] = {}
    for section in sections:
        if section.binary:
            results[section.path] = "unknown"
            continue
        reverses = git.apply(project_dir, section.patch, "--reverse", "--check").returncode == 0
        results[section.path] = "present" if reverses else "not_present"
    return results


def _heuristic(project_dir: Path, sections: list[FileSections], files: dict[str, FileResult]) -> dict[str, Any]:
    """Content check of each `not_present` file; marks it `likely_present` in `files`, returns evidence.

    Lines compare with whitespace runs collapsed. Only lines with a letter or digit count, so brace-only hunks
    prove nothing; a file without such lines is left as it is.
    """
    evidence: dict[str, Any] = {}
    for section in sections:
        path = section.path
        if files.get(path) != "not_present" or section.binary:
            continue
        added, removed = _net_lines(section.changes)
        if not added and not removed:
            continue
        target = project_dir / path
        lines = Counter(_normalize(line) for line in target.read_text(errors="replace").splitlines()) if target.is_file() else Counter()
        missing = [line for line in added if not lines[line]]
        remaining = [line for line in removed if lines[line]]
        evidence[path] = {
            "added": {"found": len(added) - len(missing), "total": len(added), "missing": missing[:EVIDENCE_LINES]},
            "removed": {"gone": len(removed) - len(remaining), "total": len(removed), "remaining": remaining[:EVIDENCE_LINES]},
        }
        if not missing and not remaining:
            files[path] = "likely_present"
    return evidence


def _normalize(line: str) -> str:
    return " ".join(line.split())


def _net_lines(changes: list[FileChange]) -> tuple[list[str], list[str]]:
    """Significant added and removed lines (normalized, deduped) over a file's series.

    A line added by one commit and removed by a later one cancels out, and so does a
    line removed and added again: a re-indented line is neither added nor removed.
    """
    added: dict[str, None] = {}
    removed: dict[str, None] = {}
    for change in changes:
        for hunk in change.hunks:
            for line in hunk.splitlines()[1:]:
                sign, text = line[:1], _normalize(line[1:])
                if sign not in "+-" or not _SIGNIFICANT.search(text):
                    continue
                mine, other = (added, removed) if sign == "+" else (removed, added)
                if text in other:
                    del other[text]
                else:
                    mine[text] = None
    return list(added), list(removed)


def _is_upstream(author: str | None, domains: Iterable[str]) -> bool:
    match = _EMAIL.search(author or "")
    if not match:
        return False
    domain = match.group(1).rpartition("@")[2].lower()
    return any(domain == d or domain.endswith("." + d) for d in (d.lower().strip() for d in domains) if d)


def _find_in_history(project_dir: Path, commit: Commit, files: list[FileChange], upstream: bool) -> dict[str, Any]:
    subject = _PATCH_TAG.sub("", commit.subject)
    entry: dict[str, Any] = {"subject": subject, "match": None, "sha": None}
    lookups: list[tuple[HistoryMatch, Callable[[], str | None]]] = []
    if change_id := _CHANGE_ID.search(commit.message):
        lookups.append(("change_id", lambda: _by_grep(project_dir, f"Change-Id: {change_id.group(1)}")))
    if subject and upstream:
        lookups.append(("subject", lambda: _by_subject(project_dir, subject)))
    lookups.append(("patch_id", lambda: _by_patch_id(project_dir, files)))
    for match, lookup in lookups:
        if sha := lookup():
            entry.update(match=match, sha=sha)
            break
    return entry


def _by_grep(project_dir: Path, text: str) -> str | None:
    out = git.git(project_dir, "log", "HEAD", "-F", f"--grep={text}", "--format=%H", "-n", "1").stdout
    return out.strip() or None


def _by_subject(project_dir: Path, subject: str) -> str | None:
    out = git.git(project_dir, "log", "HEAD", "-F", f"--grep={subject}", "--format=%H%x00%s").stdout
    for line in out.splitlines():
        sha, _, found = line.partition("\0")
        if found == subject:
            return sha
    return None


def _by_patch_id(project_dir: Path, files: list[FileChange]) -> str | None:
    patch = "".join(c.raw for c in files)
    wanted = _patch_ids(project_dir, patch)
    if not wanted:
        return None
    paths = sorted({p for c in files for p in (c.path, c.old_path) if p})
    # Only commits touching the same files, so this stays cheap on big histories.
    log = git.git(project_dir, "log", "HEAD", "-p", "--no-color", "--no-ext-diff", "--format=commit %H", "--", *paths).stdout
    for patch_id, sha in _patch_id_pairs(project_dir, log):
        if patch_id in wanted:
            return sha
    return None


def _patch_ids(project_dir: Path, patch: str) -> set[str]:
    return {patch_id for patch_id, _ in _patch_id_pairs(project_dir, patch)}


def _patch_id_pairs(project_dir: Path, text: str) -> list[tuple[str, str]]:
    if not text.strip():
        return []
    out = git.git(project_dir, "patch-id", "--stable", input=text).stdout
    pairs = [line.split() for line in out.splitlines()]
    return [(pair[0], pair[1]) for pair in pairs if len(pair) == 2]
