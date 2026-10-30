from pathlib import Path

import pytest
from repo_tree import RepoTree
from test_apply import snapshot

from aosp_migrator.prescan.apply import MISSING_BLOB
from aosp_migrator.rework.scratch import Region, Scratch, ScratchError, read_regions, sparse_pattern, touched_files

CORE = "system/core"
C = "init/init.cpp"
H = "init/init.h"
OTHER = "init/other.cpp"


def lines(*body: str) -> str:
    return "\n".join(["// head", *body, *(f"// tail {n}" for n in range(6))]) + "\n"


@pytest.fixture
def tree(tmp_path: Path) -> RepoTree:
    tree = RepoTree(tmp_path / "aosp")
    tree.add_project(CORE, {C: lines("int a = 1;"), H: "// h\n", OTHER: "// other\n", "README": "core\n"})
    return tree


def change(tree: RepoTree, project: str, files: dict[str, str | None], subject: str) -> str:
    """Patch of one commit on top of the current HEAD; the commit stays."""
    return tree.format_patch(project, tree.commit(project, files, subject))


@pytest.fixture
def series(tree: RepoTree) -> tuple[str, str]:
    """`first` adds a line, `second` changes it: without `first`, `second` only conflicts. The project is back at the start."""
    first = change(tree, CORE, {C: lines("int a = 1;", "int b = 2;")}, "add b")
    second = change(tree, CORE, {C: lines("int a = 1;", "int b = 3;"), H: "// h\nint b();\n"}, "change b")
    tree.git(CORE, "reset", "-q", "--hard", "HEAD~2")
    return first, second


def scratch(tree: RepoTree, tmp_path: Path, diffs: list[str], bases: list[str] | None = None) -> Scratch:
    return Scratch.create(tmp_path / "rework" / "1" / CORE, tree.root / CORE, touched_files(diffs), bases or [])


def test_only_touched_files_and_the_user_tree_unchanged(tree: RepoTree, tmp_path: Path, series: tuple[str, str]) -> None:
    first, second = series
    before = snapshot(tree.root)
    s = scratch(tree, tmp_path, [second], [first])
    assert s.checked_out() == [C, H]
    assert s.head == tree.git(CORE, "rev-parse", "HEAD").strip() != s.base
    # The base patch is committed: the checked-out file has it.
    assert (s.path / C).read_text() == lines("int a = 1;", "int b = 2;")
    result = s.rungs([second])
    assert (result.level, [(f.path, f.status) for f in result.files]) == ("stacked", [(C, "applied"), (H, "applied")])
    # The project's tree, index and object store are untouched; no objects were written there.
    assert snapshot(tree.root) == before
    # The patch is the item's change alone, on HEAD plus the base.
    assert result.patch is not None and "-int b = 2;\n+int b = 3;" in result.patch and "+int b();" in result.patch
    tree.git(CORE, "apply", "-", input=first)
    tree.git(CORE, "apply", "-", input=result.patch)
    assert (tree.root / CORE / C).read_text() == lines("int a = 1;", "int b = 3;")
    s.remove()
    assert not s.path.exists()


def test_conflicts_without_the_prerequisite(tree: RepoTree, tmp_path: Path, series: tuple[str, str]) -> None:
    _, second = series
    s = scratch(tree, tmp_path, [second])
    result = s.rungs([second])
    assert (result.level, result.patch) == (None, None)
    conflict, header = result.files
    assert (header.path, header.status) == (H, "applied")
    assert (conflict.path, conflict.status, conflict.rung) == (C, "conflict", "stack")
    # diff3 markers left in the file: target (ours), the patch's preimage (base), the patch (theirs).
    assert conflict.regions == [Region(start=3, end=8, ours="", base="int b = 2;\n", theirs="int b = 3;\n")]
    assert read_regions(s.path / C) == conflict.regions
    assert "<<<<<<< ours\n" in (s.path / C).read_text()


def test_base_patch_that_does_not_apply(tree: RepoTree, tmp_path: Path, series: tuple[str, str]) -> None:
    _, second = series
    with pytest.raises(ScratchError, match="base patch 1 does not apply"):
        scratch(tree, tmp_path, [second], [second.replace("int b = 2;", "int b = 9;")])
    assert not (tmp_path / "rework" / "1" / CORE).exists()


