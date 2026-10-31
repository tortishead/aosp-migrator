from pathlib import Path

import pytest
from repo_tree import RepoTree

from aosp_migrator.prescan.deps import Dependency, DiffFiles, FileBlobs, file_blobs, find

CORE = "system/core"
C = "init/init.cpp"
NEW = "init/oem.cpp"


def lines(*body: str) -> str:
    return "\n".join(["// head", *body, *(f"// tail {n}" for n in range(6))]) + "\n"


@pytest.fixture
def target(tmp_path: Path) -> RepoTree:
    tree = RepoTree(tmp_path / "aosp")
    tree.add_project(CORE, {C: lines("int a = 1;")})
    return tree


@pytest.fixture
def series(tmp_path: Path) -> tuple[str, str]:
    """Patch `first` adds a line and a file, `second` changes that line; made in a copy of the target, so the target has neither."""
    src = RepoTree(tmp_path / "src")
    src.add_project(CORE, {C: lines("int a = 1;")})
    first = src.commit(CORE, {C: lines("int a = 1;", "int b = 2;"), NEW: "void oem() {}\n"}, "add b")
    second = src.commit(CORE, {C: lines("int a = 1;", "int b = 3;")}, "change b")
    return src.format_patch(CORE, first), src.format_patch(CORE, second)


def diff(attachment_id: int, item_id: int, text: str, project: str | None = CORE) -> DiffFiles:
    """The diff as if every file conflicted."""
    files = file_blobs(text)
    return DiffFiles(attachment_id, item_id, project, files, frozenset(f.new_path for f in files))


def test_file_blobs(series: tuple[str, str]) -> None:
    first, _ = series
    files = {f.path: f for f in file_blobs(first)}
    assert files[NEW].pre is None and files[NEW].post is not None
    assert files[C].pre is not None and files[C].post is not None


def test_second_depends_on_first(target: RepoTree, series: tuple[str, str]) -> None:
    first, second = series
    pre = {f.path: f.pre for f in file_blobs(second)}[C]
    assert pre is not None
    assert find(target.root, [diff(1, 10, first), diff(2, 20, second)]) == [Dependency(2, C, pre, 1, "pending")]


def test_same_item_is_no_dependency(target: RepoTree, series: tuple[str, str]) -> None:
    first, second = series
    assert find(target.root, [diff(1, 10, first), diff(2, 10, second)]) == []


def test_in_target_when_the_target_has_the_blob(target: RepoTree, series: tuple[str, str]) -> None:
    first, second = series
    target.commit(CORE, {C: lines("int a = 1;", "int b = 2;")}, "b is in")
    assert [d.state for d in find(target.root, [diff(1, 10, first), diff(2, 20, second)])] == ["in_target"]


def test_missing_when_no_diff_makes_the_blob(target: RepoTree, series: tuple[str, str]) -> None:
    _, second = series
    assert [(d.depends_on_id, d.state) for d in find(target.root, [diff(2, 20, second)])] == [(None, "missing")]


def test_missing_only_for_conflicting_files(target: RepoTree, series: tuple[str, str]) -> None:
    _, second = series
    applies = DiffFiles(2, 20, CORE, file_blobs(second))
    assert find(target.root, [applies]) == []


def test_unmapped_diff_is_not_checked_against_the_target(target: RepoTree, series: tuple[str, str]) -> None:
    first, second = series
    assert [(d.depends_on_id, d.state) for d in find(target.root, [diff(1, 10, first), diff(2, 20, second, None)])] == [(1, "pending")]
    assert find(target.root, [diff(2, 20, second, None)]) == []


def test_other_project_or_path_is_no_maker(target: RepoTree, series: tuple[str, str]) -> None:
    first, second = series
    other = diff(1, 10, first, "frameworks/av")
    assert [d.state for d in find(target.root, [other, diff(2, 20, second)])] == ["missing"]
    pre = {f.path: f.pre for f in file_blobs(second)}[C]
    moved = DiffFiles(1, 10, CORE, [FileBlobs("x", "elsewhere.cpp", None, pre)])
    assert [d.state for d in find(target.root, [moved, diff(2, 20, second)])] == ["missing"]


def test_abbreviated_ids_match_by_prefix(target: RepoTree, series: tuple[str, str]) -> None:
    _, second = series
    pre = {f.path: f.pre for f in file_blobs(second)}[C]
    assert pre is not None
    full = DiffFiles(1, 10, CORE, [FileBlobs(C, C, None, pre + "0" * (40 - len(pre)))])
    assert [(d.depends_on_id, d.state) for d in find(target.root, [full, diff(2, 20, second)])] == [(1, "pending")]
