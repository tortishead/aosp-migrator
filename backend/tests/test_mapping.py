import shutil
from pathlib import Path

import pytest
from repo_tree import RepoTree, default_repo_tree

from aosp_migrator.importer.diffparse import FileChange
from aosp_migrator.prescan.mapping import (
    ROOT_PROJECT,
    DiffMapping,
    ProjectListMissing,
    discover_projects,
    map_diff,
    path_hints,
    read_project_list,
)

PROJECTS = [
    "frameworks/base",
    "frameworks/base/packages/SystemUI",
    "frameworks/av",
    "system/core",
    "hardware/interfaces",
]


@pytest.fixture
def tree(tmp_path: Path) -> RepoTree:
    return default_repo_tree(tmp_path / "aosp")


def test_read_project_list(tree: RepoTree) -> None:
    assert read_project_list(tree.root) == PROJECTS


def test_read_project_list_ignores_blank_lines_and_slashes(tmp_path: Path) -> None:
    (tmp_path / ".repo").mkdir()
    (tmp_path / ".repo" / "project.list").write_text("frameworks/base/\n\n  system/core \n")
    assert read_project_list(tmp_path) == ["frameworks/base", "system/core"]


def test_missing_project_list(tmp_path: Path) -> None:
    with pytest.raises(ProjectListMissing, match="repo sync"):
        read_project_list(tmp_path)


def test_discover_projects_finds_nested_git_dirs(tree: RepoTree) -> None:
    shutil.rmtree(tree.root / ".repo")
    (tree.root / "out" / "soong" / ".git").mkdir(parents=True)  # build output is skipped
    (tree.root / "vendor" / "qcom").mkdir(parents=True)  # no .git: not a project
    (tree.root / "link").symlink_to(tree.root / "system")  # symlinks are not followed
    assert discover_projects(tree.root) == sorted(PROJECTS)
    assert discover_projects(tree.root / "frameworks/base") == [ROOT_PROJECT, "packages/SystemUI"]
    assert discover_projects(tree.root / "vendor") == [ROOT_PROJECT]


def test_fixture_projects_are_git_repos_with_change_ids(tree: RepoTree) -> None:
    log = tree.git("frameworks/av", "log", "-1", "--format=%B")
    assert "Change-Id: I" in log


@pytest.mark.parametrize(
    "text",
    [
        "Fix in {AOSP_HOME}/frameworks/av/services/camera/CameraService.cpp please",
        "Fix in ${AOSP_HOME}/frameworks/av/services/camera/CameraService.cpp",
        "Fix in $AOSP_HOME/frameworks/av/services/camera/CameraService.cpp",
        "Fix in frameworks/av/services/camera/CameraService.cpp.",
        "See (frameworks/av).",
        "Path: `{ANDROID_BUILD_TOP}/frameworks/av/`",
    ],
)
def test_hint_variants(text: str) -> None:
    assert path_hints(text, PROJECTS) == ["frameworks/av"]


def test_hint_longest_prefix_wins() -> None:
    text = "{AOSP_HOME}/frameworks/base/packages/SystemUI/src/Foo.java and frameworks/base/core/Bar.java"
    assert path_hints(text, PROJECTS) == ["frameworks/base/packages/SystemUI", "frameworks/base"]


def test_hint_needs_whole_path_segment() -> None:
    # `frameworks/avx` is not under `frameworks/av`.
    assert path_hints("frameworks/avx/foo.c and system/core2/x", PROJECTS) == []


def test_hint_deduplicates_and_ignores_unknown_paths() -> None:
    text = "system/core/init/a.cpp, system/core/init/b.cpp, vendor/acme/thing.c, http://x"
    assert path_hints(text, PROJECTS) == ["system/core"]


def test_unique_file_match(tree: RepoTree) -> None:
    files = [FileChange("services/camera/libcameraservice/CameraService.cpp", added=3)]
    assert map_diff(tree.root, PROJECTS, files) == DiffMapping("frameworks/av", "files")


