import json
from pathlib import Path

import pytest
from fake_copilot import FAKE_COPILOT
from fastapi import FastAPI
from repo_tree import RepoTree
from test_build_runner import fake_jira, item, workspace  # noqa: F401
from test_import import session
from test_prescan_api import app, client, target, workspace_id  # noqa: F401
from test_scratch import CORE, C, H, change, lines, scratch, series, tree  # noqa: F401
from test_tree import snapshot

from aosp_migrator.config import Config, CopilotSettings
from aosp_migrator.copilot.client import CopilotClient
from aosp_migrator.db.models import Item, MigrationPatch, ReworkAttempt
from aosp_migrator.prescan.chain import current_patches, patch_plan
from aosp_migrator.prescan.patch import Patch
from aosp_migrator.rework import attempt, bundle, checks, intent, ladder, locate
from aosp_migrator.rework.bundle import Context, Prerequisite
from aosp_migrator.rework.checks import Finding
from aosp_migrator.rework.ladder import BUDGET_REACHED, Budget, Copilot
from aosp_migrator.rework.scratch import FileResult, Region, Scratch, touched_files

# The conflict `series` leaves in C without its first patch, diff3 style.
REGION = "<<<<<<< ours\n||||||| base\nint b = 2;\n=======\nint b = 3;\n>>>>>>> theirs\n"


@pytest.fixture
def scenarios(tmp_path: Path) -> Path:
    directory = tmp_path / "scenarios"
    directory.mkdir()
    return directory


@pytest.fixture
def log(tmp_path: Path) -> Path:
    return tmp_path / "copilot.jsonl"


def settings(**models: str) -> CopilotSettings:
    return CopilotSettings("copilot", {"resolve": None, "port": None, "reimpl": None, **models}, 60, 5, 100, 2)


@pytest.fixture
def copilot(tmp_path: Path, scenarios: Path, log: Path) -> Copilot:
    client = CopilotClient(str(FAKE_COPILOT), env={"FAKE_COPILOT_SCENARIOS": str(scenarios), "FAKE_COPILOT_LOG": str(log)}, kill_grace=1)
    return Copilot(client, settings(resolve="cheap", port="strong"), tmp_path / "prompts")


def scenario(directory: Path, name: str, *rounds: dict) -> Context:
    """Write a fake Copilot scenario (one dict, or one per round); the ticket context that picks it."""
    (directory / f"{name}.json").write_text(json.dumps(rounds[0] if len(rounds) == 1 else {"rounds": list(rounds)}))
    return Context("PROJ-7", "Change b", f"Make b three.\n@@fake:{name}@@")


def prompts(log: Path) -> list[str]:
    return [json.loads(line)["prompt"] for line in log.read_text().splitlines()] if log.exists() else []


RESOLVE = {"reply": f"{C}: kept b = 3", "edits": [{"path": C, "old": REGION, "new": "int b = 3;\n"}]}


def test_resolve_markers(tree: RepoTree, tmp_path: Path, series: tuple[str, str], copilot: Copilot, scenarios: Path, log: Path) -> None:
    _, second = series
    before = snapshot(tree.root)
    s = scratch(tree, tmp_path, [second])
    result = ladder.run(s, [second], scenario(scenarios, "resolve", RESOLVE), copilot, Budget(5), target=tree.root / CORE)
    assert (result.status, result.level, result.rungs, result.reason) == ("patched", "resolve", ["stack", "resolve"], None)
    assert [(f["path"], f["status"], f["rung"]) for f in result.files] == [(C, "resolved", "resolve"), (H, "applied", "stack")]
    assert result.patch is not None and "+int b = 3;" in result.patch and "+int b();" in result.patch and "<<<" not in result.patch
    assert result.findings == [] and result.explanation == f"{C}: kept b = 3"
    (call,) = result.calls
    assert (call.rung, call.round, call.model, call.status, call.usage is not None) == ("resolve", 0, "cheap", "ok", True)
    (prompt,) = prompts(log)
    assert Path(call.prompt_path).read_text() == prompt
    # The ticket, the conflict region with line numbers, the original section; only the failing file to edit.
    assert "Ticket PROJ-7: Change b" in prompt and f"## {C}\nConflict 1, lines 3-8:" in prompt and "     5  int b = 2;" in prompt
    assert f"Files to edit:\n- {C}\n\n" in prompt and "+int b = 3;" in prompt
    # Copilot ran in the scratch checkout, the target project readable only.
    argv = json.loads(log.read_text().splitlines()[0])["argv"]
    assert json.loads(log.read_text().splitlines()[0])["cwd"] == str(s.path.resolve())
    assert f"write({(tree.root / CORE).resolve()}/**)" in argv and ["--model", "cheap"] == argv[-2:]
    assert snapshot(tree.root) == before


