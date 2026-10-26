import hashlib
from pathlib import Path

import pytest
from repo_tree import RepoTree

from aosp_migrator.build.tree import (
    ApplyFailed,
    InFlight,
    TreeRefused,
    UnitPatch,
    apply_patches,
    prepare,
    restore,
)

BASE = "frameworks/base"
CORE = "system/core"
ACTIVITY = "core/java/android/app/Activity.java"


@pytest.fixture
def tree(tmp_path: Path) -> RepoTree:
    tree = RepoTree(tmp_path / "aosp")
    tree.add_project(BASE, {ACTIVITY: "class Activity {}\n", "Android.bp": "java_library {}\n"})
    tree.add_project(CORE, {"init/init.cpp": "// init\n"})
    return tree


def snapshot(root: Path) -> dict[str, str]:
    """Every file and directory under `root` (`.git` included), with a content hash for files."""
    return {
        p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else "dir"
        for p in sorted(root.rglob("*"))
        # The index is rewritten by `git status` / `reset` with the same entries; its stat cache may differ.
        # `reset --hard` also records ORIG_HEAD and a reflog entry.
        if p.name not in ("index", "ORIG_HEAD") and "logs" not in p.relative_to(root).parts
    }


def make_patch(tree: RepoTree, project: str, files: dict[str, str | None]) -> UnitPatch:
    """A patch of `files` against the project's HEAD; the project is left at HEAD."""
    head = tree.git(project, "rev-parse", "HEAD").strip()
    sha = tree.commit(project, files, f"change {sorted(files)}")
    diff = tree.git(project, "diff", "--binary", "-M", head, sha)
    tree.git(project, "reset", "-q", "--hard", head)
    return UnitPatch(project=project, diff=diff, base_head=head)


def build_cycle(tree: RepoTree, patches: list[UnitPatch], three_way: bool = False) -> InFlight:
    inflight = prepare(tree.root, patches)
    try:
        apply_patches(tree.root, patches, three_way)
    finally:
        restore(tree.root, InFlight.from_json(inflight.to_json()))
    return inflight


def test_restore_leaves_the_tree_byte_identical(tree: RepoTree) -> None:
    patches = [
        make_patch(tree, BASE, {ACTIVITY: "class Activity { int x; }\n", "core/java/android/app/New.java": "class New {}\n"}),
        make_patch(tree, CORE, {"init/init.cpp": None, "libnew/deep/a.c": "int a;\n"}),
    ]
    before = snapshot(tree.root)
    inflight = prepare(tree.root, patches)
    apply_patches(tree.root, patches)
    assert (tree.root / BASE / "core/java/android/app/New.java").is_file()
    assert (tree.root / CORE / "libnew/deep/a.c").is_file()
    assert not (tree.root / CORE / "init/init.cpp").exists()
    restore(tree.root, inflight)
    assert snapshot(tree.root) == before
    assert inflight.added_files == {BASE: ["core/java/android/app/New.java"], CORE: ["libnew/deep/a.c"]}
    assert inflight.added_dirs == {BASE: [], CORE: ["libnew", "libnew/deep"]}


def test_three_way_apply_restores_too(tree: RepoTree) -> None:
    patch = make_patch(tree, BASE, {ACTIVITY: "class Activity { int x; }\n", "New.java": "class New {}\n"})
    before = snapshot(tree.root)
    build_cycle(tree, [patch], three_way=True)
    assert snapshot(tree.root) == before


def test_rename_target_counts_as_added(tree: RepoTree) -> None:
    head = tree.git(BASE, "rev-parse", "HEAD").strip()
    tree.git(BASE, "mv", ACTIVITY, "core/Moved.java")
    tree.git(BASE, "commit", "-q", "-m", "move")
    diff = tree.git(BASE, "diff", "-M", head, "HEAD")
    tree.git(BASE, "reset", "-q", "--hard", head)
    patch = UnitPatch(project=BASE, diff=diff, base_head=head)
    before = snapshot(tree.root)
    inflight = build_cycle(tree, [patch])
    assert inflight.added_files == {BASE: ["core/Moved.java"]}
    assert snapshot(tree.root) == before


