"""Parse unified git diffs into a footprint: files, change kinds, line counts and symbols."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_DIFF_HEADER = re.compile(r"^diff --git a/(.+?) b/(.+)$")
_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+\d+(?:,\d+)? @@ ?(.*)$")
# Declarations worth tracking for symbol dependencies between patches: Java/Kotlin/C++
# methods and classes, AIDL methods, Android resources.
_DECL = [
    re.compile(r"\b(?:class|interface|enum|object|struct)\s+([A-Z]\w*)"),
    re.compile(
        r"^\s*(?:(?:public|protected|private|static|final|synchronized|abstract|override|"
        r"virtual|inline|suspend|fun|oneway|native)\s+)*[\w<>\[\],.:*&\s]+?\b([a-zA-Z_]\w*)\s*\("
        r"[^;]*\)\s*(?:const\s*)?(?:throws [\w.,\s]+)?\s*[{:]?\s*$"
    ),
    re.compile(r"<(?:integer|bool|string|dimen|color|array|string-array)\s+name=\"(\w+)\""),
]
_NOT_SYMBOLS = {"if", "for", "while", "switch", "catch", "return", "new", "super", "this", "else"}


@dataclass
class FileChange:
    path: str
    status: str = "M"  # A added, M modified, D deleted, R renamed
    old_path: str | None = None
    added: int = 0
    removed: int = 0
    binary: bool = False


@dataclass
class Footprint:
    files: list[FileChange] = field(default_factory=list)
    defined: set[str] = field(default_factory=set)  # symbols declared in added lines
    touched: set[str] = field(default_factory=set)  # enclosing symbols of hunks (context)

    def to_json(self) -> dict:
        return {
            "files": [vars(f) for f in self.files],
            "defined": sorted(self.defined),
            "touched": sorted(self.touched),
        }


def _declared(line: str) -> str | None:
    for pattern in _DECL:
        m = pattern.search(line)
        if m and m.group(1) not in _NOT_SYMBOLS:
            return m.group(1)
    return None


def parse_diff(diff: str) -> Footprint:
    fp = Footprint()
    current: FileChange | None = None
    in_hunk = False
    for line in diff.splitlines():
        if m := _DIFF_HEADER.match(line):
            current = FileChange(path=m.group(2))
            if m.group(1) != m.group(2):
                current.status, current.old_path = "R", m.group(1)
            fp.files.append(current)
            in_hunk = False
            continue
        if current is None:
            continue
        if not in_hunk:
            if line.startswith("new file mode"):
                current.status = "A"
            elif line.startswith("deleted file mode"):
                current.status = "D"
            elif line.startswith("rename from "):
                current.status, current.old_path = "R", line.removeprefix("rename from ")
            elif line.startswith("rename to "):
                current.path = line.removeprefix("rename to ")
            elif line.startswith("Binary files"):
                current.binary = True
        if m := _HUNK.match(line):
            in_hunk = True
            if m.group(1) and (sym := _declared(m.group(1))):
                fp.touched.add(sym)
            continue
        if not in_hunk:
            continue
        if line.startswith("+"):
            current.added += 1
            if sym := _declared(line[1:]):
                fp.defined.add(sym)
        elif line.startswith("-"):
            current.removed += 1
    return fp