@pytest.fixture
def moved(tree: RepoTree) -> str:
    """A diff whose hunk does not apply here (its context differs and there is no base to merge on)."""
    other = RepoTree(tree.root.parent / "other")
    other.add_project(CORE, {C: lines("int a = 1;", "", "int z = 0;")})
    diff = change(other, CORE, {C: lines("int a = 1;", "", "int z = 1;")}, "change z")
    tree.commit(CORE, {C: lines("int a = 2;", "", "int z = 0;")}, "a is 2")
    return diff


def test_port_a_rej_hunk(tree: RepoTree, tmp_path: Path, moved: str, copilot: Copilot, scenarios: Path, log: Path) -> None:
    context = scenario(scenarios, "port", {"reply": "ported", "edits": [{"path": C, "old": "int z = 0;", "new": "int z = 1;"}]})
    s = scratch(tree, tmp_path, [moved])
    result = ladder.run(s, [moved], context, copilot, Budget(5))
    assert (result.status, result.level, result.rungs) == ("patched", "port", ["stack", "port"])
    assert result.files[0]["status"] == "ported" and not (s.path / f"{C}.rej").exists()
    assert result.patch is not None and "-int z = 0;\n+int z = 1;" in result.patch
    (prompt,) = prompts(log)
    assert "Hunk 1 that did not apply:" in prompt and "+int z = 1;" in prompt and "     2  int a = 2;\n" in prompt and result.calls[0].model == "strong"
    assert result.findings == []


def test_markers_left_fail_the_round_then_get_fixed(tree: RepoTree, tmp_path: Path, series: tuple[str, str], copilot: Copilot, scenarios: Path, log: Path) -> None:
    _, second = series
    context = scenario(scenarios, "fix", {"reply": "nothing to do"}, RESOLVE)
    result = ladder.run(scratch(tree, tmp_path, [second]), [second], context, copilot, Budget(5))
    assert (result.status, result.level, [(c.rung, c.round) for c in result.calls]) == ("patched", "resolve", [("resolve", 0), ("resolve", 1)])
    fix = prompts(log)[1]
    assert f"## Checks your previous edit failed\n- {C}: conflict markers left (line 3, 4, 8)\n" in fix
    assert "previous edit did not pass the checks" in fix and "## Your change so far" in fix


def test_markers_still_left_fail_the_attempt(tree: RepoTree, tmp_path: Path, series: tuple[str, str], copilot: Copilot, scenarios: Path) -> None:
    _, second = series
    result = ladder.run(scratch(tree, tmp_path, [second]), [second], scenario(scenarios, "stuck", {"reply": "nothing"}), copilot, Budget(5))
    # The fix round changed nothing: no more rounds.
    assert (result.status, result.level, result.patch, len(result.calls)) == ("failed", None, None, 2)
    assert result.reason == f"{C}: conflict markers left (line 3, 4, 8)"


def test_dropped_hunk_gets_an_intent_round_then_is_flagged(tree: RepoTree, tmp_path: Path, series: tuple[str, str], copilot: Copilot, scenarios: Path, log: Path) -> None:
    _, second = series
    # Copilot keeps the target's side: the markers go, the change with them.
    context = scenario(scenarios, "drop", {"edits": [{"path": C, "old": REGION, "new": ""}]})
    result = ladder.run(scratch(tree, tmp_path, [second]), [second], context, copilot, Budget(5))
    assert (result.status, result.level, len(result.calls)) == ("patched", "resolve", 2)
    assert f"- {C}: hunk 1 of the original missing\n" in prompts(log)[1]
    assert Finding("intent", "error", C, "hunk 1 of the original missing") in result.findings
    assert Finding("intent", "error", C, "1 of 1 added lines missing: `int b = 3;`") in result.findings


