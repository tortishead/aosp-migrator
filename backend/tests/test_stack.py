from pathlib import Path

import pytest
from repo_tree import RepoTree
from test_apply import snapshot

from aosp_migrator.prescan.stack import NOT_MAPPED, Stack, StackDiff

CORE = "system/core"
AV = "frameworks/av"
C = "init/init.cpp"
README = "README"


def lines(*body: str) -> str:
    return "\n".join(["// head", *body, *(f"// tail {n}" for n in range(6))]) + "\n"


@pytest.fixture
def tree(tmp_path: Path) -> RepoTree:
    tree = RepoTree(tmp_path / "aosp")
    tree.add_project(CORE, {C: lines("int a = 1;")})
    tree.add_project(AV, {README: "camera\n"})
    return tree


def change(tree: RepoTree, project: str, files: dict[str, str | None], subject: str) -> str:
    """Patch of one commit on top of the current HEAD; the commit stays."""
    sha = tree.commit(project, files, subject)
    return tree.format_patch(project, sha)


@pytest.fixture
def series(tree: RepoTree) -> tuple[str, str]:
    """Patch `first` adds a line, `second` changes that line; the project is back at the start."""
    first = change(tree, CORE, {C: lines("int a = 1;", "int b = 2;")}, "add b")
    second = change(tree, CORE, {C: lines("int a = 1;", "int b = 3;")}, "change b")
    tree.git(CORE, "reset", "-q", "--hard", "HEAD~2")
    return first, second


def stack(tree: RepoTree, tmp_path: Path) -> Stack:
    (tmp_path / "scratch").mkdir()
    return Stack(tree.root, tmp_path / "scratch")


def test_second_applies_on_the_first(tree: RepoTree, tmp_path: Path, series: tuple[str, str]) -> None:
    first, second = series
    before = snapshot(tree.root)
    s = stack(tree, tmp_path)
    a = s.add([StackDiff("a.patch", CORE, first)])
    b = s.add([StackDiff("b.patch", CORE, second)])
    assert (a.applied, [d.status for d in a.diffs]) == (True, ["applies"])
    assert (b.applied, [d.status for d in b.diffs]) == (True, ["applies"])
    assert s.heads == {CORE: tree.git(CORE, "rev-parse", "HEAD").strip()}
    # The user's tree, index and objects are untouched.
    assert snapshot(tree.root) == before
    # The stacked patches apply in order on HEAD.
    for patch in (a.diffs[0].patch, b.diffs[0].patch):
        assert patch is not None
        tree.git(CORE, "apply", "-", input=patch)
    assert (tree.root / CORE / C).read_text() == lines("int a = 1;", "int b = 3;")


def test_out_of_order_item_is_left_out(tree: RepoTree, tmp_path: Path, series: tuple[str, str]) -> None:
    first, second = series
    s = stack(tree, tmp_path)
    b = s.add([StackDiff("b.patch", CORE, second)])
    assert (b.applied, b.diffs[0].status, b.diffs[0].patch) == (False, "conflicts", None)
    assert b.diffs[0].error
    # The stack goes on without it.
    a = s.add([StackDiff("a.patch", CORE, first)])
    assert (a.applied, a.diffs[0].status) == (True, "applies")


def test_item_is_stacked_whole_or_not_at_all(tree: RepoTree, tmp_path: Path, series: tuple[str, str]) -> None:
    first, second = series
    readme = change(tree, AV, {README: "camera\nfix\n"}, "readme")
    tree.git(AV, "reset", "-q", "--hard", "HEAD~1")
    s = stack(tree, tmp_path)
    mixed = s.add([StackDiff("readme.patch", AV, readme), StackDiff("b.patch", CORE, second), StackDiff("x.patch", None, readme)])
    assert mixed.applied is False
    assert [(d.status, d.error) for d in mixed.diffs][::2] == [("applies", None), ("unmapped", NOT_MAPPED)]
    assert mixed.diffs[1].status == "conflicts"
    assert all(d.patch is None for d in mixed.diffs)
    # The readme change was taken back out: it applies again, as is.
    again = s.add([StackDiff("readme.patch", AV, readme), StackDiff("a.patch", CORE, first)])
    assert (again.applied, [d.status for d in again.diffs]) == (True, ["applies", "applies"])


def test_present_files_are_left_out(tree: RepoTree, tmp_path: Path, series: tuple[str, str]) -> None:
    first, _ = series
    s = stack(tree, tmp_path)
    item = s.add([StackDiff("a.patch", CORE, first, present=[C])])
    assert (item.applied, item.diffs[0].status, item.diffs[0].patch) == (True, "present", None)


def test_three_way_on_the_stack(tree: RepoTree, tmp_path: Path) -> None:
    first = change(tree, CORE, {C: lines("int a = 1;", "// m", "int b = 2;")}, "add b")
    # The second patch was made where a line in its context reads differently: no plain apply, a clean merge.
    tree.commit(CORE, {C: lines("int a = 1; // one", "// m", "int b = 2;")}, "reword a")
    second = change(tree, CORE, {C: lines("int a = 1; // one", "// m", "int b = 3;")}, "change b")
    tree.git(CORE, "reset", "-q", "--hard", "HEAD~3")
    s = stack(tree, tmp_path)
    assert s.add([StackDiff("a.patch", CORE, first)]).applied
    b = s.add([StackDiff("b.patch", CORE, second)])
    assert (b.applied, b.diffs[0].status) == (True, "3way")
    patch = b.diffs[0].patch
    assert patch is not None and "-int b = 2;\n+int b = 3;" in patch and "// one" not in patch
