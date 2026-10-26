import hashlib
import shutil
from pathlib import Path

import pytest
from repo_tree import RepoTree

from aosp_migrator.prescan.apply import BINARY, MERGE_CONFLICT, MISSING_BLOB, NO_HISTORY, ApplyCheck, check_apply

PROJECT = "frameworks/av"
CPP = "services/camera/CameraService.cpp"
H = "services/camera/CameraService.h"


def lines(*body: str, top: str = "// top") -> str:
    """`top`, then filler around `body`: the fix's hunk context reaches `top`, the tail stays apart."""
    return "\n".join([*(f"// a{n}" for n in range(8)), top, "// b", *body, *(f"// tail {n}" for n in range(10))]) + "\n"


@pytest.fixture
def tree(tmp_path: Path) -> RepoTree:
    tree = RepoTree(tmp_path / "aosp")
    tree.add_project(PROJECT, {CPP: lines("int resume() {", "  return 0;", "}"), H: lines("int resume();")})
    return tree


@pytest.fixture
def project(tree: RepoTree) -> Path:
    return tree.root / PROJECT


def fix_patch(tree: RepoTree) -> str:
    """Patch of a two-file fix; the project is reset to before it."""
    sha = tree.commit(PROJECT, {CPP: lines("int resume() {", "  if (!client) return -1;", "  return 0;", "}"), H: lines("int resume() const;")}, "camera: fix resume")
    patch = tree.format_patch(PROJECT, sha)
    tree.git(PROJECT, "reset", "-q", "--hard", "HEAD~1")
    return patch


def snapshot(root: Path) -> str:
    """Hash of every file under `root`, .git included."""
    digest = hashlib.sha256()
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        digest.update(str(path.relative_to(root)).encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def test_clean_apply(tree: RepoTree, project: Path) -> None:
    assert check_apply(project, fix_patch(tree)) == ApplyCheck("applies", {"files": {CPP: "applies", H: "applies"}})


def test_three_way_after_drift(tree: RepoTree, project: Path) -> None:
    patch = fix_patch(tree)
    # A context line moved on: no plain apply, but the merge is clean.
    tree.commit(PROJECT, {CPP: lines("int resume() {", "  return 0;", "}", top="// top, reworded")}, "camera: drift")
    assert check_apply(project, patch) == ApplyCheck("3way", {"files": {CPP: "3way", H: "applies"}})


def test_real_conflict(tree: RepoTree, project: Path) -> None:
    patch = fix_patch(tree)
    tree.commit(PROJECT, {H: lines("long resume();")}, "camera: other change")
    result = check_apply(project, patch)
    assert result == ApplyCheck("conflicts", {"files": {CPP: "applies", H: "conflicts"}, "errors": {H: MERGE_CONFLICT}})


def test_missing_preimage_blob(tree: RepoTree, project: Path, tmp_path: Path) -> None:
    # The patch was made on an OEM tree whose file never existed in this history.
    oem = RepoTree(tmp_path / "oem")
    oem.add_project(PROJECT, {H: lines("int resume();", "int oem_only();")})
    patch = oem.format_patch(PROJECT, oem.commit(PROJECT, {H: lines("int resume() const;", "int oem_only();")}, "camera: oem"))
    result = check_apply(project, patch)
    assert result == ApplyCheck("conflicts", {"files": {H: "conflicts"}, "errors": {H: MISSING_BLOB}})


def test_plain_tree(tree: RepoTree, project: Path, tmp_path: Path) -> None:
    patch = fix_patch(tree)
    plain = tmp_path / "plain"
    shutil.copytree(project, plain, ignore=shutil.ignore_patterns(".git"))
    assert check_apply(plain, patch).status == "applies"
    (plain / CPP).write_text(lines("int resume() {", "  return 0;", "}", top="// top, reworded"))
    result = check_apply(plain, patch)
    assert result == ApplyCheck("conflicts", {"files": {CPP: "conflicts", H: "applies"}, "errors": {CPP: NO_HISTORY}})


def test_binary_without_data(tree: RepoTree, project: Path) -> None:
    patch = "diff --git a/logo.png b/logo.png\nindex 1111111..2222222 100644\nBinary files a/logo.png and b/logo.png differ\n"
    assert check_apply(project, patch) == ApplyCheck("conflicts", {"files": {"logo.png": "conflicts"}, "errors": {"logo.png": BINARY}})


def test_series_with_same_file_twice(tree: RepoTree, project: Path) -> None:
    tree.commit(PROJECT, {CPP: lines("int resume() {", "  if (!client) return -1;", "  return 0;", "}")}, "camera: fix")
    tree.commit(PROJECT, {CPP: lines("int resume() {", "  if (!client) return -2;", "  return 0;", "}")}, "camera: refix")
    patch = tree.format_patch(PROJECT, "HEAD~2..HEAD")
    tree.git(PROJECT, "reset", "-q", "--hard", "HEAD~2")
    assert check_apply(project, patch).status == "applies"


def test_tree_and_object_store_unchanged(tree: RepoTree, project: Path) -> None:
    patch = fix_patch(tree)
    tree.commit(PROJECT, {CPP: lines("int resume() {", "  return 0;", "}", top="// top, reworded"), H: lines("long resume();")}, "camera: drift")
    (project / "local.txt").write_text("untracked\n")
    before = snapshot(project)
    result = check_apply(project, patch)
    assert result.detail["files"] == {CPP: "3way", H: "conflicts"}
    assert snapshot(project) == before


def test_present_files_left_out(tree: RepoTree, project: Path) -> None:
    patch = fix_patch(tree)
    tree.commit(PROJECT, {H: lines("int resume() const;")}, "camera: header half only")
    assert check_apply(project, patch, [H]) == ApplyCheck("applies", {"files": {CPP: "applies"}})
