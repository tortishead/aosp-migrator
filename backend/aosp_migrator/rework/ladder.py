"""The rework ladder for one project of an item (or unit) in its scratch checkout: rungs 1-2, then Copilot.

Rungs 1-2 (`Scratch.rungs`) need no Copilot. What they leave failing goes to Copilot:
3a resolve for the files left with conflict markers, then 3b port for the files with
`.rej` hunks (the `.rej` files are read and removed first). A file with neither (binary,
gone) needs re-implement (5.8): the attempt fails before any call.

After the calls, the checks (`checks.mechanical`, `intent.check` at the highest rung
used) run on the patch from git. Their errors go back to Copilot as a fix round, at that
rung, with the change so far, until they pass, a round changes nothing, or the budget is
spent. Check errors that remain fail the attempt; intent errors that remain are left on
the patch as findings, for review. Every call counts against the `Budget` (per item, its
projects share it; and the job's); a call it does not allow fails the attempt with
"Copilot budget reached".

After a build of the patch failed, `fix_build` gives Copilot the build's errors for the
patch's files, then runs the same checks and rounds.
"""

import copy
from collections.abc import Callable, Collection, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from aosp_migrator.config import CopilotSettings
from aosp_migrator.copilot.client import CopilotClient
from aosp_migrator.rework import bundle, checks, intent
from aosp_migrator.rework.bundle import Context
from aosp_migrator.rework.checks import Finding
from aosp_migrator.rework.scratch import FileResult, Scratch, touched_files

Status = Literal["patched", "failed", "canceled"]
BUDGET_REACHED = "Copilot budget reached"
# Copilot rungs the ladder runs when allowed (re-implement comes with 5.8).
COPILOT_RUNGS = ("resolve", "port")
# Characters of Copilot's answer kept with a call.
ANSWER_CAP = 20_000


class Budget:
    """Copilot calls allowed, shared by everything that takes from it; a call also takes from `parent` (the job's)."""

    def __init__(self, limit: int, parent: "Budget | None" = None) -> None:
        self.limit = limit
        self.used = 0
        self.parent = parent

    def left(self) -> int:
        own = self.limit - self.used
        return own if self.parent is None else min(own, self.parent.left())

    def take(self) -> bool:
        if self.used >= self.limit or (self.parent is not None and not self.parent.take()):
            return False
        self.used += 1
        return True


@dataclass
class Call:
    rung: str
    round: int  # 0 for the rung's call, then fix rounds
    model: str | None
    status: str
    prompt_path: str
    parts: dict[str, str]  # Bundle part to whole / cut / left out
    answer: str
    error: str
    usage: dict[str, Any] | None
    duration: float
    build_round: int = 0  # The build-fix round it belongs to; 0 before the first build


@dataclass
class LadderResult:
    status: Status
    reason: str | None = None
    level: str | None = None
    patch: str | None = None
    # Per file: path, status (applied / merged / resolved / ported / conflict / rejected / failed), rung, error, notes.
    files: list[dict[str, Any]] = field(default_factory=list)
    rungs: list[str] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    calls: list[Call] = field(default_factory=list)
    # Copilot's answers (one line per file it changed), one after another.
    explanation: str | None = None


@dataclass
class Copilot:
    """The client and its settings, and where the prompts are kept."""

    client: CopilotClient
    settings: CopilotSettings
    prompts_dir: Path
    cap: int = bundle.CAP


def _file(result: FileResult, **changes: Any) -> dict[str, Any]:
    data = {"path": result.path, "status": result.status, "rung": result.rung, "error": result.error, "notes": list(result.notes)}
    return {**data, **changes}


