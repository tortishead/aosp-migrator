"""Pre-scan scenario: the default tickets plus a `repo` target tree that fits them.

`prescan_store(target)` builds the tree at `target` and returns a store whose
patches were made by git in scratch projects, so they apply for real. Outcomes:

- PROJ-101 camera fix: frameworks/av via a `{AOSP_HOME}/...` hint, not present. Blocks PROJ-102.
- PROJ-102 Settings diff (rename, new file): packages/apps/Settings, not present.
- PROJ-103 plain diff: frameworks/base, present.
- PROJ-104 binary + dimens.xml: frameworks/base, partial (only dimens.xml is in).
- PROJ-105 / PROJ-106: `[PATCH 1/2]` / `[PATCH 2/2]` series in system/core; 106 calls a function 105 adds.
- PROJ-107: new AIDL interface in hardware/interfaces (`${AOSP_HOME}/...` hint; `Android.bp` alone is ambiguous). Blocks PROJ-108.
- PROJ-108: frameworks/av uses that interface (cross-repo symbol edge), same file as PROJ-101.
- PROJ-109: upstream (google.com) fix, already in frameworks/base history but the lines moved on: present by Change-Id.
- PROJ-110: only touches `Android.bp`: unmapped, three candidate projects.
"""

import tempfile
from pathlib import Path

from repo_tree import RepoTree

from aosp_migrator.importer.diffparse import parse_diff
from fake_jira.fixtures import at, default_store, fixture_file
from fake_jira.store import FakeJiraStore

OEM = "dev@oem.example.com"
UPSTREAM = "dev@google.com"

INIT_CPP = """\
#include <android-base/properties.h>

namespace android {
namespace init {

static void SetupMountNamespaces() {
    // mount namespaces
}

int SecondStageMain(int argc, char** argv) {
    SetupMountNamespaces();
    return 0;
}

}  // namespace init
}  // namespace android
"""

CAMERA_TOP = """\
#include "CameraService.h"

namespace android {

status_t CameraService::connectDevice(int cameraId) {
    sp<BasicClient> client = makeClient(cameraId);
    ALOGV("%s: camera %d", __FUNCTION__, cameraId);
    return OK;
}
"""

POWER_MANAGER = """\
package com.android.server.power;

public final class PowerManagerService {
    private void releaseWakeLockInternal(IBinder lock) {
        WakeLock wakeLock = mWakeLocks.get(lock);
        mWakeLocks.remove(lock);
    }
}
"""


def images(patch: str) -> dict[str, tuple[str, str]]:
    """{path: (before, after)} built from the hunks of `patch`, padded so offsets do not matter."""
    pad = "".join(f"// padding {n}\n" for n in range(5))
    out: dict[str, tuple[str, str]] = {}
    for change in parse_diff(patch).files:
        before, after = [], []
        for hunk in change.hunks:
            lines = [line for line in hunk.splitlines()[1:] if not line.startswith("\\")]
            before.append("".join(line[1:] + "\n" for line in lines if line[:1] in (" ", "-", "")))
            after.append("".join(line[1:] + "\n" for line in lines if line[:1] in (" ", "+", "")))
        out[change.old_path or change.path] = (pad + pad.join(before) + pad, "")
        out[change.path] = (out.get(change.path, ("", ""))[0], pad + pad.join(after) + pad)
    return out


def before(patch: str, *paths: str) -> dict[str, str | None]:
    found = images(patch)
    return {path: found[path][0] for path in paths}


def after(patch: str, *paths: str) -> dict[str, str | None]:
    found = images(patch)
    return {path: found[path][1] for path in paths}


def split_series(text: str) -> list[str]:
    """One format-patch text per commit."""
    parts = text.split("\nFrom ")
    return [parts[0] + "\n", *(f"From {part}\n" for part in parts[1:])] if len(parts) > 1 else [text]


def _source_patches(scratch: Path) -> dict[str, str]:
    """Patches made in scratch projects that start out like the target tree."""
    src = RepoTree(scratch)
    patches: dict[str, str] = {}

    src.add_project("system/core", {"init/init.cpp": INIT_CPP, "Android.bp": "cc_binary {\n    name: \"init\",\n}\n"})
    base = src.git("system/core", "rev-parse", "HEAD").strip()
    one = INIT_CPP.replace(
        "static void SetupMountNamespaces() {",
        'static void LoadOemBootProps() {\n    android::base::GetBoolProperty("persist.oem.boot.fast", false);\n}\n\nstatic void SetupMountNamespaces() {',
    )
    src.commit("system/core", {"init/init.cpp": one}, "init: add OEM boot props loader", author=OEM)
    two = one.replace("    SetupMountNamespaces();\n", "    LoadOemBootProps();\n    SetupMountNamespaces();\n")
    head = src.commit("system/core", {"init/init.cpp": two}, "init: load OEM boot props in second stage", author=OEM)
    patches["105"], patches["106"] = split_series(src.format_patch("system/core", f"{base}..{head}"))
    bp = src.commit("system/core", {"Android.bp": "cc_binary {\n    name: \"init\",\n    cflags: [\"-DOEM\"],\n}\n"}, "Build with OEM flag", author=OEM)
    patches["110"] = src.format_patch("system/core", bp)

    src.add_project("hardware/interfaces", {"Android.bp": "hidl_package_root {\n    name: \"android.hardware\",\n}\n"})
    patches["107"] = src.format_patch(
        "hardware/interfaces",
        src.commit(
            "hardware/interfaces",
            {
                "camera/oem/aidl/android/hardware/camera/oem/IOemCameraConfig.aidl": (
                    "package android.hardware.camera.oem;\n\ninterface IOemCameraConfig {\n    void setOemMode(int mode);\n}\n"
                ),
                "Android.bp": "hidl_package_root {\n    name: \"android.hardware\",\n}\n\nsubdirs = [\"camera/oem\"]\n",
            },
            "camera: add OEM camera config HAL",
            author=OEM,
        ),
    )

    src.add_project("frameworks/av", {"services/camera/libcameraservice/CameraService.cpp": CAMERA_TOP})
    patches["108"] = src.format_patch(
        "frameworks/av",
        src.commit(
            "frameworks/av",
            {
                "services/camera/libcameraservice/CameraService.cpp": CAMERA_TOP.replace(
                    "    return OK;\n}\n",
                    "    IOemCameraConfig::fromBinder(getOemBinder())->setOemMode(1);\n    return OK;\n}\n",
                    1,
                ),
            },
            "camera: enable OEM mode on connect",
            author=OEM,
        ),
    )
    return patches