def test_extra_file_is_flagged(tree: RepoTree, tmp_path: Path, series: tuple[str, str], copilot: Copilot, scenarios: Path) -> None:
    _, second = series
    context = scenario(scenarios, "extra", {**RESOLVE, "creates": [{"path": "init/extra.cpp", "text": "int extra;\n"}]})
    result = ladder.run(scratch(tree, tmp_path, [second]), [second], context, copilot, Budget(5))
    assert (result.status, result.findings) == ("patched", [Finding("check", "warning", "init/extra.cpp", "not a file of the item's diffs")])
    # Outside the sparse checkout and still in the patch.
    assert result.patch is not None and "+int extra;" in result.patch


def test_item_budget(tree: RepoTree, tmp_path: Path, series: tuple[str, str], copilot: Copilot, scenarios: Path, log: Path) -> None:
    _, second = series
    context = scenario(scenarios, "budget", {"reply": "nothing"}, RESOLVE)
    budget = Budget(1)
    result = ladder.run(scratch(tree, tmp_path, [second]), [second], context, copilot, budget)
    assert (result.status, len(result.calls), budget.used) == ("failed", 1, 1)
    assert result.reason == f"{BUDGET_REACHED}; {C}: conflict markers left (line 3, 4, 8)"
    # Spent before the first call: no call at all.
    result = ladder.run(scratch(tree, tmp_path / "again", [second]), [second], context, copilot, budget)
    assert (result.status, result.reason, len(prompts(log))) == ("failed", BUDGET_REACHED, 1)


def test_intent_findings_left_when_the_budget_ends(tree: RepoTree, tmp_path: Path, series: tuple[str, str], copilot: Copilot, scenarios: Path) -> None:
    _, second = series
    context = scenario(scenarios, "drop-budget", {"edits": [{"path": C, "old": REGION, "new": ""}]})
    result = ladder.run(scratch(tree, tmp_path, [second]), [second], context, copilot, Budget(1))
    # No round for the intent errors: the patch goes to review with them.
    assert (result.status, result.level, len(result.calls)) == ("patched", "resolve", 1)
    assert Finding("intent", "error", C, "hunk 1 of the original missing") in result.findings


def test_without_copilot_or_with_a_failing_call(tree: RepoTree, tmp_path: Path, series: tuple[str, str], copilot: Copilot, scenarios: Path) -> None:
    first, second = series
    result = ladder.run(scratch(tree, tmp_path / "a", [second]), [second], Context("K", "s", ""), None, Budget(5))
    assert (result.status, result.reason, result.calls) == ("failed", f"{C}: 3-way merge conflicts", [])
    # Rungs 1-2 enough: no Copilot.
    result = ladder.run(scratch(tree, tmp_path / "b", [second], [first]), [second], Context("K", "s", ""), copilot, Budget(5))
    assert (result.status, result.level, result.calls) == ("patched", "stacked", [])
    context = scenario(scenarios, "broken", {"exit": 1, "stderr": "model unavailable\n"})
    result = ladder.run(scratch(tree, tmp_path / "c", [second]), [second], context, copilot, Budget(5))
    assert (result.status, result.reason, result.calls[0].status) == ("failed", "Copilot failed: model unavailable", "failed")
    # Not logged in: the reason says the token is missing from the backend's environment.
    context = scenario(scenarios, "no-auth", {"exit": 1, "stderr": "Error: No authentication information found.\n"})
    result = ladder.run(scratch(tree, tmp_path / "e", [second]), [second], context, copilot, Budget(5))
    assert result.reason.startswith("Copilot failed: Error: No authentication information found. COPILOT_GITHUB_TOKEN is not set")
    result = ladder.run(scratch(tree, tmp_path / "d", [second]), [second], context, copilot, Budget(5), cancel=lambda: True)
    assert (result.status, result.calls) == ("canceled", [])


