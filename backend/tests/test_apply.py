import hashlib
import shutil
from pathlib import Path

import pytest
from repo_tree import RepoTree, change_id

from aosp_migrator.prescan.apply import BINARY, MERGE_CONFLICT, MISSING_BLOB, NO_HISTORY, ApplyCheck, check_apply
from aosp_migrator.prescan.patch import Patch

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


def apply_to_worktree(tree: RepoTree, patch: str) -> None:
    tree.git(PROJECT, "apply", "--check", "-", input=patch)
    tree.git(PROJECT, "apply", "-", input=patch)


def test_clean_apply(tree: RepoTree, project: Path) -> None:
    patch = fix_patch(tree)
    result = check_apply(project, patch)
    assert result == ApplyCheck("applies", {"files": {CPP: "applies", H: "applies"}})
    # The file sections as written.
    assert result.patch == "".join(section.patch for section in Patch.parse(patch).files)


def test_three_way_after_drift(tree: RepoTree, project: Path) -> None:
    patch = fix_patch(tree)
    # A context line moved on: no plain apply, but the merge is clean.
    tree.commit(PROJECT, {CPP: lines("int resume() {", "  return 0;", "}", top="// top, reworded")}, "camera: drift")
    result = check_apply(project, patch)
    assert result == ApplyCheck("3way", {"files": {CPP: "3way", H: "applies"}})
    # The merged result: applies to HEAD as is, both files included.
    assert result.patch is not None
    apply_to_worktree(tree, result.patch)
    assert (project / CPP).read_text() == lines("int resume() {", "  if (!client) return -1;", "  return 0;", "}", top="// top, reworded")
    assert (project / H).read_text() == lines("int resume() const;")


def test_three_way_new_file(tree: RepoTree, project: Path) -> None:
    sha = tree.commit(PROJECT, {CPP: lines("int resume() {", "  if (!client) return -1;", "  return 0;", "}"), "services/camera/Oem.cpp": "// oem\n"}, "camera: oem")
    patch = tree.format_patch(PROJECT, sha)
    tree.git(PROJECT, "reset", "-q", "--hard", "HEAD~1")
    tree.commit(PROJECT, {CPP: lines("int resume() {", "  return 0;", "}", top="// top, reworded")}, "camera: drift")
    result = check_apply(project, patch)
    assert result.status == "3way"
    assert result.patch is not None
    apply_to_worktree(tree, result.patch)
    assert (project / "services/camera/Oem.cpp").read_text() == "// oem\n"


def test_real_conflict(tree: RepoTree, project: Path) -> None:
    patch = fix_patch(tree)
    tree.commit(PROJECT, {H: lines("long resume();")}, "camera: other change")
    result = check_apply(project, patch)
    assert result == ApplyCheck("conflicts", {"files": {CPP: "applies", H: "conflicts"}, "errors": {H: MERGE_CONFLICT}})
    assert result.patch is None


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
    result = check_apply(project, patch)
    assert result == ApplyCheck("conflicts", {"files": {"logo.png": "conflicts"}, "errors": {"logo.png": BINARY}})
    assert result.patch is None


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


def test_three_way_patch_leaves_tree_and_object_store_unchanged(tree: RepoTree, project: Path) -> None:
    patch = fix_patch(tree)
    tree.commit(PROJECT, {CPP: lines("int resume() {", "  return 0;", "}", top="// top, reworded")}, "camera: drift")
    (project / "local.txt").write_text("untracked\n")
    before = snapshot(project)
    assert check_apply(project, patch).patch is not None
    assert snapshot(project) == before


def test_present_files_left_out(tree: RepoTree, project: Path) -> None:
    patch = fix_patch(tree)
    tree.commit(PROJECT, {H: lines("int resume() const;")}, "camera: header half only")
    result = check_apply(project, patch, [H])
    assert result == ApplyCheck("applies", {"files": {CPP: "applies"}})
    assert result.patch is not None and H not in result.patch
    apply_to_worktree(tree, result.patch)


def test_patch_author_and_message(tree: RepoTree) -> None:
    tree.commit(PROJECT, {CPP: lines("int resume() {", "  if (!client) return -1;", "  return 0;", "}")}, "camera: fix")
    tree.commit(PROJECT, {H: lines("int resume() const;")}, "camera: const", author="dev@oem.example.com")
    series = Patch.parse(tree.format_patch(PROJECT, "HEAD~2..HEAD"))
    assert series.author == "Test Author <author@android.com>"
    # `[PATCH n/m]` tags dropped, one message after the other.
    assert series.message == (
        f"camera: fix\n\nChange-Id: {change_id(PROJECT + 'camera: fix')}\n\ncamera: const\n\nChange-Id: {change_id(PROJECT + 'camera: const')}"
    )
    plain = Patch.parse("--- a/x\n+++ b/x\n@@ -1 +1 @@\n-a\n+b\n")
    assert (plain.author, plain.message) == (None, None)
