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
projects share it); a call it does not allow fails the attempt with "Copilot budget
reached".
"""

from collections.abc import Callable, Sequence
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
# Characters of Copilot's answer kept with a call.
ANSWER_CAP = 20_000


class Budget:
    """Copilot calls allowed, shared by everything that takes from it."""

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.used = 0

    def take(self) -> bool:
        if self.used >= self.limit:
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
) -> LadderResult:
    """The ladder for `diffs` (the item's diffs for this project) in `scratch`. `copilot` None: rungs 1-2 only.

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
    if copilot is None:
        out.reason = "; ".join(f"{f.path}: {f.error or f.status}" for f in [*conflicts, *rejected])
        return out
    for path in {f.path for f in rejected}:
        (scratch.path / f"{path}.rej").unlink(missing_ok=True)

    def call(rung: bundle.Rung, files: Sequence[FileResult], fix_round: int, feedback: Sequence[str] = (), change: str | None = None) -> str | None:
        """One Copilot call; None when it ran, else why the attempt stops (`out.status` set on cancel)."""
        if cancel():
            out.status = "canceled"
            return "canceled"
        if not budget.take():
            return BUDGET_REACHED
        prompt = bundle.build(rung, files, diffs, context, scratch.path, target, feedback, change, copilot.cap)
        copilot.prompts_dir.mkdir(parents=True, exist_ok=True)
        prompt_path = copilot.prompts_dir / f"call-{len(list(copilot.prompts_dir.glob('call-*.txt'))) + 1}.txt"
        prompt_path.write_text(prompt.prompt)
        model = copilot.settings.models.get(rung)
        result = copilot.client.run(scratch.path, prompt.prompt, model, copilot.settings.timeout, [target] if target is not None else [], cancel)
        out.calls.append(
            Call(rung, fix_round, model, result.status, str(prompt_path), prompt.summary(), result.output[:ANSWER_CAP], result.error, result.usage, result.duration)
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

    rung: bundle.Rung = "port" if rejected else "resolve"
    steps: list[tuple[bundle.Rung, list[FileResult]]] = [("resolve", conflicts), ("port", rejected)]
    for step, files in steps:
        if files:
            out.rungs.append(step)
            if stop := call(step, files, 0):
                out.reason = stop
                return out
    edited = [*conflicts, *rejected]
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
        out.reason = f"{stop}; {failing[0].text()}" if stop == BUDGET_REACHED else stop
        out.findings = findings
        return out
    out.findings = findings
    failing = [f for f in checks.errors(findings) if f.kind == "check"]
    if failing:
        out.reason = failing[0].text()
        return out
    fixed = {f.path: ("resolved", "resolve") if f.status == "conflict" else ("ported", "port") for f in edited}
    for file in out.files:
        if file["path"] in fixed:
            file["status"], file["rung"] = fixed[file["path"]]
    out.status, out.level, out.patch = "patched", rung, patch
    return out


def _read(file: Path) -> str | None:
    try:
        return file.read_text(errors="replace")
    except OSError:
        return None


def _diff_of(scratch: Scratch, paths: Sequence[str]) -> str:
    """The change so far against the base, for `paths` only."""
    patch = scratch.patch() or ""
    return bundle.original_sections([patch], paths)
