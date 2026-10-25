"""Diff sniffing and parsing: file list and +/- counts. No repo mapping here (that is MS2).

Handles `git format-patch` output (one or more commits per file), plain git diffs,
and plain unified diffs without `diff --git` headers. Lines are counted from the
hunk header ranges, so a format-patch signature (`-- `) is never taken as a removal.
"""

import re
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

# How many leading lines the sniffer looks at.
SNIFF_LINES = 50

FileStatus = Literal["modified", "added", "deleted", "renamed", "copied"]

_FROM_SHA = re.compile(r"^From [0-9a-f]{40} ")
_HUNK = re.compile(r"^@@ -\d+(?:,(\d+))? \+\d+(?:,(\d+))? @@")
_C_ESCAPES = {"n": "\n", "t": "\t", '"': '"', "\\": "\\", "a": "\a", "b": "\b", "f": "\f", "r": "\r", "v": "\v"}


@dataclass
class FileChange:
    path: str  # Path after the change; the old path for a deleted file
    old_path: str | None = None  # Set for renames and copies
    status: FileStatus = "modified"
    added: int = 0
    removed: int = 0
    binary: bool = False


@dataclass
class ParsedDiff:
    format: Literal["format-patch", "plain"]
    files: list[FileChange] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


def looks_like_diff(text: str) -> bool:
    """True if the first lines look like a patch: `From <sha>`, `diff --git`, or `---`/`+++`/`@@`."""
    lines = text.splitlines()[:SNIFF_LINES]
    if lines and _FROM_SHA.match(lines[0]):
        return True
    for i, line in enumerate(lines):
        if line.startswith("diff --git "):
            return True
        if (
            line.startswith("--- ")
            and i + 2 < len(lines)
            and lines[i + 1].startswith("+++ ")
            and _HUNK.match(lines[i + 2])
        ):
            return True
    return False


def parse_diff(text: str) -> ParsedDiff:
    lines = text.splitlines()
    first = next((line for line in lines if line.strip()), "")
    result = ParsedDiff(format="format-patch" if _FROM_SHA.match(first) else "plain")
    current: FileChange | None = None
    # True while `current` came from `diff --git` and its ---/+++ lines are still to come.
    git_sides_pending = False
    i = 0
    while i < len(lines):
        line = lines[i]
        if _FROM_SHA.match(line):
            current, git_sides_pending = None, False  # Next commit of a series
        elif line.startswith("diff --git "):
            current = _git_header(line[len("diff --git ") :])
            result.files.append(current)
            git_sides_pending = True
        elif line.startswith("--- ") and i + 1 < len(lines) and lines[i + 1].startswith("+++ "):
            old, new = _side_path(line[4:]), _side_path(lines[i + 1][4:])
            if current is not None and git_sides_pending:
                _apply_git_paths(current, old, new)
            else:
                # Plain unified diff: no `diff --git` header announced this file.
                current = FileChange(path="")
                result.files.append(current)
                _apply_plain_paths(current, old, new)
            git_sides_pending = False
            i += 2
            continue
        elif current is not None and (hunk := _HUNK.match(line)):
            i = _count_hunk(lines, i + 1, current, _count(hunk.group(1)), _count(hunk.group(2)))
            continue
        elif current is not None:
            _git_extended_header(line, current)
        i += 1
    return result


def _count(group: str | None) -> int:
    return 1 if group is None else int(group)


def _count_hunk(lines: list[str], i: int, change: FileChange, old_left: int, new_left: int) -> int:
    """Count +/- lines of one hunk starting at `i`; return the index after it."""
    while i < len(lines) and (old_left > 0 or new_left > 0):
        line = lines[i]
        if line.startswith("+"):
            change.added += 1
            new_left -= 1
        elif line.startswith("-"):
            change.removed += 1
            old_left -= 1
        elif line.startswith("\\"):
            pass  # "\ No newline at end of file"
        elif line.startswith(" ") or line == "":
            old_left -= 1
            new_left -= 1
        else:
            break  # Truncated hunk; let the caller look at this line
        i += 1
    while i < len(lines) and lines[i].startswith("\\"):
        i += 1
    return i


def _git_header(rest: str) -> FileChange:
    old, new = _split_git_names(rest)
    change = FileChange(path=_strip_prefix(new, "b/"), old_path=_strip_prefix(old, "a/"))
    _settle_old_path(change)
    return change