def run(
    scratch: Scratch,
    diffs: Sequence[str],
    context: Context,
    copilot: Copilot | None,
    budget: Budget,
    source_project: Path | None = None,
    present: Sequence[str] = (),
    target: Path | None = None,
    cancel: Callable[[], bool] = lambda: False,
    rungs: Collection[str] = COPILOT_RUNGS,
) -> LadderResult:
    """The ladder for `diffs` (the item's diffs for this project) in `scratch`. `copilot` None: rungs 1-2 only;
    `rungs`: the Copilot rungs allowed.

    `target`: the project in the user's tree, readable by Copilot.
    """
    first = scratch.rungs(diffs, source_project, present)
    # Rung 2 ran for a file it merged or tried (`source base: <why not>`), not one it skipped.
    source = any(f.rung == "source" or any(n.startswith("source base:") for n in f.notes) for f in first.files)
    out = LadderResult("failed", rungs=["stack", "source"] if source else ["stack"])
    out.files = [_file(f) for f in first.files]
    if first.level is not None:
        out.status, out.level, out.patch = "patched", first.level, first.patch
        return out
    conflicts = [f for f in first.files if f.status == "conflict"]
    rejected = [f for f in first.files if f.status == "rejected"]
    if failed := [f for f in first.files if f.status == "failed"]:
        out.reason = "; ".join(f"{f.path}: {f.error}" for f in failed) + " (needs re-implement)"
        return out
    steps: list[tuple[bundle.Rung, list[FileResult]]] = [("resolve", conflicts), ("port", rejected)]
    unselected = [step for step, files in steps if files and step not in rungs]
    if copilot is None or unselected:
        out.reason = "; ".join(f"{f.path}: {f.error or f.status}" for f in [*conflicts, *rejected])
        if copilot is not None:
            out.reason += f" ({', '.join(unselected)} not selected)"
        return out
    for path in {f.path for f in rejected}:
        (scratch.path / f"{path}.rej").unlink(missing_ok=True)

    call = _Caller(out, scratch, diffs, context, copilot, budget, target, cancel)
    rung: bundle.Rung = "port" if rejected else "resolve"
    for step, files in steps:
        if files:
            out.rungs.append(step)
            if stop := call(step, files, 0):
                out.reason = stop
                return out
    edited = [*conflicts, *rejected]
    if not _check_rounds(out, call, scratch, diffs, rung, edited):
        return out
    fixed = {f.path: ("resolved", "resolve") if f.status == "conflict" else ("ported", "port") for f in edited}
    for file in out.files:
        if file["path"] in fixed:
            file["status"], file["rung"] = fixed[file["path"]]
    return out


def fix_build(
    scratch: Scratch,
    diffs: Sequence[str],
    context: Context,
    copilot: Copilot,
    budget: Budget,
    previous: LadderResult,
    errors: Sequence[str],
    build_round: int,
    target: Path | None = None,
    cancel: Callable[[], bool] = lambda: False,
) -> LadderResult:
    """One build-fix round on a patched result: Copilot gets the build's error lines and the change so far for every
    file of the patch, then the checks run as after any call (with their own fix rounds).

    At the rung `previous` was made with; `port` for a patch from rungs 1-2. The result carries `previous`'s calls
    too; its level is the rung, its patch what git has now (the same as before when Copilot changed nothing).
    """
    assert previous.status == "patched" and previous.patch is not None
    rung = build_rung(previous)
    out = LadderResult("failed", level=previous.level, files=copy.deepcopy(previous.files), rungs=list(dict.fromkeys([*previous.rungs, rung])))
    out.calls, out.explanation, out.findings = list(previous.calls), previous.explanation, list(previous.findings)
    paths = checks.patch_paths(previous.patch)
    files = [FileResult(path, "applied") for path in paths]
    call = _Caller(out, scratch, diffs, context, copilot, budget, target, cancel, build_round)
    if stop := call(rung, files, 0, errors, _diff_of(scratch, paths), "build"):
        out.reason = stop
        return out
    if not _check_rounds(out, call, scratch, diffs, rung, files):
        return out
    for file in out.files:
        path = file["path"]
        if bundle.original_sections([out.patch or ""], [path]) != bundle.original_sections([previous.patch], [path]):
            file["rung"] = rung
            file["notes"] = [*file.get("notes", []), f"build fix {build_round}"]
    return out


def build_rung(result: LadderResult) -> bundle.Rung:
    """The rung a build fix of `result` runs at."""
    return "resolve" if result.level == "resolve" else "port"


