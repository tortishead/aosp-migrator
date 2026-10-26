"""Already-present check: is a diff already in the mapped target project?

First `git apply --reverse --check` per file, in the project's working tree; all files
reversible means present, some means partial. When that is not conclusive, the project
history is searched for each format-patch commit by Change-Id, then subject, then
`git patch-id`. The subject only counts for upstream authors (Google/AOSP): OEM subjects
like "Fix crash" match too easily. A shallow or partial clone, or a plain tree, that
cannot answer gives `unknown` ("history unavailable").
Read-only: see `prescan.git`.
"""

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from aosp_migrator.config import DEFAULT_UPSTREAM_DOMAINS
from aosp_migrator.importer.diffparse import Commit, FileChange, parse_diff
from aosp_migrator.prescan.git import GitError, git, is_shallow

PresenceStatus = Literal["present", "not_present", "partial", "unknown"]
FileResult = Literal["present", "not_present", "unknown"]
HistoryMatch = Literal["change_id", "subject", "patch_id"]

HISTORY_UNAVAILABLE = "history unavailable"

_FROM_SHA = re.compile(r"^From [0-9a-f]{40} ", re.MULTILINE)
_CHANGE_ID = re.compile(r"^Change-Id:\s*(I[0-9a-f]{40})\s*$", re.MULTILINE)
_PATCH_TAG = re.compile(r"^\[PATCH[^\]]*\]\s*")
_EMAIL = re.compile(r"<([^>]+)>")
_P1 = re.compile(r"^(?:diff --git a/|--- a/|\+\+\+ b/)", re.MULTILINE)


@dataclass
class Presence:
    status: PresenceStatus
    # files: {path: present / not_present / unknown}; history: [{subject, match, sha}]
    # per format-patch commit (match/sha None when not found); reason when unknown.
    detail: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def unknown(cls, reason: str) -> "Presence":
        return cls("unknown", {"reason": reason})


def check_presence(project_dir: Path, diff_text: str, upstream_domains: Iterable[str] = DEFAULT_UPSTREAM_DOMAINS) -> Presence:
    commits = _split_commits(diff_text)
    files = _reverse_check_files(project_dir, commits)
    detail: dict[str, Any] = {"files": files}
    known = [r for r in files.values() if r != "unknown"]
    if known and all(r == "present" for r in known):
        return Presence("present", detail)
    some_files = "present" in known

    if not any(parsed.commits for _, parsed in commits):
        if not known:
            return Presence("unknown", {**detail, "reason": "no file could be checked"})
        return Presence("partial" if some_files else "not_present", detail)

    try:
        history = [
            _find_in_history(project_dir, header, parsed.files, _is_upstream(header.author, upstream_domains))
            for (_, parsed) in commits
            for header in parsed.commits
        ]
        shallow = is_shallow(project_dir)
    except GitError:
        return Presence("partial" if some_files else "unknown", {**detail, "reason": HISTORY_UNAVAILABLE})
    detail["history"] = history
    found = sum(h["sha"] is not None for h in history)
    if found == len(history):
        return Presence("present", detail)
    if found or some_files:
        return Presence("partial", detail)
    if shallow:
        return Presence("unknown", {**detail, "reason": HISTORY_UNAVAILABLE})
    return Presence("not_present", detail)


def combine(statuses: Iterable[PresenceStatus]) -> PresenceStatus:
    """Item-level presence: present only when every diff is present."""
    statuses = list(statuses)
    if statuses and all(s == "present" for s in statuses):
        return "present"
    if any(s in ("present", "partial") for s in statuses):
        return "partial"
    if not statuses or "unknown" in statuses:
        return "unknown"
    return "not_present"


def _split_commits(text: str) -> list[tuple[str, Any]]:
    """[(text, ParsedDiff)] per format-patch commit; a plain diff is one entry."""
    starts = [m.start() for m in _FROM_SHA.finditer(text)] or [0]
    starts[0] = 0
    chunks = [text[a:b] for a, b in zip(starts, [*starts[1:], len(text)])]
    return [(chunk, parse_diff(chunk)) for chunk in chunks]


def _reverse_check_files(project_dir: Path, commits: list[tuple[str, Any]]) -> dict[str, FileResult]:
    """Reverse-apply each file on its own, with its sections from every commit of a series.

    Sections go in series order: `git apply --reverse` undoes them newest first itself.
    """
    sections: dict[str, list[FileChange]] = {}
    for _, parsed in commits:
        for change in parsed.files:
            key = change.old_path if change.old_path in sections else change.path
            sections.setdefault(key, []).append(change)
    results: dict[str, FileResult] = {}
    for key, changes in sections.items():
        path = changes[-1].path
        if any(c.binary and "GIT binary patch" not in c.raw for c in changes):
            results[path] = "unknown"  # "Binary files differ": nothing to apply
            continue
        patch = "".join(c.raw for c in changes)
        strip = "-p1" if _P1.search(patch) else "-p0"
        result = git(project_dir, "apply", "--reverse", "--check", strip, "-", input=patch, check=False)
        results[path] = "present" if result.returncode == 0 else "not_present"
    return results


def _is_upstream(author: str | None, domains: Iterable[str]) -> bool:
    match = _EMAIL.search(author or "")
    if not match:
        return False
    domain = match.group(1).rpartition("@")[2].lower()
    return any(domain == d or domain.endswith("." + d) for d in (d.lower().strip() for d in domains) if d)


def _find_in_history(project_dir: Path, commit: Commit, files: list[FileChange], upstream: bool) -> dict[str, Any]:
    subject = _PATCH_TAG.sub("", commit.subject)
    entry: dict[str, Any] = {"subject": subject, "match": None, "sha": None}
    lookups: list[tuple[HistoryMatch, Any]] = []
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
    out = git(project_dir, "log", "HEAD", "-F", f"--grep={text}", "--format=%H", "-n", "1").stdout
    return out.strip() or None


def _by_subject(project_dir: Path, subject: str) -> str | None:
    out = git(project_dir, "log", "HEAD", "-F", f"--grep={subject}", "--format=%H%x00%s").stdout
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
    log = git(project_dir, "log", "HEAD", "-p", "--no-color", "--no-ext-diff", "--format=commit %H", "--", *paths).stdout
    for patch_id, sha in _patch_id_pairs(project_dir, log):
        if patch_id in wanted:
            return sha
    return None


def _patch_ids(project_dir: Path, patch: str) -> set[str]:
    return {patch_id for patch_id, _ in _patch_id_pairs(project_dir, patch)}


def _patch_id_pairs(project_dir: Path, text: str) -> list[tuple[str, str]]:
    if not text.strip():
        return []
    out = git(project_dir, "patch-id", "--stable", input=text).stdout
    pairs = [line.split() for line in out.splitlines()]
    return [(pair[0], pair[1]) for pair in pairs if len(pair) == 2]
