"""Synthetic source and target trees for testing without real Android checkouts.

Builds two small repo-style trees, each with two projects and a manifest:

* ``source/`` — the Android 14 base plus ten OEM patches (the patch set to migrate)
* ``target/`` — the same Android 14 base plus "upstream" Android 15 changes

Both trees share byte-identical base commits (fixed authors and dates), just like real
OEM trees share AOSP history. Each OEM patch exercises one scenario from the design;
``EXPECTED`` records what the analysis should eventually conclude for it.

Run ``python -m tests.fixtures.scenario <dir>`` to write a tree for manual inspection.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

FRAMEWORKS = "frameworks/base"
CAR = "packages/services/Car"
PROJECTS = (FRAMEWORKS, CAR)

SOURCE_BASE_TAG = "android-14-base"
SOURCE_HEAD_TAG = "oem-14-head"
TARGET_BASE_TAG = "android-15-base"

POWER = "core/java/android/os/PowerHelper.java"
BT_OLD = "services/core/java/com/android/server/BtManager.java"
BT_NEW = "services/core/java/com/android/server/bluetooth/BtManager.java"
CONFIG = "core/res/res/values/config.xml"
CAR_POWER = "service/src/com/android/car/CarPowerService.java"
LEGACY_AUDIO = "service/src/com/android/car/LegacyAudio.java"
WATCHDOG = "service/src/com/android/car/CarWatchdog.java"
# Build metadata, for module and test selection (M7).
FW_BP = "Android.bp"
SERVICES_BP = "services/Android.bp"
POWER_TEST_MAPPING = "core/java/android/os/TEST_MAPPING"
CAR_BP = "service/Android.bp"
CAR_TEST_MAPPING = "service/TEST_MAPPING"

BASE_FILES = {
    FRAMEWORKS: {
        POWER: """package android.os;

public class PowerHelper {
    private int mMode;

    public void setMode(int mode) {
        mMode = mode;
    }

    public int getMode() {
        return mMode;
    }
}
""",
        BT_OLD: """package com.android.server;

public class BtManager {
    private static final int RECONNECT_TIMEOUT_MS = 5000;

    public int reconnectTimeout() {
        return RECONNECT_TIMEOUT_MS;
    }
}
""",
        FW_BP: """package {
    default_visibility: ["//visibility:public"],
}

java_library {
    name: "framework",
    srcs: ["core/java/**/*.java"],
}

filegroup {
    name: "framework-res-values",
    srcs: ["core/res/res/values/*.xml"],
}
""",
        SERVICES_BP: """java_library {
    name: "services.core",
    srcs: ["core/java/**/*.java"],
}
""",
        POWER_TEST_MAPPING: """{
  // Run for every change to android.os.
  "presubmit": [
    { "name": "FrameworksCoreTests", "options": [{ "include-filter": "android.os" }] }
  ],
  "postsubmit": [{ "name": "PowerStressTests" }]
}
""",
        CONFIG: """<?xml version="1.0" encoding="utf-8"?>
<resources>
    <integer name="config_screenBrightnessSettingDefault">102</integer>
    <bool name="config_autoBrightness">false</bool>
</resources>
""",
    },
    CAR: {
        CAR_POWER: """package com.android.car;

public class CarPowerService {
    public void onStateChanged(int state) {
        applyState(state);
    }

    private void applyState(int state) {
    }
}
""",
        CAR_BP: """android_app {
    name: "CarService",
    srcs: ["src/**/*.java"],
}
""",
        CAR_TEST_MAPPING: """{
  "presubmit": [{ "name": "CarServiceUnitTest" }]
}
""",
        LEGACY_AUDIO: """package com.android.car;

public class LegacyAudio {
    public void duck(float volume) {
        setVolume(volume);
    }

