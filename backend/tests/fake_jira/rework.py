"""Rework scenario: the build scenario's tickets (PROJ-102 fails to build) plus a chain of two conflicting items.

- PROJ-111 changes `mode` in system/core's `init/oem_props.cpp`, next to a line the target
  changed since: a 3-way merge leaves conflict markers. It also adds `level()` to
  `init/oem_props.h`, which applies.
- PROJ-112 changes that `level()`: written on top of PROJ-111 (a `pending` dependency),
  it applies only on PROJ-111's patch.
- Every ticket's description carries `@@fake:<key>@@`, which picks the fake Copilot's
  scenario `<key>.json` (tests write them).
"""

import tempfile
from pathlib import Path

from repo_tree import RepoTree

from fake_jira.build import fail_settings_build
from fake_jira.fixtures import at
from fake_jira.prescan import OEM, prescan_store
from fake_jira.store import FakeJiraStore

CORE = "system/core"
PROPS = "init/oem_props.cpp"
PROPS_H = "init/oem_props.h"
PROPS_TEXT = "// OEM props\nint mode = 0;\nint level = 0;\n"
PROPS_H_TEXT = "// OEM props\nint mode();\n"
# What the target made of `level` since: next to PROJ-111's line.
LOCAL = PROPS_TEXT.replace("int level = 0;", "int level = 5;")
# The region a 3-way merge of PROJ-111 leaves in the target's file (diff3 style).
REGION = "<<<<<<< ours\nint mode = 0;\nint level = 5;\n||||||| base\nint mode = 0;\nint level = 0;\n=======\nint mode = 1;\nint level = 0;\n>>>>>>> theirs\n"
RESOLVED = "int mode = 1;\nint level = 5;\n"


def marker(key: str) -> str:
    return f"@@fake:{key}@@"


def rework_store(target: Path) -> FakeJiraStore:
    store = prescan_store(target)
    fail_settings_build(store)
    tree = RepoTree(target)
    tree.commit(CORE, {PROPS: PROPS_TEXT, PROPS_H: PROPS_H_TEXT}, "init: OEM props")
    tree.commit(CORE, {PROPS: LOCAL}, "init: OEM level 5")
    with tempfile.TemporaryDirectory() as scratch:
        src = RepoTree(Path(scratch))
        src.add_project(CORE, {PROPS: PROPS_TEXT, PROPS_H: PROPS_H_TEXT})
        first = src.commit(CORE, {PROPS: PROPS_TEXT.replace("mode = 0", "mode = 1"), PROPS_H: PROPS_H_TEXT + "int level();\n"}, "init: OEM mode 1", author=OEM)
        prerequisite = src.format_patch(CORE, first)
        second = src.commit(CORE, {PROPS_H: PROPS_H_TEXT + "int level(int boost);\n"}, "init: OEM level boost", author=OEM)
        dependent = src.format_patch(CORE, second)
    for key, summary, filename, text in (
        ("PROJ-111", "OEM mode 1", "0001-init-OEM-mode-1.patch", prerequisite),
        ("PROJ-112", "OEM level boost", "0001-init-OEM-level-boost.patch", dependent),
    ):
        issue = store.issues[key]
        issue.summary = summary
        issue.labels = ["oem-patch"]
        store.attach(key, filename, text.encode(), "text/x-patch", at(20 + int(key[-1])))
    for issue in store.issues.values():
        issue.description = f"{issue.description}\n{marker(issue.key)}".lstrip()
    return store