def test_zero_matches(tree: RepoTree) -> None:
    files = [FileChange("does/not/exist.c")]
    assert map_diff(tree.root, PROJECTS, files) == DiffMapping(None, None, [])


def test_multiple_matches(tree: RepoTree) -> None:
    files = [FileChange("Android.bp")]
    assert map_diff(tree.root, PROJECTS, files) == DiffMapping(None, None, ["system/core", "hardware/interfaces"])


def test_hint_resolves_multiple_matches(tree: RepoTree) -> None:
    files = [FileChange("Android.bp")]
    assert map_diff(tree.root, PROJECTS, files, ["hardware/interfaces"]) == DiffMapping("hardware/interfaces", "hint")


def test_hint_not_verified_falls_back_to_files(tree: RepoTree) -> None:
    files = [FileChange("init/init.cpp")]
    assert map_diff(tree.root, PROJECTS, files, ["frameworks/av"]) == DiffMapping("system/core", "files")


def test_several_verified_hints_stay_ambiguous(tree: RepoTree) -> None:
    files = [FileChange("Android.bp")]
    mapping = map_diff(tree.root, PROJECTS, files, ["system/core", "hardware/interfaces"])
    assert mapping == DiffMapping(None, None, ["system/core", "hardware/interfaces"])


def test_all_touched_files_must_exist(tree: RepoTree) -> None:
    files = [FileChange("Android.bp"), FileChange("init/init.cpp")]
    assert map_diff(tree.root, PROJECTS, files) == DiffMapping("system/core", "files")


def test_new_files_are_not_evidence(tree: RepoTree) -> None:
    files = [
        FileChange("services/camera/libcameraservice/CameraService.h"),
        FileChange("services/camera/libcameraservice/New.cpp", status="added"),
    ]
    assert map_diff(tree.root, PROJECTS, files) == DiffMapping("frameworks/av", "files")


def test_rename_checks_source_path(tree: RepoTree) -> None:
    files = [FileChange("init/main.cpp", old_path="init/init.cpp", status="renamed")]
    assert map_diff(tree.root, PROJECTS, files) == DiffMapping("system/core", "files")


def test_only_new_files_take_single_hint_on_trust(tree: RepoTree) -> None:
    files = [FileChange("brand/new.c", status="added")]
    assert map_diff(tree.root, PROJECTS, files, ["system/core"]) == DiffMapping("system/core", "hint")
    assert map_diff(tree.root, PROJECTS, files) == DiffMapping(None, None, [])


def test_single_project_tree(tree: RepoTree) -> None:
    root = tree.root / "system/core"
    assert map_diff(root, [ROOT_PROJECT], [FileChange("init/init.cpp")]) == DiffMapping(ROOT_PROJECT, "files")
    assert map_diff(root, [ROOT_PROJECT], [FileChange("brand/new.c", status="added")]) == DiffMapping(ROOT_PROJECT, "files")
    assert map_diff(root, [ROOT_PROJECT], [FileChange("missing.c")]) == DiffMapping(None, None, [])


def test_parent_escape_is_ignored(tree: RepoTree) -> None:
    # `../av/...` from frameworks/base would exist, but must not count.
    files = [FileChange("../av/services/camera/libcameraservice/CameraService.cpp")]
    assert map_diff(tree.root, PROJECTS, files) == DiffMapping(None, None, [])


def test_multi_repo_ticket(tree: RepoTree) -> None:
    description = (
        "Touches {AOSP_HOME}/frameworks/base/core/java/android/app/Activity.java and\n"
        "{AOSP_HOME}/frameworks/base/packages/SystemUI/src/com/android/systemui/Dependency.java"
    )
    hints = path_hints(description, PROJECTS)
    framework = [FileChange("core/java/android/app/Activity.java")]
    systemui = [FileChange("src/com/android/systemui/Dependency.java")]
    assert map_diff(tree.root, PROJECTS, framework, hints) == DiffMapping("frameworks/base", "hint")
    assert map_diff(tree.root, PROJECTS, systemui, hints) == DiffMapping("frameworks/base/packages/SystemUI", "hint")