def test_users_untracked_files_survive(tree: RepoTree) -> None:
    patch = make_patch(tree, BASE, {"core/New.java": "class New {}\n"})
    (tree.root / BASE / "notes.txt").write_text("mine\n")
    (tree.root / BASE / "core" / "scratch.java").write_text("mine\n")
    before = snapshot(tree.root)
    build_cycle(tree, [patch])
    assert snapshot(tree.root) == before


def test_directory_the_user_filled_meanwhile_stays(tree: RepoTree) -> None:
    patch = make_patch(tree, CORE, {"libnew/a.c": "int a;\n"})
    inflight = prepare(tree.root, [patch])
    apply_patches(tree.root, [patch])
    (tree.root / CORE / "libnew" / "mine.txt").write_text("mine\n")
    restore(tree.root, inflight)
    assert not (tree.root / CORE / "libnew" / "a.c").exists()
    assert (tree.root / CORE / "libnew" / "mine.txt").read_text() == "mine\n"


@pytest.mark.parametrize("staged", [False, True])
def test_dirty_project_is_refused_before_any_write(tree: RepoTree, staged: bool) -> None:
    patches = [
        make_patch(tree, CORE, {"init/init.cpp": "// changed\n"}),
        make_patch(tree, BASE, {ACTIVITY: "class Activity { int x; }\n"}),
    ]
    (tree.root / BASE / "Android.bp").write_text("// local edit\n")
    if staged:
        tree.git(BASE, "add", "Android.bp")
    before = snapshot(tree.root)
    with pytest.raises(TreeRefused, match=f"project dirty: {BASE}"):
        prepare(tree.root, patches)
    assert snapshot(tree.root) == before


def test_untracked_file_where_the_patch_adds_one_is_refused(tree: RepoTree) -> None:
    patch = make_patch(tree, BASE, {"core/New.java": "class New {}\n"})
    (tree.root / BASE / "core" / "New.java").write_text("mine\n")
    with pytest.raises(TreeRefused, match=f"project dirty: {BASE}"):
        prepare(tree.root, [patch])
    assert (tree.root / BASE / "core" / "New.java").read_text() == "mine\n"


def test_moved_head_is_refused(tree: RepoTree) -> None:
    patch = make_patch(tree, BASE, {ACTIVITY: "class Activity { int x; }\n"})
    tree.commit(BASE, {"Android.bp": "java_library { x }\n"}, "upstream moved on")
    with pytest.raises(TreeRefused, match=f"HEAD moved: {BASE}"):
        prepare(tree.root, [patch])


def test_project_without_git_is_refused(tree: RepoTree) -> None:
    (tree.root / "vendor" / "x").mkdir(parents=True)
    with pytest.raises(TreeRefused, match="not a git project: vendor/x"):
        prepare(tree.root, [UnitPatch(project="vendor/x", diff="", base_head=None)])


def test_multi_project_unit_restores_all_on_partial_failure(tree: RepoTree) -> None:
    good = make_patch(tree, BASE, {ACTIVITY: "class Activity { int x; }\n", "core/New.java": "class New {}\n"})
    bad = make_patch(tree, CORE, {"init/init.cpp": "// changed\n"})
    bad.diff = bad.diff.replace("-// init", "-// not there")
    before = snapshot(tree.root)
    with pytest.raises(ApplyFailed, match=CORE):
        build_cycle(tree, [good, bad])
    assert snapshot(tree.root) == before


def test_conflicting_cluster_members_fail_with_three_way(tree: RepoTree) -> None:
    first = make_patch(tree, BASE, {ACTIVITY: "class Activity { int a; }\n"})
    second = make_patch(tree, BASE, {ACTIVITY: "class Activity { int b; }\n"})
    before = snapshot(tree.root)
    with pytest.raises(ApplyFailed, match=BASE):
        build_cycle(tree, [first, second], three_way=True)
    assert snapshot(tree.root) == before


def test_recovery_restores_a_half_applied_tree(tree: RepoTree) -> None:
    first = make_patch(tree, BASE, {ACTIVITY: "class Activity { int x; }\n", "core/New.java": "class New {}\n"})
    second = make_patch(tree, CORE, {"libnew/a.c": "int a;\n"})
    before = snapshot(tree.root)
    stored = prepare(tree.root, [first, second]).to_json()
    # Crash after the first patch.
    apply_patches(tree.root, [first])
    restore(tree.root, InFlight.from_json(stored))
    assert snapshot(tree.root) == before