    private void setVolume(float volume) {
    }
}
""",
    },
}


@dataclass(frozen=True)
class Expected:
    """What a correct analysis concludes for one OEM patch."""

    key: str
    project: str
    verdict: str  # needed, upstream, obsolete, dropped-by-revert
    cluster: str  # patches with the same cluster name belong together
    note: str


EXPECTED = [
    Expected("P1", FRAMEWORKS, "needed", "power-mode", "conflicts: setMode() gained a parameter"),
    Expected("P2", FRAMEWORKS, "needed", "power-mode", "fix of P1, same Jira key"),
    Expected("P3", FRAMEWORKS, "needed", "bt-timeout", "file moved upstream: rename-aware apply"),
    Expected("P4", CAR, "obsolete", "audio-ducking", "LegacyAudio.java deleted upstream"),
    Expected("P5", CAR, "dropped-by-revert", "power-log", "reverted by P6"),
    Expected("P6", CAR, "dropped-by-revert", "power-log", "revert of P5"),
    Expected("P7", FRAMEWORKS, "upstream", "brightness", "identical change already upstream"),
    Expected("P8", CAR, "needed", "power-mode", "uses PowerHelper.isOemMode() added by P1"),
    Expected(
        "P9", FRAMEWORKS, "upstream", "brightness", "same line changed upstream, other patch-id"
    ),
    Expected("P10", CAR, "upstream", "watchdog", "backport whose original commit is in target"),
]


@dataclass
class Scenario:
    root: Path
    source: Path
    target: Path
    commits: dict[str, str] = field(default_factory=dict)  # patch key -> source commit sha

    def project(self, tree: Path, name: str) -> Path:
        return tree / name


class _Git:
    """Runs git with fixed identities and an advancing fake clock, for reproducible SHAs."""

    def __init__(self, start: int):
        self.clock = start

    def __call__(self, repo: Path, *args: str) -> str:
        env = {
            **os.environ,
            "GIT_AUTHOR_NAME": "AOSP Dev",
            "GIT_AUTHOR_EMAIL": "dev@android.example",
            "GIT_COMMITTER_NAME": "AOSP Dev",
            "GIT_COMMITTER_EMAIL": "dev@android.example",
            "GIT_AUTHOR_DATE": f"{self.clock} +0000",
            "GIT_COMMITTER_DATE": f"{self.clock} +0000",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_SYSTEM": os.devnull,
        }
        result = subprocess.run(
            ["git", *args], cwd=repo, env=env, check=True, capture_output=True, text=True
        )
        return result.stdout.strip()

    def commit(self, repo: Path, message: str, author: str | None = None) -> str:
        self.clock += 3600
        self(repo, "add", "-A")
        extra = ["--author", author] if author else []
        self(repo, "commit", "-q", "-m", message, *extra)
        return self(repo, "rev-parse", "HEAD")


def _write(repo: Path, rel: str, text: str) -> None:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def _edit(repo: Path, rel: str, old: str, new: str) -> None:
    path = repo / rel
    text = path.read_text()
    if old not in text:
        raise AssertionError(f"fixture edit failed: {old!r} not in {rel}")
    path.write_text(text.replace(old, new, 1))


def _manifest(tree: Path, revision: str) -> None:
    projects = "\n".join(f'  <project path="{p}" name="{p}" />' for p in PROJECTS)
    _write(
        tree,
        ".repo/manifests/default.xml",
        f"""<?xml version="1.0" encoding="UTF-8"?>
<manifest>
  <remote name="aosp" fetch="." />
  <default revision="{revision}" remote="aosp" />
{projects}
</manifest>
""",
    )


def _base(git: _Git, tree: Path, tag: str) -> None:
    for project, files in BASE_FILES.items():
        repo = tree / project
        repo.mkdir(parents=True)
        git(repo, "init", "-q", "-b", "main")
        for rel, text in files.items():
            _write(repo, rel, text)
        git.commit(repo, "Android 14 base")
        git(repo, "tag", tag)


OEM = "OEM Dev <oem.dev@oem.example>"


WATCHDOG_UPSTREAM = """package com.android.car;

public class CarWatchdog {
    private static final int TIMEOUT_MS = 3000;