class _Caller:
    """Makes the Copilot calls of one ladder run into `out`."""

    def __init__(
        self,
        out: LadderResult,
        scratch: Scratch,
        diffs: Sequence[str],
        context: Context,
        copilot: Copilot,
        budget: Budget,
        target: Path | None,
        cancel: Callable[[], bool],
        build_round: int = 0,
    ) -> None:
        self.out, self.scratch, self.diffs, self.context, self.copilot = out, scratch, diffs, context, copilot
        self.budget, self.target, self.cancel, self.build_round = budget, target, cancel, build_round

    def __call__(
        self,
        rung: bundle.Rung,
        files: Sequence[FileResult],
        fix_round: int,
        feedback: Sequence[str] = (),
        change: str | None = None,
        kind: bundle.Feedback = "checks",
    ) -> str | None:
        """One Copilot call; None when it ran, else why the attempt stops (`out.status` set on cancel)."""
        out, copilot = self.out, self.copilot
        if self.cancel():
            out.status = "canceled"
            return "canceled"
        if not self.budget.take():
            return BUDGET_REACHED
        prompt = bundle.build(rung, files, self.diffs, self.context, self.scratch.path, self.target, feedback, change, copilot.cap, kind)
        copilot.prompts_dir.mkdir(parents=True, exist_ok=True)
        prompt_path = copilot.prompts_dir / f"call-{len(list(copilot.prompts_dir.glob('call-*.txt'))) + 1}.txt"
        prompt_path.write_text(prompt.prompt)
        model = copilot.settings.models.get(rung)
        read_dirs = [self.target] if self.target is not None else []
        result = copilot.client.run(self.scratch.path, prompt.prompt, model, copilot.settings.timeout, read_dirs, self.cancel)
        out.calls.append(
            Call(
                rung,
                fix_round,
                model,
                result.status,
                str(prompt_path),
                prompt.summary(),
                result.output[:ANSWER_CAP],
                result.error,
                result.usage,
                result.duration,
                self.build_round,
            )
        )
        if result.status == "canceled":
            out.status = "canceled"
            return "canceled"
        if result.status != "ok":
            first_line = next(iter(result.error.splitlines()), "") or f"exit code {result.exit_code}"
            return f"Copilot {result.status}: {first_line}"
        if result.output:
            out.explanation = f"{out.explanation}\n\n{result.output}" if out.explanation else result.output
        return None


def _check_rounds(out: LadderResult, call: _Caller, scratch: Scratch, diffs: Sequence[str], rung: bundle.Rung, edited: Sequence[FileResult]) -> bool:
    """Checks on the patch, their errors back as fix rounds; True with `out` patched, else False with its reason."""
    paths = [f.path for f in edited]
    item_files = touched_files(diffs)
    rounds = 0
    previous: str | None = None
    while True:
        patch = scratch.patch()
        findings = checks.mechanical(scratch, patch, item_files)
        findings += intent.check(diffs, patch, {p: _read(scratch.path / p) for p in paths}, rung, paths)
        blocking = checks.errors(findings)
        if not blocking or patch == previous:
            break
        previous = patch
        rounds += 1
        stop = call(rung, edited, rounds, [f.text() for f in blocking], _diff_of(scratch, paths))
        if stop is None:
            continue
        failing = [f for f in blocking if f.kind == "check"]
        if stop == BUDGET_REACHED and not failing:
            # Only intent findings left: the patch goes to review with them.
            break
        out.reason = f"{stop}; {failing[0].text()}" if stop == BUDGET_REACHED and failing else stop
        out.findings = findings
        return False
    out.findings = findings
    failing = [f for f in checks.errors(findings) if f.kind == "check"]
    if failing:
        out.reason = failing[0].text()
        return False
    out.status, out.level, out.patch = "patched", rung, patch
    return True


def _read(file: Path) -> str | None:
    try:
        return file.read_text(errors="replace")
    except OSError:
        return None


def _diff_of(scratch: Scratch, paths: Sequence[str]) -> str:
    """The change so far against the base, for `paths` only."""
    patch = scratch.patch() or ""
    return bundle.original_sections([patch], paths)
