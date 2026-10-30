"""Mechanical checks of a reworked patch, before any build and without Copilot.

In the scratch checkout after Copilot's edits: no conflict markers left in the files the
patch writes, no `.rej` files, the patch applies (`git apply --check`) on its base commit,
and it writes only files of the item's diffs. The first three are errors (fed back as a
fix round, an attempt fails on one that remains); another file written is a warning,
shown in review.
"""

import re
import tempfile
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal

from aosp_migrator.prescan import git
from aosp_migrator.prescan.patch import Patch
from aosp_migrator.rework.scratch import Scratch

Severity = Literal["error", "warning"]

# Start, base and end markers; a bare `=======` may be real content (rst, generated text).
_MARKER = re.compile(r"^(?:<{7}|\|{7}|>{7})(?: |$)")


@dataclass(frozen=True)
class Finding:
    kind: Literal["check", "intent"]
    severity: Severity
    path: str | None
    message: str

    def text(self) -> str:
        return f"{self.path}: {self.message}" if self.path else self.message

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


def errors(findings: Iterable[Finding]) -> list[Finding]:
    return [f for f in findings if f.severity == "error"]


def patch_paths(patch: str) -> list[str]:
    """Every path the patch writes (both sides of a rename), in patch order."""
    paths: dict[str, None] = {}
    for section in Patch.parse(patch).files:
        for change in section.changes:
            paths.update(dict.fromkeys(p for p in (change.old_path, change.path) if p))
    return list(paths)


def marker_lines(file: Path) -> list[int]:
    """Lines (1-based) of a file that start with a conflict marker."""
    try:
        text = file.read_text(errors="replace")
    except OSError:
        return []
    return [n for n, line in enumerate(text.splitlines(), 1) if _MARKER.match(line)]


def rej_files(scratch: Scratch) -> list[str]:
    return sorted(p.relative_to(scratch.path).as_posix() for p in scratch.path.rglob("*.rej") if p.is_file() and ".git" not in p.relative_to(scratch.path).parts)


def mechanical(scratch: Scratch, patch: str | None, item_files: Iterable[str]) -> list[Finding]:
    """Check findings of `patch` (from `Scratch.patch`) as made in `scratch`; `item_files` are the files the item's diffs touch."""
    findings = [Finding("check", "error", path, "rejected hunks file left") for path in rej_files(scratch)]
    if patch is None:
        return [*findings, Finding("check", "error", None, "no change against the base")]
    paths = patch_paths(patch)
    for path in paths:
        if lines := marker_lines(scratch.path / path):
            findings.append(Finding("check", "error", path, f"conflict markers left (line {', '.join(map(str, lines[:5]))})"))
    if error := apply_check(scratch, patch):
        findings.append(Finding("check", "error", None, f"does not apply on its base: {error}"))
    allowed = set(item_files)
    findings += [Finding("check", "warning", path, "not a file of the item's diffs") for path in paths if path not in allowed and not path.endswith(".rej")]
    return findings


def apply_check(scratch: Scratch, patch: str) -> str | None:
    """`git apply --check` of `patch` on the base commit, in a temp index; None when it applies, else the error."""
    with tempfile.TemporaryDirectory(prefix="aosp-migrator-check-") as tmp:
        env = {"GIT_INDEX_FILE": str(Path(tmp) / "index")}
        git.git(scratch.path, "read-tree", scratch.base, env=env)
        result = git.apply(scratch.path, patch, "--cached", "--check", env=env)
    if result.returncode == 0:
        return None
    lines = [line.removeprefix("error: ") for line in result.stderr.splitlines() if line.startswith("error: ")]
    return lines[0] if lines else result.stderr.strip()
