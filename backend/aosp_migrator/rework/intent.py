"""Intent check of a reworked patch against the original diff, per file, without Copilot.

What the original change does should still be there after the rework:
- added lines kept: the original's added lines are in the file afterwards (whitespace
  normalized; lines without a letter or digit, such as a lone brace, left out). In the
  file rather than among the patch's added lines, as the target may have had one already.
- identifiers and string literals the original adds (in its added lines, not its removed
  ones) are in the file afterwards;
- no original hunk dropped: some added line of each hunk is in the file afterwards, or
  for a hunk that only removes, the patch removes one of its lines too;
- no removal the original did not make: a line the patch removes that is neither one the
  original removes nor a variant of one (its identifiers a subset of a removed line's,
  e.g. the target's `int b = 5;` where the original changed `int b = 2;`).

Thresholds per rung: strict for resolve (markers resolved in place, nothing should be
lost), looser for port (hunks rewritten for code that moved on), warnings only for
re-implement. A finding over its rung's threshold is an error (fed back as a fix round,
left on the proposed patch after the rounds), one within it a warning.
"""

import re
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from aosp_migrator.importer.diffparse import FileChange
from aosp_migrator.prescan.patch import Patch
from aosp_migrator.rework.checks import Finding

_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]{2,}")
_STRING = re.compile(r'"(?:[^"\\\n]|\\.)*"')
_WORD = re.compile(r"[A-Za-z0-9]")
# Listed in a finding, at most.
SHOWN = 3


@dataclass(frozen=True)
class Thresholds:
    added_kept: float  # Share of the original's added lines in the file afterwards
    identifiers_kept: float  # Share of its new identifiers and string literals
    dropped_hunks: int  # Original hunks with no trace, allowed
    extra_removals: int  # Lines removed that the original did not remove, allowed
    blocking: bool  # False: findings are warnings, whatever the counts


THRESHOLDS = {
    "resolve": Thresholds(1.0, 1.0, 0, 0, True),
    "port": Thresholds(0.8, 0.9, 0, 2, True),
    "reimpl": Thresholds(0.5, 0.7, 1, 5, False),
}


def normalize(line: str) -> str:
    return " ".join(line.split())


@dataclass
class _Lines:
    added: list[str]
    removed: list[str]


def _lines(changes: Sequence[FileChange]) -> list[_Lines]:
    """Per hunk its added and removed lines, normalized, trivial ones left out."""
    out: list[_Lines] = []
    for change in changes:
        for hunk in change.hunks:
            lines = _Lines([], [])
            for line in hunk.splitlines()[1:]:
                text = normalize(line[1:])
                if not _WORD.search(text):
                    continue
                if line.startswith("+"):
                    lines.added.append(text)
                elif line.startswith("-"):
                    lines.removed.append(text)
            out.append(lines)
    return out


def _changes(text: str) -> dict[str, list[FileChange]]:
    """A diff's changes per file, by the path before and after (a rename is found by either)."""
    files: dict[str, list[FileChange]] = {}
    for section in Patch.parse(text).files:
        for change in section.changes:
            for path in dict.fromkeys(p for p in (change.old_path, change.path) if p):
                files.setdefault(path, []).append(change)
    return files


def _identifiers(text: str) -> set[str]:
    """Identifiers outside string literals."""
    return set(_IDENTIFIER.findall(_STRING.sub('""', text)))


def _show(values: Sequence[str]) -> str:
    shown = ", ".join(f"`{v}`" for v in values[:SHOWN])
    return shown + (f" and {len(values) - SHOWN} more" if len(values) > SHOWN else "")


def check(
    originals: Sequence[str],
    patch: str | None,
    contents: Mapping[str, str | None],
    rung: str,
    paths: Sequence[str] | None = None,
    moved: Mapping[str, str] | None = None,
) -> list[Finding]:
    """Intent findings of `patch` against the `originals` (the item's diffs for the project).

    `contents`: each file's text after the patch (None: deleted or missing). `paths`
    limits the check to those files (the ones the rework changed); None checks them all.
    `moved`: a file's path in the originals to where the target has it (re-implement);
    its content and its part of the patch are looked up there.
    """
    limits = THRESHOLDS[rung]
    reworked = _changes(patch or "")
    findings: list[Finding] = []

    def finding(path: str, over: bool, message: str) -> None:
        findings.append(Finding("intent", "error" if over and limits.blocking else "warning", path, message))

    for text in originals:
        for section in Patch.parse(text).files:
            path = section.path
            if paths is not None and path not in paths:
                continue
            hunks = _lines(section.changes)
            where = (moved or {}).get(path, path)
            content = contents.get(where)
            after = Counter(normalize(line) for line in (content or "").splitlines())
            # Added lines, counted against the file afterwards (each line once per occurrence).
            added = Counter(line for hunk in hunks for line in hunk.added)
            missing = [line for line, count in added.items() for _ in range(count - min(count, after[line]))]
            if added and missing:
                kept = 1 - len(missing) / sum(added.values())
                finding(path, kept < limits.added_kept, f"{len(missing)} of {sum(added.values())} added lines missing: {_show(missing)}")
            # New identifiers and string literals.
            removed_text = "\n".join(line for hunk in hunks for line in hunk.removed)
            added_text = "\n".join(line for hunk in hunks for line in hunk.added)
            new = sorted({*(_identifiers(added_text) - _identifiers(removed_text)), *(set(_STRING.findall(added_text)) - set(_STRING.findall(removed_text)))})
            body = content or ""
            words = _identifiers(body)
            lost = [n for n in new if (n not in body if n.startswith('"') else n not in words)]
            if new and lost:
                finding(path, 1 - len(lost) / len(new) < limits.identifiers_kept, f"identifiers or strings the original adds missing: {_show(lost)}")
            # Hunks with no trace.
            mine = _lines(reworked.get(where, []))
            mine_removed = Counter(line for hunk in mine for line in hunk.removed)
            dropped = [
                n
                for n, hunk in enumerate(hunks, 1)
                if (hunk.added and not any(after[line] for line in hunk.added)) or (not hunk.added and hunk.removed and not any(mine_removed[line] for line in hunk.removed))
            ]
            if dropped:
                finding(path, len(dropped) > limits.dropped_hunks, f"hunk {', '.join(map(str, dropped))} of the original missing")
            # Removals the original did not make.
            original_removed = [line for hunk in hunks for line in hunk.removed]
            removed_sets = [_identifiers(line) for line in original_removed]
            left = Counter(original_removed)
            extra: list[str] = []
            for line in (line for hunk in mine for line in hunk.removed):
                if left[line]:
                    left[line] -= 1
                elif not any(_identifiers(line) <= ids for ids in removed_sets if ids):
                    extra.append(line)
            if extra:
                finding(path, len(extra) > limits.extra_removals, f"removes lines the original does not: {_show(extra)}")
    return findings
