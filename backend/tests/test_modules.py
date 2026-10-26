import json
from pathlib import Path

import pytest

from aosp_migrator.build.modules import (
    affected_modules,
    changed_files,
    find_module_info,
    read_module_info,
    to_build_root,
    to_target_tree,
)
from aosp_migrator.importer.diffparse import FileChange

MODULES = {
    "framework": {"module_name": "framework", "path": ["frameworks/base"]},
    "services": {"module_name": "services", "path": ["frameworks/base/services"]},
    "services.core": {"module_name": "services.core", "path": ["frameworks/base/services/core"]},
    "SystemUI": {"module_name": "SystemUI", "path": ["frameworks/base/packages/SystemUI"]},
    "SystemUI-tests": {"module_name": "SystemUI-tests", "path": ["frameworks/base/packages/SystemUI"]},
    "libcutils": {"module_name": "libcutils", "path": ["system/core/libcutils"]},
    "libcutils_32": {"module_name": "libcutils", "path": ["system/core/libcutils"]},
}


def write_module_info(root: Path, product: str = "generic", modules: dict = MODULES) -> Path:
    path = root / "out" / "target" / "product" / product / "module-info.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(modules))
    return path


def touch(root: Path, *paths: str) -> None:
    for rel in paths:
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text("")


def test_changed_files_joins_project_and_keeps_rename_sources() -> None:
    changes = [
        FileChange(path="core/A.java"),
        FileChange(path="core/B.java", old_path="old/B.java", status="renamed"),
        FileChange(path="core/A.java"),
    ]
    assert changed_files("frameworks/base", changes) == [
        "frameworks/base/core/A.java",
        "frameworks/base/old/B.java",
        "frameworks/base/core/B.java",
    ]
    assert changed_files(".", [FileChange(path="Android.bp")]) == ["Android.bp"]


def test_path_conversion_with_repo_root_above_target_tree(tmp_path: Path) -> None:
    root, target = tmp_path, tmp_path / "qssi"
    assert to_build_root("frameworks/base/A.java", target, root) == "qssi/frameworks/base/A.java"
    assert to_target_tree("qssi/frameworks/base/A.java", target, root) == "frameworks/base/A.java"
    assert to_target_tree("vendor/x/A.java", target, root) is None


def test_path_conversion_with_build_root_below_target_tree(tmp_path: Path) -> None:
    target, root = tmp_path, tmp_path / "qssi"
    assert to_build_root("qssi/system/core/x.c", target, root) == "system/core/x.c"
    assert to_build_root("vendor/x/A.java", target, root) is None
    assert to_target_tree("system/core/x.c", target, root) == "qssi/system/core/x.c"


def test_read_module_info_merges_variants(tmp_path: Path) -> None:
    by_path = read_module_info(write_module_info(tmp_path))
    assert by_path["system/core/libcutils"] == {"libcutils"}
    assert by_path["frameworks/base/packages/SystemUI"] == {"SystemUI", "SystemUI-tests"}


def test_find_module_info_needs_exactly_one_product(tmp_path: Path) -> None:
    assert find_module_info(tmp_path) is None
    only = write_module_info(tmp_path, "a")
    assert find_module_info(tmp_path) == only
    write_module_info(tmp_path, "b")
    assert find_module_info(tmp_path) is None


def test_longest_module_path_wins(tmp_path: Path) -> None:
    write_module_info(tmp_path)
    result = affected_modules(
        [
            "frameworks/base/services/core/java/Foo.java",
            "frameworks/base/core/java/Bar.java",
            "frameworks/base/packages/SystemUI/src/Baz.kt",
            "system/core/libcutils/a.c",
        ],
        tmp_path,
        tmp_path,
    )
    assert result.tool == "m"
    assert result.targets == ["SystemUI", "SystemUI-tests", "framework", "libcutils", "services.core"]
    assert result.unbuilt == []


def test_android_bp_change_builds_its_modules(tmp_path: Path) -> None:
    write_module_info(tmp_path)
    result = affected_modules(["frameworks/base/services/Android.bp"], tmp_path, tmp_path)
    assert result.targets == ["services"]


def test_file_under_no_module_builds_nothing(tmp_path: Path) -> None:
    write_module_info(tmp_path)
    result = affected_modules(["system/core/OWNERS", "docs/README.md"], tmp_path, tmp_path)
    assert result.nothing_to_build
    assert result.unbuilt == ["system/core/OWNERS", "docs/README.md"]


def test_module_info_paths_are_relative_to_a_repo_root_above_the_target_tree(tmp_path: Path) -> None:
    write_module_info(tmp_path, modules={"x": {"module_name": "x", "path": ["qssi/system/core/libcutils"]}})
    result = affected_modules(
        ["system/core/libcutils/a.c", "system/core/OWNERS"], tmp_path / "qssi", tmp_path
    )
    assert result.targets == ["x"]
    assert result.unbuilt == ["system/core/OWNERS"]


@pytest.mark.parametrize("products", [[], ["a", "b"]])
def test_fallback_to_nearest_build_file_directory(tmp_path: Path, products: list[str]) -> None:
    for product in products:
        write_module_info(tmp_path, product)
    touch(
        tmp_path,
        "frameworks/base/Android.bp",
        "frameworks/base/services/core/Android.bp",
        "system/core/libcutils/Android.mk",
        "Android.bp",
    )
    result = affected_modules(
        [
            "frameworks/base/services/core/java/Foo.java",
            "frameworks/base/core/java/Bar.java",
            "system/core/libcutils/include/a.h",
            "system/core/libcutils/Android.mk",
            "system/core/OWNERS",
        ],
        tmp_path,
        tmp_path,
    )
    assert result.tool == "mmm"
    assert result.targets == ["frameworks/base", "frameworks/base/services/core", "system/core/libcutils"]
    # The build root's own Android.bp is never a fallback.
    assert result.unbuilt == ["system/core/OWNERS"]


def test_fallback_with_repo_root_above_target_tree(tmp_path: Path) -> None:
    touch(tmp_path, "qssi/system/core/Android.bp")
    result = affected_modules(["system/core/init/main.cpp"], tmp_path / "qssi", tmp_path)
    assert result.targets == ["qssi/system/core"]


def test_file_outside_build_root_builds_nothing(tmp_path: Path) -> None:
    root = tmp_path / "qssi"
    write_module_info(root)
    result = affected_modules(["vendor/x/Android.bp"], tmp_path, root)
    assert result.nothing_to_build
    assert result.unbuilt == ["vendor/x/Android.bp"]
