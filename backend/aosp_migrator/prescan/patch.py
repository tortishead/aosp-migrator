"""A diff split the way the pre-scan checks it: per format-patch commit, then per file.

A series touches a file in several commits; `git apply` takes all of that file's
sections together, in series order, so presence and apply checks work per file.
"""

import re
from dataclasses import dataclass
from functools import cached_property

from aosp_migrator.importer.diffparse import Commit, FileChange, ParsedDiff, parse_diff

_FROM_SHA = re.compile(r"^From [0-9a-f]{40} ", re.MULTILINE)
_P1 = re.compile(r"^(?:diff --git a/|--- a/|\+\+\+ b/)", re.MULTILINE)
_PATCH_TAG = re.compile(r"^\[PATCH[^\]]*\]\s*")


@dataclass(frozen=True)
class FileSections:
    """One file's changes from every commit of a series, in series order."""

    changes: list[FileChange]

    @property
    def path(self) -> str:
        """The file's path after the last change (renames follow the file)."""
        return self.changes[-1].path

    @cached_property
    def patch(self) -> str:
        return "".join(c.raw for c in self.changes)

    @property
    def binary(self) -> bool:
        """A "Binary files differ" section: nothing `git apply` could check."""
        return any(c.binary and "GIT binary patch" not in c.raw for c in self.changes)


@dataclass(frozen=True)
class Patch:
    commits: list[ParsedDiff]

    @classmethod
    def parse(cls, text: str) -> "Patch":
        """One entry per format-patch commit; a plain diff is one entry."""
        starts = [m.start() for m in _FROM_SHA.finditer(text)] or [0]
        starts[0] = 0
        return cls([parse_diff(text[a:b]) for a, b in zip(starts, [*starts[1:], len(text)])])

    @property
    def headers(self) -> list[tuple[Commit, list[FileChange]]]:
        """Each format-patch commit header with the files of its part of the series."""
        return [(header, parsed.files) for parsed in self.commits for header in parsed.commits]

    @property
    def author(self) -> str | None:
        """Author of the first commit; None for a plain diff."""
        return next((commit.author for commit, _ in self.headers if commit.author), None)

    @property
    def message(self) -> str | None:
        """Commit message without `[PATCH n/m]` tags, the messages of a series one after another; None for a plain diff."""
        messages = [
            "\n\n".join(part for part in (_PATCH_TAG.sub("", commit.subject), commit.message.strip()) if part)
            for commit, _ in self.headers
        ]
        return "\n\n".join(m for m in messages if m) or None

    @cached_property
    def files(self) -> list[FileSections]:
        sections: dict[str, list[FileChange]] = {}
        for parsed in self.commits:
            for change in parsed.files:
                key = change.old_path if change.old_path in sections else change.path
                sections.setdefault(key, []).append(change)
        return [FileSections(changes) for changes in sections.values()]


def strip_option(patch: str) -> str:
    """`-p1` for `a/` and `b/` prefixed paths, else `-p0`."""
    return "-p1" if _P1.search(patch) else "-p0"