def test_bundle_cut_at_the_cap_keeps_priority_order(tmp_path: Path) -> None:
    (tmp_path / "f.c").write_text("".join(f"line {n}\n" for n in range(1, 40)))
    files = [FileResult("f.c", "conflict", "stack", regions=[Region(18, 22, "", None, "")])]
    original = "diff --git a/f.c b/f.c\n--- a/f.c\n+++ b/f.c\n@@ -1 +1 @@\n-line 1\n+line one\n"
    context = Context("K-1", "Summary", "Description " * 10, note="keep the old name", previous_reason="hunk 2 missing", prerequisites=[Prerequisite("K-0", "Before", original, None)])
    whole = bundle.build("resolve", files, [original], context, tmp_path, feedback=["f.c: conflict markers left"])
    assert whole.summary() == {"feedback": "whole", "ticket": "whole", "history": "whole", "files": "whole", "original": "whole", "prerequisites": "whole"}
    headings = ["## Checks your previous edit failed", "## Ticket K-1", "## Earlier attempts", "## f.c", "## The original change", "## Changes this one builds on"]
    positions = [whole.prompt.index(h) for h in headings]
    assert positions == sorted(positions)
    assert "The previous attempt failed: hunk 2 missing" in whole.prompt and "rejected the last patch: keep the old name" in whole.prompt
    assert "     8  line 8\n" in whole.prompt and "    32  line 32\n" in whole.prompt and "     7  line 7\n" not in whole.prompt

    cap = sum(len(p.text) for p in whole.parts[:3]) + 1 + 50
    cut = bundle.build("resolve", files, [original], context, tmp_path, feedback=["f.c: conflict markers left"], cap=cap)
    assert cut.summary() == {"feedback": "whole", "ticket": "whole", "history": "whole", "files": "cut", "original": "left out", "prerequisites": "left out"}
    assert bundle.CUT in cut.prompt and "## The original change" not in cut.prompt
    # Instructions and the closing request are never cut.
    assert cut.prompt.startswith("A 3-way merge") is False and "Files to edit:\n- f.c" in cut.prompt and cut.prompt.endswith(bundle.CLOSING)


def section(path: str, old: str, new: str) -> str:
    return f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n@@ -1 +1 @@\n-{old}\n+{new}\n"


def test_prerequisite_delta_only_for_shared_files(tmp_path: Path) -> None:
    (tmp_path / "a.c").write_text("x\n")
    files = [FileResult("a.c", "rejected", hunks=["@@ -1 +1 @@\n-x\n+y\n"])]
    shared = Prerequisite("K-1", "Renames foo", section("a.c", "foo()", "bar()") + section("b.c", "b1", "b2"), section("a.c", "foo()", "baz()") + section("b.c", "b1", "b2"))
    unchanged = Prerequisite("K-2", "Same", section("a.c", "p", "q"), section("a.c", "p", "q"))
    elsewhere = Prerequisite("K-3", "Other file", section("c.c", "c", "d"), None)
    pending = Prerequisite("K-4", "Not yet", section("a.c", "m", "n"), None)
    prompt = bundle.build("port", files, [section("a.c", "x", "y")], Context("K-5", "s", "d", prerequisites=[shared, unchanged, elsewhere, pending]), tmp_path).prompt
    part = prompt[prompt.index("## Changes this one builds on") :]
    assert "### K-1: Renames foo\nOriginal:" in part and "+bar()" in part and "+baz()" in part and "b.c" not in part
    assert "### K-2: Same\nMigrated unchanged" in part and "K-3" not in part
    assert "### K-4: Not yet\nNot migrated yet." in part and "+n" in part


ORIGINAL = (
    "diff --git a/f.c b/f.c\n--- a/f.c\n+++ b/f.c\n"
    '@@ -1,3 +1,3 @@\n a\n-int count = 0;\n+int count = LIMIT;\n cleanup();\n'
    '@@ -10,3 +10,4 @@\n j\n+log("ready");\n k\n l\n'
)


