"""The prompt of one Copilot call: what the rung asks for, then context in a fixed priority, cut at the cap.

Priority (first kept first, the rest cut when the cap is reached):
1. the findings of the round before (a fix round), or the build errors (a build-fix round)
2. ticket key, summary and description (trimmed)
3. the user's reject note and the previous attempt's failure reason
4. the failing files: conflict regions with `CONTEXT` lines around (resolve), or the
   `.rej` hunks plus the current code where they would go (port), or why the file did
   not apply and where the target has it now (re-implement; Copilot reads it there); in
   a fix round the change so far for those files
5. the original diff's sections for those files
6. re-implement: the source tree's file before and after the change
7. per prerequisite touching those files: its summary and, for those files, its
   original diff next to its migrated patch (renames and API changes the dependent
   needs too); "migrated unchanged" when they make the same changes
The instructions (what to edit, what not to touch) open the prompt and the request for a
short explanation per file closes it; neither counts against the cap. The part that
crosses the cap is cut there (marked), the parts after it left out; which is noted per
part, for the attempt.
"""

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from aosp_migrator.prescan.patch import Patch
from aosp_migrator.rework.locate import Location
from aosp_migrator.rework.scratch import FileResult

# Characters of context per call, instructions aside.
CAP = 60_000
# Characters of the ticket description.
DESCRIPTION_CAP = 4_000
# Lines around a conflict region or where a rejected hunk would go.
CONTEXT = 10
CUT = "\n[... cut at the size limit ...]\n"

Rung = Literal["resolve", "port", "reimpl"]
# What a fix round's feedback is: check findings, or the build's error lines.
Feedback = Literal["checks", "build"]
_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+\d+(?:,\d+)? @@", re.MULTILINE)


@dataclass
class Prerequisite:
    key: str
    summary: str
    # Its diffs for the project, as in Jira.
    original: str
    # Its current patch for the project; None when it has none yet.
    migrated: str | None


@dataclass
class Context:
    """What a bundle says about the item besides the files."""

    key: str
    summary: str
    description: str
    note: str | None = None  # The user's reject note on the last patch
    previous_reason: str | None = None  # Why the last attempt failed
    prerequisites: list[Prerequisite] = field(default_factory=list)


@dataclass
class Part:
    name: str
    text: str
    kept: Literal["whole", "cut", "left out"] = "whole"


@dataclass
class Bundle:
    prompt: str
    parts: list[Part]

    def summary(self) -> dict[str, str]:
        return {part.name: part.kept for part in self.parts}


def instructions(rung: Rung, paths: Sequence[str], target: Path | None, fix_round: Feedback | None) -> str:
    files = "\n".join(f"- {p}" for p in paths)
    if rung == "resolve":
        task = (
            "A change from another Android tree is being ported into this checkout. A 3-way merge left git conflict markers "
            "(diff3 style) in the files below: between `<<<<<<<` and `|||||||` is the code in this tree now, between "
            "`|||||||` and `=======` the code the change was written against, between `=======` and `>>>>>>>` the change. "
            "Resolve every conflict region in place: keep this tree's code and apply what the change does to it. "
            "Remove every conflict marker."
        )
    elif rung == "port":
        task = (
            "A change from another Android tree is being ported into this checkout. The rest of it applied; the hunks below "
            "did not, as the code they change differs here. Edit the files below so that each hunk's change is made to the "
            "current code (where the code moved, where it is now)."
        )
    else:
        task = (
            "A change from another Android tree is being ported into this checkout. For the files below it could not be "
            "applied at all: the file moved, was rewritten or is gone here. Re-implement what the change does, from its "
            "intent (ticket, original change, the source file before and after it), in the files below as they are in this "
            "tree. Where a file is gone, make the change where its code lives now; create the file only if it belongs here."
        )
    if fix_round == "checks":
        task += " Your previous edit did not pass the checks listed below: fix what they name."
    elif fix_round == "build":
        task += " The change as it is now does not build: fix the build errors listed below in these files."
    rules = (
        f"Files to edit:\n{files}\n\n"
        "Edit only these files, in this directory. Do not create other files, do not write `.orig` or `.rej` files, "
        "do not run anything. Do not add, drop or reformat code the change does not touch."
    )
    if target is not None:
        rules += f" The rest of the target tree is at {target}: read it there when you need other code (read-only)."
    return f"{task}\n\n{rules}\n"


CLOSING = "\nWhen done, answer with one short line per file: `<path>: what you changed and why`.\n"