def _git_extended_header(line: str, change: FileChange) -> None:
    if line.startswith("new file mode"):
        change.status = "added"
    elif line.startswith("deleted file mode"):
        change.status = "deleted"
    elif line.startswith("rename from "):
        change.status = "renamed"
        change.old_path = _unquote(line[len("rename from ") :])
    elif line.startswith("rename to "):
        change.path = _unquote(line[len("rename to ") :])
    elif line.startswith("copy from "):
        change.status = "copied"
        change.old_path = _unquote(line[len("copy from ") :])
    elif line.startswith("copy to "):
        change.path = _unquote(line[len("copy to ") :])
    elif line == "GIT binary patch" or (line.startswith("Binary files ") and line.endswith(" differ")):
        change.binary = True
    else:
        return
    _settle_old_path(change)


def _apply_git_paths(change: FileChange, old: str | None, new: str | None) -> None:
    # ---/+++ names are exact (the `diff --git` line is ambiguous with spaces).
    if new is not None:
        change.path = _strip_prefix(new, "b/")
    if old is not None:
        change.old_path = _strip_prefix(old, "a/")
    if old is None:
        change.status = "added"
    elif new is None:
        change.status = "deleted"
        change.path = change.old_path or change.path
    _settle_old_path(change)


def _apply_plain_paths(change: FileChange, old: str | None, new: str | None) -> None:
    if old is not None and new is not None and old.startswith("a/") and new.startswith("b/"):
        old, new = old[2:], new[2:]
    if old is None:
        change.status = "added"
    elif new is None:
        change.status = "deleted"
    change.path = new if new is not None else old or ""


def _settle_old_path(change: FileChange) -> None:
    """Keep `old_path` only where it carries information."""
    if change.status not in ("renamed", "copied"):
        if change.status == "deleted" and change.old_path:
            change.path = change.old_path
        change.old_path = None


def _side_path(raw: str) -> str | None:
    """Path from a `---`/`+++` line, without timestamp; None for /dev/null."""
    path = _unquote(raw.split("\t", 1)[0])
    return None if path == "/dev/null" else path


def _split_git_names(rest: str) -> tuple[str, str]:
    """`a/X b/Y` from a `diff --git` line. Names may be C-quoted or contain spaces."""
    if rest.startswith('"'):
        end = _quoted_end(rest)
        old, tail = _unquote(rest[:end]), rest[end:].lstrip(" ")
        return old, _unquote(tail)
    start = rest.rfind(' "')
    if rest.endswith('"') and start != -1:
        return rest[:start], _unquote(rest[start + 1 :])
    # Unquoted with spaces: `a/P b/P` is symmetric when the path is unchanged.
    mid = len(rest) // 2
    if len(rest) % 2 == 1 and rest[mid] == " " and rest[2:mid] == rest[mid + 3 :]:
        return rest[:mid], rest[mid + 1 :]
    split = rest.rfind(" b/")
    if split == -1:
        split = rest.find(" ")
    return rest[:split], rest[split + 1 :]


def _quoted_end(s: str) -> int:
    i = 1
    while i < len(s):
        if s[i] == "\\":
            i += 2
            continue
        if s[i] == '"':
            return i + 1
        i += 1
    return len(s)


def _unquote(s: str) -> str:
    """Undo git's C-style quoting of paths with special characters (octal escapes are UTF-8 bytes)."""
    if len(s) < 2 or not (s.startswith('"') and s.endswith('"')):
        return s
    out = bytearray()
    body = s[1:-1]
    i = 0
    while i < len(body):
        c = body[i]
        if c == "\\" and i + 1 < len(body):
            nxt = body[i + 1]
            octal = body[i + 1 : i + 4]
            if len(octal) == 3 and all(d in "01234567" for d in octal):
                out.append(int(octal, 8) & 0xFF)
                i += 4
                continue
            out.extend(_C_ESCAPES.get(nxt, nxt).encode())
            i += 2
            continue
        out.extend(c.encode())
        i += 1
    return out.decode("utf-8", errors="replace")


def _strip_prefix(path: str, prefix: str) -> str:
    return path[len(prefix) :] if path.startswith(prefix) else path