@pytest.fixture
def oem(tree: RepoTree, tmp_path: Path) -> tuple[RepoTree, str]:
    """A source tree whose system/core has an OEM line the target lacks; the item's diff changes a line next to it."""
    source = RepoTree(tmp_path / "source")
    source.add_project(CORE, {C: lines("int a = 1;"), H: "// h\n"})
    source.commit(CORE, {C: lines("int a = 1;", "// oem", "", "int z = 0;")}, "OEM line")
    diff = change(source, CORE, {C: lines("int a = 1;", "// oem", "", "int z = 1;")}, "change z")
    tree.commit(CORE, {C: lines("int a = 1;", "", "int z = 0;")}, "z")
    return source, diff


def test_missing_blob_merges_on_the_source_base(tree: RepoTree, tmp_path: Path, oem: tuple[RepoTree, str]) -> None:
    source, diff = oem
    before = snapshot(source.root)
    s = scratch(tree, tmp_path, [diff])
    result = s.rungs([diff], source.root / CORE)
    assert (result.level, [(f.path, f.status, f.rung) for f in result.files]) == ("source3way", [(C, "merged", "source")])
    assert result.patch is not None and "-int z = 0;\n+int z = 1;" in result.patch and "oem" not in result.patch
    assert snapshot(source.root) == before


def test_source_project_at_another_path_is_skipped(tree: RepoTree, tmp_path: Path, oem: tuple[RepoTree, str]) -> None:
    source, diff = oem
    s = scratch(tree, tmp_path, [diff])
    # Not at the target project's path in the source tree: no source base; the hunk goes to a `.rej`.
    result = s.rungs([diff], source.root / "system/core-oem")
    (file,) = result.files
    assert (result.level, file.status, file.error, file.notes) == (None, "rejected", MISSING_BLOB, ["source base skipped: no source project at core-oem"])
    assert len(file.hunks) == 1 and file.hunks[0].startswith("@@ ") and "+int z = 1;\n" in file.hunks[0]
    assert (s.path / f"{C}.rej").is_file()
    assert scratch_rejects_without_source(tree, tmp_path, diff) == ["source base skipped: no source tree"]


def scratch_rejects_without_source(tree: RepoTree, tmp_path: Path, diff: str) -> list[str]:
    s = Scratch.create(tmp_path / "rework" / "2" / CORE, tree.root / CORE, touched_files([diff]))
    (file,) = s.rungs([diff]).files
    assert file.status == "rejected"
    return file.notes


def test_rejected_hunks_and_the_ones_that_applied(tree: RepoTree, tmp_path: Path) -> None:
    long = "\n".join(f"// line {n}" for n in range(40)) + "\n"
    tree.commit(CORE, {OTHER: long}, "long")
    diff = change(tree, CORE, {OTHER: long.replace("// line 2\n", "// line two\n").replace("// line 30\n", "// line thirty\n")}, "two hunks")
    # A target that moved on under the second hunk only, in a separate repository: no blob for a 3-way merge.
    elsewhere = RepoTree(tmp_path / "elsewhere")
    elsewhere.add_project(CORE, {OTHER: long.replace("// line 30\n", "// line 30 (target)\n")})
    s = Scratch.create(tmp_path / "rework" / "3" / CORE, elsewhere.root / CORE, touched_files([diff]))
    (file,) = s.rungs([diff]).files
    assert (file.status, file.error) == ("rejected", MISSING_BLOB)
    assert [h.splitlines()[0] for h in file.hunks] == ["@@ -28,7 +28,7 @@"]
    assert "-// line 30\n+// line thirty\n" in file.hunks[0]
    # The first hunk is in the file.
    assert "// line two\n" in (s.path / OTHER).read_text()


def test_present_files_are_left_out(tree: RepoTree, tmp_path: Path, series: tuple[str, str]) -> None:
    first, second = series
    s = scratch(tree, tmp_path, [second], [first])
    result = s.rungs([second], present=[H])
    assert [(f.path, f.status) for f in result.files] == [(C, "applied")]
    assert result.patch is not None and H not in result.patch


def test_read_regions_merge_style(tmp_path: Path) -> None:
    file = tmp_path / "f"
    file.write_text("a\n<<<<<<< ours\nx\n=======\ny\nz\n>>>>>>> theirs\nb\n<<<<<<< HEAD\n=======\nw\n>>>>>>> patch\n")
    assert read_regions(file) == [Region(2, 7, "x\n", None, "y\nz\n"), Region(9, 12, "", None, "w\n")]


def test_sparse_pattern_is_literal() -> None:
    assert sparse_pattern("res/a[1]*?.txt") == r"/res/a\[1]\*\?.txt"
