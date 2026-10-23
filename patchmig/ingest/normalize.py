"""Turn raw patches from any source into normalised records with links between them."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime

from patchmig.gitutil import CommitInfo, patch_id
from patchmig.ingest.diffparse import Footprint, parse_diff

_CHANGE_ID = re.compile(r"^Change-Id:\s*(I[0-9a-f]{40})\s*$", re.M)
_TICKET_TRAILER = re.compile(r"^(?:Bug|Bugs|Jira|Issue|Ticket|Tracked-On):\s*(.+)$", re.M | re.I)
_JIRA_KEY = re.compile(r"\b([A-Z][A-Z0-9]{1,9}-\d+)\b")
_REVERTS = re.compile(r"This reverts commit ([0-9a-f]{7,40})")
_CHERRY = re.compile(r"\(cherry picked from commit ([0-9a-f]{7,40})\)")
_FIXES = re.compile(r"^Fixes:\s*([0-9a-f]{7,40})", re.M)
_FIXISH = re.compile(r"^(?:\[[^\]]*\]\s*)?(?:fix|fixup|bugfix|hotfix|correct)\b", re.I)


@dataclass
class RawPatch:
    """One patch as an adapter found it, before normalisation."""

    project: str
    subject: str
    message: str
    diff: str
    source: str  # adapter type that produced it
    commit_sha: str | None = None
    diff_path: str | None = None
    author: str | None = None
    authored_at: datetime | None = None
    jira_keys: list[str] = field(default_factory=list)  # extra keys known from the source

    @classmethod
    def from_commit(cls, project: str, info: CommitInfo, source: str) -> RawPatch:
        return cls(
            project=project,
            subject=info.subject,
            message=info.message,
            diff=info.diff,
            source=source,
            commit_sha=info.sha,
            author=info.author,
            authored_at=info.authored_at,
        )


@dataclass
class NormalizedPatch:
    project: str
    subject: str
    message: str
    commit_sha: str | None
    diff_path: str | None
    author: str | None
    authored_at: datetime | None
    change_id: str | None
    fingerprint: str | None
    jira_keys: list[str]
    footprint: Footprint
    sources: list[str]
    reverts: str | None = None  # sha prefix as written in the message
    cherry_picked_from: str | None = None
    fixes: list[str] = field(default_factory=list)
    links: dict = field(default_factory=dict)  # resolved links, filled by link_patches
    chain_role: str = "base"

    @property
    def key(self) -> str:
        return self.commit_sha or self.fingerprint or f"{self.project}:{self.subject}"


def jira_keys_in(subject: str, message: str, projects: set[str] | None = None) -> list[str]:
    """Jira keys from ticket trailers and the subject; filtered to known projects if given.

    The message body is not searched freely: text like "UTF-8" or "ANDROID-15" would match.
    """
    text = [subject, *(m.group(1) for m in _TICKET_TRAILER.finditer(message))]
    keys: list[str] = []
    for chunk in text:
        for key in _JIRA_KEY.findall(chunk):
            if (projects is None or key.split("-")[0] in projects) and key not in keys:
                keys.append(key)
    return keys


def normalize(raw: RawPatch, jira_projects: set[str] | None = None) -> NormalizedPatch:
    keys = jira_keys_in(raw.subject, raw.message, jira_projects)
    for key in raw.jira_keys:
        if key not in keys:
            keys.append(key)
    reverts = _REVERTS.search(raw.message)
    cherry = _CHERRY.search(raw.message)
    change_id = _CHANGE_ID.search(raw.message)
    return NormalizedPatch(
        project=raw.project,
        subject=raw.subject,
        message=raw.message,
        commit_sha=raw.commit_sha,
        diff_path=raw.diff_path,
        author=raw.author,
        authored_at=raw.authored_at,
        change_id=change_id.group(1) if change_id else None,
        fingerprint=patch_id(raw.diff),
        jira_keys=keys,
        footprint=parse_diff(raw.diff),
        sources=[raw.source],
        reverts=reverts.group(1) if reverts else None,
        cherry_picked_from=cherry.group(1) if cherry else None,
        fixes=_FIXES.findall(raw.message),
    )


def merge_duplicates(patches: list[NormalizedPatch]) -> list[NormalizedPatch]:
    """The same patch may come from several sources (git range, sheet, Jira): merge them.

    Identity is the commit SHA; a patch without one (a diff file) matches by patch-id. Two
    different commits are never merged, even with equal patch-ids. The first occurrence wins
    for metadata, except that a commit fills in identity a diff file lacked. Sources and Jira
    keys are combined, and the order of first appearance is kept.
    """
    result: list[NormalizedPatch] = []
    by_sha: dict[str, NormalizedPatch] = {}
    by_fingerprint: dict[str, NormalizedPatch] = {}
    for p in patches:
        existing = by_sha.get(p.commit_sha) if p.commit_sha else None
        if existing is None and p.fingerprint in by_fingerprint:
            candidate = by_fingerprint[p.fingerprint]
            if candidate.commit_sha is None or p.commit_sha is None:
                existing = candidate
        if existing is None:
            result.append(p)
            if p.commit_sha:
                by_sha[p.commit_sha] = p
            if p.fingerprint:
                by_fingerprint.setdefault(p.fingerprint, p)
            continue
        if existing.commit_sha is None and p.commit_sha is not None:
            for name in ("commit_sha", "author", "authored_at", "message", "subject", "change_id"):
                setattr(existing, name, getattr(p, name))
            existing.reverts, existing.fixes = p.reverts, p.fixes
            existing.cherry_picked_from = p.cherry_picked_from
            by_sha[p.commit_sha] = existing
        existing.sources += [s for s in p.sources if s not in existing.sources]
        existing.jira_keys += [k for k in p.jira_keys if k not in existing.jira_keys]
        existing.diff_path = existing.diff_path or p.diff_path
    return result


def _find(patches: list[NormalizedPatch], sha_prefix: str) -> NormalizedPatch | None:
    for p in patches:
        if p.commit_sha and p.commit_sha.startswith(sha_prefix):
            return p
    return None


def link_patches(patches: list[NormalizedPatch]) -> None:
    """Resolve reverts/fixes references to patches in the set and assign chain roles.

    Roles: ``revert`` reverts another patch; ``fix`` names the patch it fixes, or looks like a
    fix and shares a Jira key with an earlier patch; otherwise ``base``. Links point to commit
    SHAs so they survive re-ingestion; references outside the set are kept as ``external``.
    """
    seen_keys: set[str] = set()
    for p in patches:
        if p.reverts:
            target = _find(patches, p.reverts)
            if target:
                p.links["reverts"] = target.key
                target.links.setdefault("reverted_by", []).append(p.key)
            else:
                p.links["reverts_external"] = p.reverts
            p.chain_role = "revert"
        fixed = [t.key for sha in p.fixes if (t := _find(patches, sha))]
        if fixed:
            p.links["fixes"] = fixed
        if p.cherry_picked_from:
            p.links["cherry_picked_from"] = p.cherry_picked_from
        if p.chain_role != "revert" and (
            fixed or (_FIXISH.match(p.subject) and seen_keys.intersection(p.jira_keys))
        ):
            p.chain_role = "fix"
        seen_keys.update(p.jira_keys)
