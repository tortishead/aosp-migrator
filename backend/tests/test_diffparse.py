import pytest
from fake_jira.fixtures import fixture_file

from aosp_migrator.importer.diffparse import Commit, FileChange, looks_like_diff, parse_diff


def fixture_text(name: str) -> str:
    return fixture_file(name).decode()


def test_format_patch_single_file() -> None:
    parsed = parse_diff(fixture_text("camera-fix.v1.patch"))
    assert parsed.format == "format-patch"
    # The `-- ` signature line is not counted as a removal.
    assert parsed.files == [FileChange("services/camera/libcameraservice/CameraService.cpp", added=3)]


def test_format_patch_multi_file() -> None:
    parsed = parse_diff(fixture_text("camera-fix.v2.patch"))
    assert parsed.format == "format-patch"
    assert [(f.path, f.added, f.removed) for f in parsed.files] == [
        ("services/camera/libcameraservice/CameraService.cpp", 4, 0),
        ("services/camera/libcameraservice/CameraService.h", 1, 1),
    ]


def test_format_patch_commit_header() -> None:
    parsed = parse_diff(fixture_text("camera-fix.v2.patch"))
    assert parsed.commits == [
        Commit(
            sha="9b8a7f6e5d4c3b2a1f0e9d8c7b6a5f4e3d2c1b0a",
            author="Jane Doe <jane.doe@example.com>",
            date="2026-09-25T16:40:00+02:00",
            subject="[PATCH] camera: fix null deref on resume",
            message="Log the missing client and return NO_INIT instead of crashing.\n\nBug: PROJ-101",
        )
    ]


def test_commit_header_folded_encoded_subject() -> None:
    text = (
        "From 3f2a9c1d4e5b6a7c8d9e0f1a2b3c4d5e6f7a8b9c Mon Sep 17 00:00:00 2001\n"
        "From: =?UTF-8?q?J=C3=B6rg?= <j@example.com>\n"
        "Date: not a date\n"
        "Subject: [PATCH v2 1/3] audio: a subject\n"
        " folded over two lines\n"
        "\n"
        "diff --git a/x b/x\n"
    )
    assert parse_diff(text).commits == [
        Commit(
            sha="3f2a9c1d4e5b6a7c8d9e0f1a2b3c4d5e6f7a8b9c",
            author="Jörg <j@example.com>",
            date="not a date",
            subject="[PATCH v2 1/3] audio: a subject folded over two lines",
        )
    ]


def test_plain_diff_has_no_commits() -> None:
    assert parse_diff(fixture_text("settings.diff")).commits == []


def test_plain_git_diff_with_new_file_and_rename() -> None:
    parsed = parse_diff(fixture_text("settings.diff"))
    assert parsed.format == "plain"
    assert parsed.files == [
        FileChange("src/com/android/settings/display/BrightnessLevelPreferenceController.java", added=2, removed=1),
        FileChange("res/values/oem_strings.xml", status="added", added=4),
        FileChange(
            "src/com/android/settings/display/DisplayUtilsOem.java",
            old_path="src/com/android/settings/display/OemDisplayUtils.java",
            status="renamed",
            added=1,
            removed=1,
        ),
        FileChange("Android.bp", added=1),
    ]


def test_plain_unified_diff_without_git_header() -> None:
    parsed = parse_diff(fixture_text("fix.txt"))
    assert parsed.format == "plain"
    assert parsed.files == [FileChange("core/java/android/os/BatteryStats.java", added=1, removed=1)]


def test_binary_patch() -> None:
    parsed = parse_diff(fixture_text("icons.patch"))
    assert parsed.files == [
        FileChange("packages/SystemUI/res/drawable-xxhdpi/oem_battery.png", binary=True),
        FileChange("packages/SystemUI/res/values/dimens.xml", added=1, removed=1),
    ]


def test_patch_series_in_one_file() -> None:
    series = fixture_text("camera-fix.v1.patch") + fixture_text("audio-linked.patch")
    parsed = parse_diff(series)
    assert parsed.format == "format-patch"
    assert [(f.path, f.added) for f in parsed.files] == [
        ("services/camera/libcameraservice/CameraService.cpp", 3),
        ("media/libaudiohal/impl/StreamHalAidl.cpp", 2),
    ]