def test_intent_thresholds_per_rung() -> None:
    patch = "diff --git a/f.c b/f.c\n--- a/f.c\n+++ b/f.c\n@@ -1,3 +1,3 @@\n a\n-int count = 5;\n+int count = LIMIT;\n cleanup();\n"
    after = {"f.c": "a\nint count = LIMIT;\ncleanup();\nj\nk\nl\n"}
    # The second hunk is gone: its line and its string missing.
    resolve = intent.check([ORIGINAL], patch, after, "resolve")
    assert [(f.severity, f.message) for f in resolve] == [
        ("error", '1 of 2 added lines missing: `log("ready");`'),
        ("error", 'identifiers or strings the original adds missing: `"ready"`, `log`'),
        ("error", "hunk 2 of the original missing"),
    ]
    assert {f.severity for f in intent.check([ORIGINAL], patch, after, "reimpl")} == {"warning"}
    # Port allows a few removals the original did not make; resolve none. `int count = 5;` is a variant of a removed line.
    extra = patch.replace(" cleanup();\n", "-cleanup();\n").replace("@@ -1,3 +1,3 @@", "@@ -1,3 +1,2 @@")
    whole = {"f.c": 'a\nint count = LIMIT;\nj\nlog("ready");\nk\nl\n'}
    assert [(f.severity, f.message) for f in intent.check([ORIGINAL], extra, whole, "resolve")] == [("error", "removes lines the original does not: `cleanup();`")]
    assert [(f.severity, f.message) for f in intent.check([ORIGINAL], extra, whole, "port")] == [("warning", "removes lines the original does not: `cleanup();`")]
    assert intent.check([ORIGINAL], patch, {"f.c": whole["f.c"].replace("LIMIT;\n", "LIMIT;\ncleanup();\n")}, "resolve") == []
    # Limited to the files a rework changed.
    assert intent.check([ORIGINAL], None, {}, "resolve", paths=["g.c"]) == []


def test_mechanical_checks(tree: RepoTree, tmp_path: Path, series: tuple[str, str]) -> None:
    _, second = series
    s = scratch(tree, tmp_path, [second])
    s.rungs([second])
    (s.path / "init/x.rej").write_text("@@\n")
    patch = s.patch()
    found = checks.mechanical(s, patch, [C, H])
    assert Finding("check", "error", "init/x.rej", "rejected hunks file left") in found
    assert Finding("check", "error", C, "conflict markers left (line 3, 4, 8)") in found
    assert checks.apply_check(s, patch or "") is None
    assert checks.apply_check(s, (patch or "").replace("int a = 1;", "int a = 9;")) is not None
    (s.path / "init/x.rej").unlink()
    (s.path / C).write_text(lines("int a = 1;"))
    (s.path / H).write_text("// h\n")
    assert checks.mechanical(s, s.patch(), [C, H]) == [Finding("check", "error", None, "no change against the base")]


