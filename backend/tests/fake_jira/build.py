"""Build scenario: the pre-scan scenario plus a fake `build/envsetup.sh` and two changes.

- PROJ-102's new resource file carries the `FAIL_BUILD` marker: its build fails like a compile error.
- system/core moved on near PROJ-105's hunk: PROJ-105 no longer applies as is, only with a 3-way merge.
"""

from pathlib import Path

from repo_tree import RepoTree

from fake_jira.prescan import INIT_CPP, prescan_store
from fake_jira.store import FakeJiraStore

# `lunch`, `m` and `mmm` that only echo; a `FAIL_BUILD` marker anywhere in the tree fails the
# build like a compile error, a `SLOW_BUILD` one makes it hang.
ENVSETUP = """\
lunch() { echo "lunch $1"; }
_fake_build() {
    echo "$@"
    if grep -rq --exclude-dir=.git --exclude-dir=.repo --exclude-dir=build SLOW_BUILD .; then sleep 60; fi
    if grep -rq --exclude-dir=.git --exclude-dir=.repo --exclude-dir=build FAIL_BUILD .; then
        echo "packages/apps/Settings/res/values/oem_strings.xml:3: error: FAIL_BUILD marker"
        echo "FAILED: out/target/common/obj/APPS/Settings_intermediates/package-res.apk"
        echo "ninja: build stopped"
        return 1
    fi
    echo "#### build completed successfully ####"
}
m() { _fake_build m "$@"; }
mmm() { _fake_build mmm "$@"; }
"""


def write_envsetup(target: Path) -> None:
    (target / "build").mkdir()
    (target / "build" / "envsetup.sh").write_text(ENVSETUP)


def fail_settings_build(store: FakeJiraStore) -> None:
    """Put the failure marker into PROJ-102's diff."""
    for attachment in store.issues["PROJ-102"].attachments:
        if attachment.filename == "settings.diff":
            attachment.content = attachment.content.replace(b"Screen brightness", b"FAIL_BUILD")


def build_store(target: Path) -> FakeJiraStore:
    """The pre-scan scenario at `target`, set up for builds (see the module docstring)."""
    store = prescan_store(target)
    fail_settings_build(store)
    write_envsetup(target)
    # A context line of PROJ-105's hunk, two lines away from its change: no plain apply, a clean merge.
    RepoTree(target).commit("system/core", {"init/init.cpp": INIT_CPP.replace("namespace android {\n", "namespace android {  // OEM tree\n", 1)}, "init: mark OEM tree")
    return store