    public boolean isHealthy(long lastPingMs, long nowMs) {
        return nowMs - lastPingMs < TIMEOUT_MS;
    }
}
"""


def _oem_patches(git: _Git, tree: Path, upstream: dict[str, str]) -> dict[str, str]:
    fw, car = tree / FRAMEWORKS, tree / CAR
    c: dict[str, str] = {}

    _edit(
        fw, POWER, "    private int mMode;\n", "    private int mMode;\n    private boolean mOem;\n"
    )
    _edit(
        fw, POWER, "public void setMode(int mode) {", "public synchronized void setMode(int mode) {"
    )
    _edit(
        fw,
        POWER,
        "    public int getMode() {",
        "    public boolean isOemMode() {\n        return mOem && mMode > 0;\n    }\n\n"
        "    public void setMode(int mode, boolean oem) {\n        mOem = oem;\n"
        "        setMode(mode);\n    }\n\n    public int getMode() {",
    )
    c["P1"] = git.commit(
        fw,
        "Add OEM power mode\n\nBug: OEM-101\nChange-Id: I1000000000000000000000000000000000000001",
        OEM,
    )

    _edit(fw, POWER, "return mOem && mMode > 0;", "return mOem && mMode >= 0;")
    c["P2"] = git.commit(
        fw,
        "Fix OEM power mode check for mode 0\n\nBug: OEM-101\nChange-Id: I1000000000000000000000000000000000000002",
        OEM,
    )

    _edit(fw, BT_OLD, "RECONNECT_TIMEOUT_MS = 5000", "RECONNECT_TIMEOUT_MS = 12000")
    c["P3"] = git.commit(
        fw,
        "Tune BT reconnect timeout for car head units\n\nBug: OEM-202\nChange-Id: I1000000000000000000000000000000000000003",
        OEM,
    )

    _edit(car, LEGACY_AUDIO, "        setVolume(volume);", "        setVolume(volume * 0.5f);")
    c["P4"] = git.commit(
        car,
        "Halve ducking volume in legacy audio\n\nBug: OEM-303\nChange-Id: I1000000000000000000000000000000000000004",
        OEM,
    )

    _edit(
        car,
        CAR_POWER,
        "        applyState(state);",
        '        Log.i("CarPower", "state=" + state);\n        applyState(state);',
    )
    c["P5"] = git.commit(
        car,
        "Log car power transitions\n\nBug: OEM-404\nChange-Id: I1000000000000000000000000000000000000005",
        OEM,
    )

    git(car, "revert", "--no-edit", "--no-commit", c["P5"])
    c["P6"] = git.commit(
        car,
        f'Revert "Log car power transitions"\n\nThis reverts commit {c["P5"]}.\n\n'
        "Bug: OEM-404\nChange-Id: I1000000000000000000000000000000000000006",
        OEM,
    )

    _edit(
        fw,
        CONFIG,
        '<bool name="config_autoBrightness">false</bool>',
        '<bool name="config_autoBrightness">true</bool>',
    )
    c["P7"] = git.commit(
        fw,
        "Enable auto brightness by default\n\nBug: OEM-505\nChange-Id: I1000000000000000000000000000000000000007",
        OEM,
    )

    _edit(
        car,
        CAR_POWER,
        "    private void applyState(int state) {\n    }",
        "    private void applyState(int state) {\n        if (mPowerHelper.isOemMode()) {\n"
        "            return;\n        }\n    }\n\n    private final android.os.PowerHelper mPowerHelper ="
        " new android.os.PowerHelper();",
    )
    c["P8"] = git.commit(
        car,
        "Skip state changes in OEM power mode\n\nBug: OEM-606\nChange-Id: I1000000000000000000000000000000000000008",
        OEM,
    )

    _edit(
        fw,
        CONFIG,
        '<integer name="config_screenBrightnessSettingDefault">102</integer>',
        '<integer name="config_screenBrightnessSettingDefault">128</integer>',
    )
    c["P9"] = git.commit(
        fw,
        "Raise default screen brightness\n\nBug: OEM-808\nChange-Id: I1000000000000000000000000000000000000009",
        OEM,
    )

    # A backport adapted to Android 14 (one extra line), so its patch-id differs from the
    # original; only the "cherry picked from" reference proves the original is in target.
    _write(
        car,
        WATCHDOG,
        WATCHDOG_UPSTREAM.replace(
            "public class CarWatchdog {",
            "// Backported from Android 15.\npublic class CarWatchdog {",
        ),
    )
    c["P10"] = git.commit(
        car,
        "Add car watchdog health check\n\nBug: OEM-707\n"
        "Change-Id: I1000000000000000000000000000000000000010\n"
        f"(cherry picked from commit {upstream['watchdog']})",
        OEM,
    )

    for project in PROJECTS:
        git(tree / project, "tag", SOURCE_HEAD_TAG)
    return c


def _upstream_changes(git: _Git, tree: Path) -> dict[str, str]:
    """Android 15 changes in the target; returns the SHAs OEM patches refer to."""
    fw, car = tree / FRAMEWORKS, tree / CAR
    shas: dict[str, str] = {}

    _edit(
        fw, POWER, "public void setMode(int mode) {", "public void setMode(int mode, int reason) {"
    )
    git.commit(fw, "PowerHelper: record the reason for mode changes")

    (fw / BT_NEW).parent.mkdir(parents=True, exist_ok=True)
    git(fw, "mv", BT_OLD, BT_NEW)
    _edit(fw, BT_NEW, "package com.android.server;", "package com.android.server.bluetooth;")
    git.commit(fw, "Move BtManager into the bluetooth package")

    # Same content as OEM patch P7, landed upstream independently.
    _edit(
        fw,
        CONFIG,
        '<bool name="config_autoBrightness">false</bool>',
        '<bool name="config_autoBrightness">true</bool>',
    )
    git.commit(fw, "Enable auto brightness by default")

    # Contains OEM patch P9's line change, but together with another line: other patch-id.
    _edit(
        fw,
        CONFIG,
        '    <integer name="config_screenBrightnessSettingDefault">102</integer>\n',
        '    <integer name="config_screenBrightnessSettingDefault">128</integer>\n'
        '    <integer name="config_screenOffTimeout">30000</integer>\n',
    )
    git.commit(fw, "Tune display defaults")

    git(car, "rm", "-q", LEGACY_AUDIO)
    git.commit(car, "Remove LegacyAudio, replaced by the audio HAL")

    _write(car, WATCHDOG, WATCHDOG_UPSTREAM)
    shas["watchdog"] = git.commit(car, "Add CarWatchdog health check")

    for project in PROJECTS:
        git(tree / project, "tag", TARGET_BASE_TAG)
    return shas


def build_scenario(root: Path) -> Scenario:
    """Create source and target trees under ``root`` (which must not exist or be empty)."""
    root.mkdir(parents=True, exist_ok=True)
    if any(root.iterdir()):
        raise FileExistsError(f"{root} is not empty")
    source, target = root / "source", root / "target"

    start = 1_700_000_000
    _base(_Git(start), source, SOURCE_BASE_TAG)
    _base(_Git(start), target, SOURCE_BASE_TAG)  # same clock: identical base commits
    upstream = _upstream_changes(_Git(start + 200_000), target)
    commits = _oem_patches(_Git(start + 100_000), source, upstream)
    _manifest(source, SOURCE_HEAD_TAG)
    _manifest(target, TARGET_BASE_TAG)
    return Scenario(root=root, source=source, target=target, commits=commits)


def variant(scenario: Scenario, root: Path) -> Scenario:
    """A private copy of the scenario's trees, for a test that adds a situation of its own.

    The shared scenario and EXPECTED stay as they are; the test adds commits to the copy
    (oem_commit) and proves its situation exists before testing the behaviour.
    """
    shutil.copytree(scenario.root, root, symlinks=True)
    return Scenario(root, root / "source", root / "target", dict(scenario.commits))


def oem_commit(
    scenario: Scenario, project: str, files: dict[str, str | bytes], message: str, *, at: int
) -> str:
    """Commit `files` on the source head of `project`, dated `at`, and move the head tag.

    A date older than the parent's is what a rebase or a cherry-pick leaves behind.
    """
    repo = scenario.source / project
    for rel, content in files.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_text(content)
    git = _Git(at - 3600)  # commit() advances the clock by an hour first
    sha = git.commit(repo, message, OEM)
    git(repo, "tag", "-f", SOURCE_HEAD_TAG)
    return sha


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("usage: python -m tests.fixtures.scenario <empty-dir>")
    scenario = build_scenario(Path(sys.argv[1]))
    print(f"source: {scenario.source}\ntarget: {scenario.target}")
