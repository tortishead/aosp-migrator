import json
from pathlib import Path

import pytest

from aosp_migrator.build.modules import (
    API_CHECK_TARGET,
    affected_modules,
    changed_files,
    find_module_info,
    read_module_info,
    to_build_root,
    to_target_tree,
    widening_files,
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
    by_path = read_module_info(write_module_info(tmp_path)).by_path
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


# libcutils is used by init and libutils (directly, through any dependency field) and by
# SystemUI only through libutils; framework is used by services and SystemUI.
USERS = {
    **MODULES,
    "init": {"module_name": "init", "path": ["system/core/init"], "shared_libs": ["libcutils"], "dependencies": ["libcutils", "libbase"]},
    "libutils": {"module_name": "libutils", "path": ["system/core/libutils"], "static_libs": ["libcutils"]},
    "libutils_32": {"module_name": "libutils", "path": ["system/core/libutils"], "static_libs": ["libcutils_32"]},
    "services": {"module_name": "services", "path": ["frameworks/base/services"], "libs": ["framework"]},
    "SystemUI": {"module_name": "SystemUI", "path": ["frameworks/base/packages/SystemUI"], "dependencies": ["framework", "libutils"]},
}


def file_change(path: str, *lines: str) -> FileChange:
    return FileChange(path=path, hunks=["@@ -1,1 +1,1 @@\n" + "\n".join(lines) + "\n"] if lines else [])


def test_read_module_info_users_from_every_dependency_field(tmp_path: Path) -> None:
    users = read_module_info(write_module_info(tmp_path, modules=USERS)).users
    assert users["libcutils"] == {"init", "libutils"}
    assert users["framework"] == {"services", "SystemUI"}
    assert users["libbase"] == {"init"}


def test_widening_files() -> None:
    changes = [
        file_change("libcutils/include/cutils/a.h"),
        file_change("libcutils/a.c", "+int x = 1;"),
        file_change("Android.bp"),
        file_change("aidl/android/IFoo.aidl"),
        file_change("api/current.txt"),
        file_change("api/README.md"),
        file_change("core/java/Public.java", " public class Public {", "+    public void foo() {}"),
        file_change("core/java/Annotated.java", "-    @SystemApi", "     void bar();"),
        file_change("core/java/Override.java", "+    @Override", "+    protected void baz() {}"),
        file_change("core/java/Private.java", " public class Private {", "+    private int count;", "+        return publicValue;"),
    ]
    reaching, api = widening_files("system/core", changes)
    assert reaching == [
        "system/core/libcutils/include/cutils/a.h",
        "system/core/Android.bp",
        "system/core/aidl/android/IFoo.aidl",
        "system/core/core/java/Public.java",
        "system/core/core/java/Annotated.java",
        "system/core/core/java/Override.java",
    ]
    assert api == [
        "system/core/api/current.txt",
        "system/core/core/java/Public.java",
        "system/core/core/java/Annotated.java",
        "system/core/core/java/Override.java",
    ]


def test_header_change_builds_the_users_of_its_module(tmp_path: Path) -> None:
    write_module_info(tmp_path, modules=USERS)
    header = "system/core/libcutils/include/cutils/a.h"
    result = affected_modules([header, "system/core/libcutils/a.c"], tmp_path, tmp_path, reaching=[header])
    assert result.targets == ["libcutils", "init", "libutils"]
    assert (result.dependents, result.dependents_left_out, result.api_check) == (["init", "libutils"], 0, False)


def test_source_change_builds_no_users(tmp_path: Path) -> None:
    write_module_info(tmp_path, modules=USERS)
    result = affected_modules(["system/core/libcutils/a.c"], tmp_path, tmp_path)
    assert (result.targets, result.dependents) == (["libcutils"], [])


def test_dependents_are_capped(tmp_path: Path) -> None:
    write_module_info(tmp_path, modules=USERS)
    header = "system/core/libcutils/include/cutils/a.h"
    result = affected_modules([header], tmp_path, tmp_path, reaching=[header], dependents_cap=1)
    assert (result.targets, result.dependents_left_out) == (["libcutils", "init"], 1)
    result = affected_modules([header], tmp_path, tmp_path, reaching=[header], dependents_cap=0)
    assert (result.targets, result.dependents_left_out) == (["libcutils"], 2)


def test_changed_modules_are_not_built_twice_as_dependents(tmp_path: Path) -> None:
    write_module_info(tmp_path, modules=USERS)
    changed = ["system/core/libcutils/Android.bp", "system/core/libutils/Android.bp"]
    result = affected_modules(changed, tmp_path, tmp_path, reaching=changed)
    assert (result.targets, result.dependents) == (["libcutils", "libutils", "SystemUI", "init"], ["SystemUI", "init"])


def test_api_change_adds_the_api_check(tmp_path: Path) -> None:
    write_module_info(tmp_path, modules=USERS)
    api = "frameworks/base/api/current.txt"
    result = affected_modules([api], tmp_path, tmp_path, api=[api])
    assert result.targets == ["framework", API_CHECK_TARGET]
    java = "frameworks/base/core/java/android/os/Foo.java"
    result = affected_modules([java], tmp_path, tmp_path, reaching=[java], api=[java])
    assert result.targets == ["framework", "SystemUI", "services", API_CHECK_TARGET]


def test_no_dependents_or_api_check_without_module_info(tmp_path: Path) -> None:
    touch(tmp_path, "system/core/libcutils/Android.bp")
    header = "system/core/libcutils/include/cutils/a.h"
    result = affected_modules([header], tmp_path, tmp_path, reaching=[header], api=[header])
    assert (result.tool, result.targets, result.api_check) == ("mmm", ["system/core/libcutils"], False)
