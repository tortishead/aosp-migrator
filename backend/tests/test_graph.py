from aosp_migrator.importer.diffparse import parse_diff
from aosp_migrator.prescan.graph import Edge, GraphItem, ScannedDiff, build_edges


def diff(path: str, old_start: int, lines: list[str], *, header: str = "") -> str:
    """One-file git diff with a single hunk at `old_start`. Lines carry their +/-/space prefix."""
    old = sum(1 for line in lines if not line.startswith("+"))
    new = sum(1 for line in lines if not line.startswith("-"))
    body = "\n".join(lines)
    return (
        f"{header}diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n"
        f"@@ -{old_start},{old} +{old_start},{new} @@\n{body}\n"
    )


def patch(subject: str, author: str, text: str, sha: str = "1" * 40) -> str:
    return f"From {sha} Mon Sep 17 00:00:00 2001\nFrom: Dev <{author}>\nDate: Mon, 21 Sep 2026 10:00:00 +0200\nSubject: {subject}\n\n---\n{text}-- \n2.43.0\n"


def item(item_id: int, *diffs: tuple[str | None, str], links: list[tuple[int, str]] | None = None) -> GraphItem:
    return GraphItem(item_id, [ScannedDiff(project, parse_diff(text)) for project, text in diffs], links or [])


def edges(*items: GraphItem) -> dict[tuple[int, int, str], dict]:
    return {e.key: e.detail for e in build_edges(items)}


CONTEXT = [" a", " b", " c"]


def test_same_file_without_overlap_is_a_files_edge() -> None:
    a = item(1, ("frameworks/av", diff("x.cpp", 10, [*CONTEXT, "+one"])))
    b = item(2, ("frameworks/av", diff("x.cpp", 500, [*CONTEXT, "+two"])))
    assert edges(b, a) == {(1, 2, "files"): {"projects": ["frameworks/av"], "files": ["x.cpp"], "directed": False}}


def test_overlapping_hunks_are_a_hunks_edge() -> None:
    a = item(1, ("frameworks/av", diff("x.cpp", 10, [*CONTEXT, "+one"])))
    b = item(2, ("frameworks/av", diff("x.cpp", 12, [" c", "-d", "+two"])))
    assert edges(a, b) == {
        (1, 2, "hunks"): {"projects": ["frameworks/av"], "files": ["x.cpp"], "overlapping": ["x.cpp"], "directed": False}
    }


def test_touching_lines_another_diff_adds_is_a_hunks_edge() -> None:
    # b's old side (14..17) only meets the lines a adds (new side 10..16), not a's old side (10..13).
    a = item(1, ("p", diff("x.cpp", 10, [*CONTEXT, "+one", "+two", "+three"])))
    b = item(2, ("p", diff("x.cpp", 14, [" one", " two", "-three", "+3"])))
    assert set(edges(a, b)) == {(1, 2, "hunks")}


def test_same_path_in_other_project_or_unmapped_is_no_edge() -> None:
    a = item(1, ("system/core", diff("Android.bp", 1, [*CONTEXT, "+x"])))
    b = item(2, ("hardware/interfaces", diff("Android.bp", 1, [*CONTEXT, "+y"])))
    c = item(3, (None, diff("Android.bp", 1, [*CONTEXT, "+z"])))
    assert edges(a, b, c) == {}


def test_links_between_backlog_items() -> None:
    a = item(1, links=[(2, "is blocked by"), (3, "relates to"), (99, "blocks")])
    b = item(2, links=[(1, "blocks")])
    c = item(3)
    d = item(4, links=[(1, "blocks")])
    assert edges(a, b, c, d) == {
        (1, 2, "link"): {"description": "is blocked by", "directed": True},
        (1, 3, "link"): {"description": "relates to", "directed": False},
        (1, 4, "link"): {"description": "is blocked by", "directed": True},
    }


def test_patch_series_later_depends_on_earlier() -> None:
    text = diff("init.cpp", 1, [*CONTEXT, "+x"])
    first = item(1, (None, patch("[PATCH 1/2] init: one", "dev@oem.com", text)))
    second = item(2, (None, patch("[PATCH 2/2] init: two", "dev@oem.com", text)))
    other_author = item(3, (None, patch("[PATCH 1/2] init: three", "else@oem.com", text)))
    single = item(4, (None, patch("[PATCH] init: four", "dev@oem.com", text)))
    assert edges(first, second, other_author, single) == {
        (2, 1, "series"): {"author": "dev@oem.com", "from_patch": "2/2", "to_patch": "1/2", "directed": True}
    }


def test_symbol_added_in_one_repo_used_in_another() -> None:
    aidl = diff(
        "camera/oem/IOemCameraConfig.aidl",
        1,
        [" package android.hardware.camera.oem;", "+interface IOemCameraConfig {", "+    void setOemMode(int mode);", "+}"],
    )
    use = diff(
        "services/camera/libcameraservice/CameraService.cpp",
        40,
        [*CONTEXT, "+    auto config = IOemCameraConfig::fromBinder(binder);", "+    config->setOemMode(1);"],
    )
    hal = item(1, ("hardware/interfaces", aidl))
    service = item(2, ("frameworks/av", use))
    assert edges(hal, service) == {(2, 1, "symbol"): {"symbols": ["IOemCameraConfig", "setOemMode"], "directed": True}}


def test_symbol_kinds() -> None:
    defines = diff(
        "x.java",
        1,
        [
            " class A {",
            '+    static final String KEY_OEM_MODE = "persist.oem.camera.mode";',
            "+    public static void enableOemMode(Context context) {",
            '+    <string name="oem_brightness_title">Screen brightness</string>',
            '+    <permission android:name="com.oem.permission.CAMERA_MODE" />',
        ],
    )
    uses = diff(
        "y.java",
        1,
        [
            " class B {",
            "+    String s = A.KEY_OEM_MODE;",
            '+    SystemProperties.get("persist.oem.camera.mode");',
            "+    A.enableOemMode(this);",
            "+    getString(R.string.oem_brightness_title);",
            '+    enforceCallingPermission("com.oem.permission.CAMERA_MODE");',
        ],
    )
    found = edges(item(1, (None, defines)), item(2, (None, uses)))
    assert found[(2, 1, "symbol")]["symbols"] == [
        "KEY_OEM_MODE",
        "com.oem.permission.CAMERA_MODE",
        "enableOemMode",
        "oem_brightness_title",
        "persist.oem.camera.mode",
    ]


def test_modified_or_called_names_are_not_definitions() -> None:
    # The signature changes but the name existed before; calls and returns define nothing.
    a = diff(
        "x.h",
        1,
        [
            "-    virtual void        notifyCameraState(int id);",
            "+    virtual status_t    notifyCameraState(int id);",
            "+    return computeSomething(id);",
            "+    refreshOemLayout();",
        ],
    )
    b = diff("y.cpp", 1, [" notifyCameraState(1);", " computeSomething(2);", " refreshOemLayout();"])
    assert edges(item(1, (None, a)), item(2, (None, b))) == {}


def test_no_self_edges() -> None:
    a = item(
        1,
        ("p", diff("x.cpp", 10, [*CONTEXT, "+void addedHelper() {"])),
        ("p", diff("x.cpp", 10, [*CONTEXT, "+  addedHelper();"])),
        links=[(1, "relates to")],
    )
    assert build_edges([a]) == []


def test_edge_key() -> None:
    assert Edge(1, 2, "files", {}).key == (1, 2, "files")