def build(
    rung: Rung,
    files: Sequence[FileResult],
    originals: Sequence[str],
    context: Context,
    checkout: Path,
    target: Path | None = None,
    feedback: Sequence[str] = (),
    change_so_far: str | None = None,
    cap: int = CAP,
    feedback_kind: Feedback = "checks",
    locations: Mapping[str, Location] | None = None,
) -> Bundle:
    """The prompt for `files` (the failing ones, as rungs 1-2 or the previous round left them) in `checkout`.

    `locations`: for re-implement, per file of the diff where the target has it; the files to edit are those.
    """
    locations = locations or {}
    paths = [f.path for f in files]
    edit_paths = list(dict.fromkeys(locations[p].edit_path if p in locations else p for p in paths))
    parts: list[Part] = []
    if feedback and feedback_kind == "build":
        parts.append(Part("feedback", "## Build errors\n```\n" + "".join(f"{line}\n" for line in feedback) + "```\n"))
    elif feedback:
        parts.append(Part("feedback", "## Checks your previous edit failed\n" + "".join(f"- {line}\n" for line in feedback)))
    description = context.description.strip()
    if len(description) > DESCRIPTION_CAP:
        description = description[:DESCRIPTION_CAP] + CUT
    parts.append(Part("ticket", f"## Ticket {context.key}: {context.summary}\n{description}\n"))
    if context.note or context.previous_reason:
        text = "## Earlier attempts\n"
        if context.previous_reason:
            text += f"The previous attempt failed: {context.previous_reason}\n"
        if context.note:
            text += f"The reviewer rejected the last patch: {context.note}\n"
        parts.append(Part("history", text))
    if change_so_far:
        parts.append(Part("files", f"## Your change so far (against the base)\n```diff\n{change_so_far}```\n"))
    else:
        parts.append(Part("files", "".join(_location_text(f, locations[f.path]) if f.path in locations else _file_text(checkout, f) for f in files)))
    sections = original_sections(originals, paths)
    if sections:
        parts.append(Part("original", f"## The original change for these files\n```diff\n{sections}```\n"))
    source = "".join(_source_text(locations[p]) for p in paths if p in locations)
    if source:
        parts.append(Part("source", "## In the source tree, before and after the change\n" + source))
    prerequisites = "".join(_prerequisite_text(p, paths) for p in context.prerequisites)
    if prerequisites:
        parts.append(Part("prerequisites", "## Changes this one builds on (migrated before it)\n" + prerequisites))
    return Bundle(instructions(rung, edit_paths, target, feedback_kind if feedback else None) + "\n" + _cut(parts, cap) + CLOSING, parts)


def _cut(parts: list[Part], cap: int) -> str:
    out: list[str] = []
    left = cap
    for part in parts:
        if left <= 0:
            part.kept = "left out"
        elif len(part.text) <= left:
            out.append(part.text)
            left -= len(part.text)
        else:
            out.append(part.text[:left] + CUT)
            part.kept = "cut"
            left = 0
    return "\n".join(out)


def _numbered(lines: list[str], start: int, end: int) -> str:
    """Lines `start`..`end` (1-based, clipped) with their numbers."""
    start, end = max(start, 1), min(end, len(lines))
    return "".join(f"{n:>6}  {lines[n - 1]}" for n in range(start, end + 1))


def _file_text(checkout: Path, result: FileResult) -> str:
    try:
        lines = (checkout / result.path).read_text(errors="replace").splitlines(keepends=True)
    except OSError:
        lines = []
    if lines and not lines[-1].endswith("\n"):
        lines[-1] += "\n"
    text = f"## {result.path}\n"
    if result.regions:
        for n, region in enumerate(result.regions, 1):
            text += f"Conflict {n}, lines {region.start}-{region.end}:\n```\n{_numbered(lines, region.start - CONTEXT, region.end + CONTEXT)}```\n"
    for n, hunk in enumerate(result.hunks, 1):
        text += f"Hunk {n} that did not apply:\n```diff\n{hunk}```\n"
        if match := _HUNK.match(hunk):
            start, length = int(match.group(1)), int(match.group(2) or 1)
            text += f"The file now around line {start}:\n```\n{_numbered(lines, start - CONTEXT, start + length + CONTEXT)}```\n"
    return text


def _location_text(result: FileResult, location: Location) -> str:
    text = f"## {result.path}\nDid not apply: {result.error or result.status}.\n"
    if location.how == "path":
        return text + "The file is still here, changed too much for the change to apply: read it and edit it in place.\n"
    if location.found is not None:
        return text + f"In this tree it {location.describe()}: read it and edit it there.\n"
    return text + (
        "No file like it in this tree: it is gone. If the change still belongs here, create the file at its path with what "
        "the change needs; if not, leave it and say why.\n"
    )


def _source_text(location: Location) -> str:
    if location.before is None and location.after is None:
        return ""
    text = f"### {location.path}\n"
    text += f"Before:\n```\n{location.before}```\n" if location.before is not None else "Before: (a new file)\n"
    text += f"After:\n```\n{location.after}```\n" if location.after is not None else "After: (deleted)\n"
    return text


def original_sections(diffs: Sequence[str], paths: Sequence[str]) -> str:
    """The sections of `diffs` for `paths` (by the path before or after), in diff order."""
    wanted = set(paths)
    return "".join(
        change.raw for text in diffs for section in Patch.parse(text).files for change in section.changes if {change.path, change.old_path} & wanted
    )


def _changed_lines(sections: str) -> list[str]:
    return [line for line in sections.splitlines() if line[:1] in "+-" and not line.startswith(("+++", "---"))]


def _prerequisite_text(prerequisite: Prerequisite, paths: Sequence[str]) -> str:
    original = original_sections([prerequisite.original], paths)
    if not original:
        return ""
    text = f"### {prerequisite.key}: {prerequisite.summary}\n"
    migrated = original_sections([prerequisite.migrated], paths) if prerequisite.migrated else ""
    if prerequisite.migrated is None:
        return text + f"Not migrated yet. Its original change:\n```diff\n{original}```\n"
    if _changed_lines(original) == _changed_lines(migrated):
        return text + "Migrated unchanged (already in the files above).\n"
    return text + f"Original:\n```diff\n{original}```\nAs migrated (already in the files above):\n```diff\n{migrated or '(no change to these files)\n'}```\n"
