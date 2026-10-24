"""Per-file detail of a unified diff: paths, added/removed lines and the old-side lines touched.

The footprint (ingest/diffparse.py) summarises a patch; this module keeps what the analysis
compares against the target: exact lines, and line ranges in the coordinates of the file the
diff applies to.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_DIFF_HEADER = re.compile(r"^diff --git a/(.+?) b/(.+)$")
_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


@dataclass
class FileDiff:
    old_path: str | None  # None: the diff adds the file
    new_path: str | None  # None: the diff deletes the file
    added: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    changed: list[tuple[int, int]] = field(default_factory=list)  # old-side ranges, inclusive
    binary: bool = False  # content not in the diff ("Binary files ... differ")

    @property
    def path(self) -> str:
        return self.old_path or self.new_path or ""


def _mark(ranges: list[tuple[int, int]], start: int, end: int) -> None:
    if ranges and start <= ranges[-1][1] + 1:
        ranges[-1] = (ranges[-1][0], max(end, ranges[-1][1]))
    else:
        ranges.append((start, end))


def split_diff(diff: str) -> list[FileDiff]:
    files: list[FileDiff] = []
    current: FileDiff | None = None
    old_line = 0
    in_hunk = False
    for line in diff.splitlines():
        if m := _DIFF_HEADER.match(line):
            current = FileDiff(old_path=m.group(1), new_path=m.group(2))
            files.append(current)
            in_hunk = False
            continue
        if current is None:
            continue
        if m := _HUNK.match(line):
            in_hunk = True
            old_line = int(m.group(1)) + (1 if m.group(2) == "0" else 0)
            continue
        if not in_hunk:
            if line.startswith("new file mode") or line == "--- /dev/null":
                current.old_path = None
            elif line.startswith("deleted file mode") or line == "+++ /dev/null":
                current.new_path = None
            elif line.startswith("rename from "):
                current.old_path = line.removeprefix("rename from ")
            elif line.startswith("rename to "):
                current.new_path = line.removeprefix("rename to ")
            elif line.startswith(("Binary files ", "GIT binary patch")):
                current.binary = True
            continue
        if line.startswith("-"):
            current.removed.append(line[1:])
            _mark(current.changed, old_line, old_line)
            old_line += 1
        elif line.startswith("+"):
            current.added.append(line[1:])
            # An insertion sits between two old lines; count both neighbours as touched.
            _mark(current.changed, max(old_line - 1, 1), old_line)
        elif line.startswith(" "):
            old_line += 1
        elif line.startswith("\\"):
            continue  # "\ No newline at end of file"
        else:
            in_hunk = False
    return files


def overlaps(a: list[tuple[int, int]], b: list[tuple[int, int]], pad: int = 0) -> bool:
    return any(s1 - pad <= e2 and s2 - pad <= e1 for s1, e1 in a for s2, e2 in b)


def normalize_line(line: str) -> str:
    return " ".join(line.split())