def test_deleted_file_and_binary_files_differ() -> None:
    text = (
        "diff --git a/old.txt b/old.txt\n"
        "deleted file mode 100644\n"
        "index 1111111..0000000\n"
        "--- a/old.txt\n"
        "+++ /dev/null\n"
        "@@ -1,2 +0,0 @@\n"
        "-one\n"
        "-two\n"
        "diff --git a/logo.png b/logo.png\n"
        "new file mode 100644\n"
        "index 0000000..2222222\n"
        "Binary files /dev/null and b/logo.png differ\n"
    )
    assert parse_diff(text).files == [
        FileChange("old.txt", status="deleted", removed=2),
        FileChange("logo.png", status="added", binary=True),
    ]


def test_mode_change_and_copy_without_hunks() -> None:
    text = (
        "diff --git a/run.sh b/run.sh\n"
        "old mode 100644\n"
        "new mode 100755\n"
        "diff --git a/a.c b/b.c\n"
        "similarity index 100%\n"
        "copy from a.c\n"
        "copy to b.c\n"
    )
    assert parse_diff(text).files == [
        FileChange("run.sh"),
        FileChange("b.c", old_path="a.c", status="copied"),
    ]


def test_paths_with_spaces_and_quotes() -> None:
    text = (
        "diff --git a/res/my file.xml b/res/my file.xml\n"
        "index 1111111..2222222 100644\n"
        "--- a/res/my file.xml\n"
        "+++ b/res/my file.xml\n"
        "@@ -1 +1 @@\n"
        "-a\n"
        "+b\n"
        'diff --git "a/res/caf\\303\\251.txt" "b/res/caf\\303\\251.txt"\n'
        "old mode 100644\n"
        "new mode 100755\n"
    )
    assert [f.path for f in parse_diff(text).files] == ["res/my file.xml", "res/café.txt"]


def test_no_newline_marker_and_hunk_without_counts() -> None:
    text = "--- a/f\n+++ b/f\n@@ -1 +1 @@\n-old\n\\ No newline at end of file\n+new\n\\ No newline at end of file\n"
    assert parse_diff(text).files == [FileChange("f", added=1, removed=1)]


def test_plain_diff_timestamps_and_no_prefix() -> None:
    text = (
        "--- frameworks/base/Foo.java.orig\t2026-09-01 10:00:00\n"
        "+++ frameworks/base/Foo.java\t2026-09-01 10:05:00\n"
        "@@ -1,3 +1,3 @@\n"
        " a\n"
        "-b\n"
        "+c\n"
        " d\n"
    )
    assert parse_diff(text).files == [FileChange("frameworks/base/Foo.java", added=1, removed=1)]


@pytest.mark.parametrize(
    "name",
    ["camera-fix.v1.patch", "camera-fix.v2.patch", "settings.diff", "fix.txt", "icons.patch", "audio-linked.patch"],
)
def test_sniff_detects_fixture_diffs(name: str) -> None:
    assert looks_like_diff(fixture_text(name))


@pytest.mark.parametrize(
    "text",
    [
        fixture_text("notes.txt"),  # Has ---/+++ lines, but not a diff
        "",
        "Just some notes\n--- \n+++ \nno hunk here\n",
        "From: someone@example.com\nSubject: hi\n",
    ],
)
def test_sniff_rejects_non_diffs(text: str) -> None:
    assert not looks_like_diff(text)


def test_hunk_text() -> None:
    parsed = parse_diff(fixture_text("camera-fix.v2.patch"))
    header = parsed.files[1].hunks[0]
    assert header.splitlines()[0] == "@@ -310,5 +310,5 @@ public:"
    # Ends with the last context line, before the format-patch signature.
    assert header.splitlines()[-1] == "     status_t            dump(int fd, const Vector<String16>& args);"
    assert [len(f.hunks) for f in parse_diff(fixture_text("icons.patch")).files] == [0, 1]
    # Hunks are left out of the stored JSON unless asked for.
    assert "hunks" not in parsed.to_json()["files"][0]
    assert parsed.to_json(hunks=True)["files"][1]["hunks"] == [header]