def test_attempt_record_patch_and_next_context(app: FastAPI, workspace: int, config: Config) -> None:
    base, dependent = item(app, "PROJ-105"), item(app, "PROJ-106")
    with session(app) as s:
        first = attempt.start(s, workspace, [dependent.id], {CORE: "abc"})
        failed = ladder.LadderResult("failed", reason="init.cpp: conflict markers left", rungs=["stack", "resolve"], files=[{"path": "init.cpp", "status": "conflict"}])
        failed.calls = [ladder.Call("resolve", 0, "cheap", "ok", "/p/call-1.txt", {"ticket": "whole"}, "answer", "", {"totalPremiumRequestCost": 1}, 1.5)]
        attempt.record(s, first, CORE, failed)
        attempt.finish(s, first, "failed", failed.reason)
        row = s.get(Item, dependent.id)
        assert row is not None and (row.rework_status, row.rework_reason, row.reworked_at is not None) == ("rework_failed", failed.reason, True)
        stored = s.get(ReworkAttempt, first.id)
        assert stored is not None and stored.files == [{"project": CORE, "path": "init.cpp", "status": "conflict"}]
        assert stored.calls[0]["project"] == CORE and stored.calls[0]["usage"] == {"totalPremiumRequestCost": 1} and stored.rungs == ["stack", "resolve"]

        # The next attempt starts from the failure; the prerequisite shows with its migrated patch.
        prerequisite = s.get(Item, base.id)
        assert prerequisite is not None and row is not None
        context = attempt.context(s, row, CORE, [prerequisite])
        assert (context.key, context.previous_reason, context.note) == ("PROJ-106", failed.reason, None)
        (pre,) = context.prerequisites
        (base_patch,) = current_patches(s, prerequisite)
        assert (pre.key, pre.migrated) == ("PROJ-105", base_patch.diff) and pre.original

        second = attempt.start(s, workspace, [dependent.id], {CORE: "abc"}, [base_patch.id])
        patched = ladder.LadderResult("patched", level="resolve", patch="diff --git a/x b/x\n", findings=[Finding("intent", "warning", "x", "m")])
        patch = attempt.save_patch(s, second, dependent.id, CORE, patched, "abc", [base_patch.id])
        attempt.finish(s, second, "patched")
        assert (patch.source, patch.level, patch.review, patch.attempt_id, patch.base_patch_ids) == ("ai", "resolve", "proposed", second.id, [base_patch.id])
        assert patch.findings == [{"kind": "intent", "severity": "warning", "path": "x", "message": "m"}]
        assert (row.rework_status, row.rework_reason) == ("proposed", None)
        assert attempt.last_failure(s, row) is None
        # The build applies the proposed patch in place of the item's original.
        assert patch in patch_plan(s, row)

        # Rejected with a note: the next bundle carries it.
        patch.review, patch.review_note = "rejected", "keep the old API"
        s.commit()
        assert attempt.context(s, row, CORE, [prerequisite]).note == "keep the old API"
        stacked = attempt.save_patch(s, second, dependent.id, CORE, ladder.LadderResult("patched", level="stacked", patch="d"), "abc", [])
        assert (stacked.source, stacked.review) == ("original", "proposed")
        canceled = attempt.start(s, workspace, [dependent.id], {})
        attempt.finish(s, canceled, "canceled")
        assert row.rework_status == "proposed"
        assert s.query(MigrationPatch).filter(MigrationPatch.attempt_id == second.id).count() == 2



# A file long enough to be found by content: lines of at least `locate.PROBE_MIN` characters.
BODY = "".join(f"static int value_{n} = compute_value({n});\n" for n in range(8))
MOVED = "init/moved/init_main.cpp"


def reimpl_setup(tmp_path: Path, target_files: dict[str, str]) -> tuple[RepoTree, RepoTree, str]:
    src = RepoTree(tmp_path / "src")
    src.add_project(CORE, {C: BODY + "int b = 2;\n", "README": "core\n"})
    diff = change(src, CORE, {C: BODY + "int b = 3;\n"}, "change b")
    target = RepoTree(tmp_path / "aosp")
    target.add_project(CORE, {"README": "core\n", **target_files})
    return src, target, diff