def _target_tree(target: Path) -> tuple[RepoTree, str]:
    """The target tree; returns it and the upstream patch already in its history (PROJ-109)."""
    camera = fixture_file("camera-fix.v2.patch").decode()
    settings = fixture_file("settings.diff").decode()
    battery = fixture_file("fix.txt").decode()
    icons = fixture_file("icons.patch").decode()
    cpp = "services/camera/libcameraservice/CameraService.cpp"
    header = "services/camera/libcameraservice/CameraService.h"

    tree = RepoTree(target)
    tree.add_project(
        "frameworks/av",
        {cpp: CAMERA_TOP + before(camera, cpp)[cpp], header: before(camera, header)[header]},
    )
    tree.add_project(
        "frameworks/base",
        {
            **after(battery, "core/java/android/os/BatteryStats.java"),
            **after(icons, "packages/SystemUI/res/values/dimens.xml"),
            "packages/SystemUI/res/drawable-xxhdpi/oem_battery.png": "not the patched icon\n",
            "services/core/java/com/android/server/power/PowerManagerService.java": POWER_MANAGER,
        },
    )
    power = "services/core/java/com/android/server/power/PowerManagerService.java"
    fixed = POWER_MANAGER.replace("        mWakeLocks.remove(lock);\n", "        if (wakeLock == null) return;\n        mWakeLocks.remove(lock);\n")
    upstream = tree.commit("frameworks/base", {power: fixed}, "Fix wakelock leak on release", author=UPSTREAM)
    upstream_patch = tree.format_patch("frameworks/base", upstream)
    # Later local work on the same lines: reverse-apply no longer works.
    tree.commit("frameworks/base", {power: fixed.replace("if (wakeLock == null) return;", "if (wakeLock == null) {\n            return;\n        }")}, "Style")
    tree.add_project(
        "packages/apps/Settings",
        before(
            settings,
            "src/com/android/settings/display/BrightnessLevelPreferenceController.java",
            "src/com/android/settings/display/OemDisplayUtils.java",
            "Android.bp",
        ),
    )
    tree.add_project("system/core", {"init/init.cpp": INIT_CPP, "Android.bp": "cc_binary {\n    name: \"init\",\n}\n"})
    tree.add_project("hardware/interfaces", {"Android.bp": "hidl_package_root {\n    name: \"android.hardware\",\n}\n"})
    return tree, upstream_patch


def prescan_store(target: Path) -> FakeJiraStore:
    """Default tickets with pre-scan scenarios on PROJ-101 and PROJ-105..110; builds the matching tree at `target`."""
    _, upstream_patch = _target_tree(target)
    with tempfile.TemporaryDirectory() as scratch:
        patches = _source_patches(Path(scratch))
    patches["109"] = upstream_patch

    store = default_store()
    camera = store.issues["PROJ-101"]
    camera.description += "\n\nCrash in {AOSP_HOME}/frameworks/av/services/camera/libcameraservice/CameraService.cpp"
    camera.description_html += "<p>Crash in {AOSP_HOME}/frameworks/av/services/camera/libcameraservice/CameraService.cpp</p>"

    scenarios = {
        "105": ("OEM boot props loader (1/2)", "0001-init-add-OEM-boot-props-loader.patch", ""),
        "106": ("OEM boot props in second stage (2/2)", "0002-init-load-OEM-boot-props.patch", ""),
        "107": ("OEM camera config HAL", "0001-camera-add-OEM-camera-config-HAL.patch", "New HAL under ${AOSP_HOME}/hardware/interfaces/camera/oem/aidl."),
        "108": ("Enable OEM camera mode", "0001-camera-enable-OEM-mode.patch", ""),
        "109": ("Wakelock leak on release", "0001-Fix-wakelock-leak-on-release.patch", "Upstream fix, cherry-picked for the OEM build."),
        "110": ("Build init with OEM flag", "0001-Build-with-OEM-flag.patch", ""),
    }
    for n, (summary, filename, description) in scenarios.items():
        issue = store.issues[f"PROJ-{n}"]
        issue.summary = summary
        issue.description = description
        issue.description_html = f"<p>{description}</p>" if description else ""
        issue.labels = ["oem-patch"]
        store.attach(issue.key, filename, patches[n].encode(), "text/x-patch", at(10 + int(n) - 105))
    store.link("PROJ-107", "Blocks", "PROJ-108")
    return store