def test_reimplement_a_moved_file(tmp_path: Path, copilot: Copilot, scenarios: Path, log: Path) -> None:
    # Moved and changed a little: found by content, not by name.
    src, target, diff = reimpl_setup(tmp_path, {MOVED: "// moved here\n" + BODY.replace("value_7", "value_seven") + "int b = 2;\n"})
    before = snapshot(target.root)
    edit = {"reply": f"{MOVED}: b is three", "edits": [{"path": MOVED, "old": "int b = 2;", "new": "int b = 3;"}]}
    context = scenario(scenarios, "moved", edit)
    s = Scratch.create(tmp_path / "rework" / "1" / CORE, target.root / CORE, touched_files([diff]))
    # Without re-implement selected: no call.
    result = ladder.run(s, [diff], context, copilot, Budget(5), src.root / CORE)
    assert (result.status, result.calls) == ("failed", []) and result.reason == f"{C}: {C}: No such file or directory {ladder.NEEDS_REIMPL}"

    result = ladder.run(s, [diff], context, copilot, Budget(5), src.root / CORE, rungs=ladder.COPILOT_RUNGS)
    assert (result.status, result.level, result.rungs, result.moved) == ("patched", "reimpl", ["stack", "reimpl"], {C: MOVED})
    (file,) = result.files
    assert (file["status"], file["rung"], file["notes"]) == ("reimplemented", "reimpl", [f"moved to {MOVED} (found by content: 7 of 8 lines)"])
    assert result.patch is not None and f"+++ b/{MOVED}" in result.patch and "-int b = 2;\n+int b = 3;" in result.patch
    # Flagged for review; the moved file is not "another file".
    assert result.findings == [Finding("intent", "warning", C, f"re-implemented from intent, moved to {MOVED} (found by content: 7 of 8 lines): review closely")]
    (prompt,) = prompts(log)
    assert f"Files to edit:\n- {MOVED}\n\n" in prompt and f"In this tree it moved to {MOVED}" in prompt
    # The source tree's before and after, from the source project's objects.
    assert f"## In the source tree, before and after the change\n### {C}\nBefore:\n```\n{BODY}int b = 2;\n```\nAfter:\n```\n{BODY}int b = 3;\n```" in prompt
    assert result.calls[0].model is None and snapshot(target.root) == before


def test_reimplement_a_file_gone(tmp_path: Path, copilot: Copilot, scenarios: Path, log: Path) -> None:
    src, target, diff = reimpl_setup(tmp_path, {"init/other.cpp": "int unrelated_value_here = 0;\n"})
    context = scenario(scenarios, "gone", {"reply": f"{C}: recreated", "creates": [{"path": C, "text": "int b = 3;\n"}]})
    s = Scratch.create(tmp_path / "rework" / "1" / CORE, target.root / CORE, touched_files([diff]))
    result = ladder.run(s, [diff], context, copilot, Budget(5), None, rungs=ladder.COPILOT_RUNGS)
    assert (result.status, result.level, result.moved) == ("patched", "reimpl", {})
    assert result.files[0]["notes"] == ["not in the target: gone"]
    assert Finding("intent", "warning", C, "re-implemented from intent, not in the target: gone: review closely") in result.findings
    # Intent findings are warnings only.
    assert all(f.severity == "warning" for f in result.findings)
    (prompt,) = prompts(log)
    assert "it is gone" in prompt and f"Files to edit:\n- {C}\n" in prompt
    # No source tree: before/after unknown.
    assert "## In the source tree" not in prompt


def test_locate_by_name_and_at_its_path(tmp_path: Path) -> None:
    _, target, diff = reimpl_setup(tmp_path, {"other/dir/init.cpp": "int nothing_alike = 0;\n", "init/old/init.cpp": "int also_unlike = 1;\n"})
    s = Scratch.create(tmp_path / "rework" / "1" / CORE, target.root / CORE, touched_files([diff]))
    (section,) = Patch.parse(diff).files
    where = locate.locate(s, section)
    # Same name, more trailing directories in common; no source tree for the blobs.
    assert (where.found, where.how, where.before, where.after) == ("init/old/init.cpp", "name", None, None)
    assert (s.path / "init/old/init.cpp").read_text() == "int also_unlike = 1;\n" and s.patch() is None
    target.commit(CORE, {C: "rewritten\n"}, "rewrite")
    s = Scratch.create(tmp_path / "rework" / "2" / CORE, target.root / CORE, touched_files([diff]))
    assert (locate.locate(s, section).found, locate.locate(s, section).how) == (C, "path")


def test_binary_without_data_is_not_reimplemented(tree: RepoTree, tmp_path: Path, copilot: Copilot) -> None:
    binary = "diff --git a/res/icon.png b/res/icon.png\nindex 1111111..2222222 100644\nBinary files a/res/icon.png and b/res/icon.png differ\n"
    s = scratch(tree, tmp_path, [binary])
    result = ladder.run(s, [binary], Context("K", "s", ""), copilot, Budget(5), rungs=ladder.COPILOT_RUNGS)
    assert (result.status, result.calls) == ("failed", []) and ladder.NEEDS_REIMPL not in (result.reason or "")
