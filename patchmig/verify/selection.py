"""What to build and test for a cluster, read from the target tree at the cluster's head.

Modules: `build.module_map` globs first, else the modules of the nearest Android.bp (or
Android.mk) above each changed file. Tests: TEST_MAPPING files from each changed file's
directory up to the project root, in the configured groups, plus `tests.extra_modules`.
"""

from __future__ import annotations

import json
import posixpath
import re
from dataclasses import dataclass, field
from fnmatch import fnmatch
from pathlib import Path

from patchmig.config.schema import BuildConfig, TestsConfig
from patchmig.gitutil import file_at

_BP_BLOCK = re.compile(r"^(\w+)\s*\{(.*?)^\}", re.M | re.S)
_BP_NAME = re.compile(r'^\s*name:\s*"([^"]+)"', re.M)
_MK_MODULE = re.compile(r"^\s*LOCAL_MODULE\s*:=\s*(\S+)", re.M)
_SKIP_TYPES = {"package", "soong_namespace", "license", "license_kind"}


@dataclass
class Selection:
    modules: list[str] = field(default_factory=list)
    tests: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_json(self) -> dict:
        return {"modules": self.modules, "tests": self.tests, "notes": self.notes}


def bp_modules(text: str) -> list[str]:
    names = []
    for kind, body in _BP_BLOCK.findall(text):
        if kind in _SKIP_TYPES:
            continue
        if m := _BP_NAME.search(body):
            names.append(m.group(1))
    return names


def _parents(path: str):
    d = posixpath.dirname(path)
    while True:
        yield d
        if not d:
            return
        d = posixpath.dirname(d)


def _mapping_tests(text: str, groups: list[str]) -> list[str]:
    # TEST_MAPPING allows // comments.
    lines = [ln for ln in text.splitlines() if not ln.lstrip().startswith("//")]
    data = json.loads("\n".join(lines))
    return [t["name"] for g in groups for t in data.get(g, []) if "name" in t]


def select(
    repo: Path,
    rev: str,
    project: str,
    paths: list[str],
    build: BuildConfig,
    tests: TestsConfig,
) -> Selection:
    sel = Selection()
    for path in paths:
        tree_path = f"{project}/{path}"
        mapped = [m for g, ms in build.module_map.items() if fnmatch(tree_path, g) for m in ms]
        if mapped:
            sel.modules += mapped
        else:
            for d in _parents(path):
                bp = file_at(repo, rev, posixpath.join(d, "Android.bp"))
                if bp is not None and (names := bp_modules(bp)):
                    sel.modules += names
                    break
                mk = file_at(repo, rev, posixpath.join(d, "Android.mk"))
                if mk is not None and (names := _MK_MODULE.findall(mk)):
                    sel.modules += names
                    break
            else:
                sel.notes.append(f"no Android.bp or Android.mk above {tree_path}")
        if tests.use_test_mapping:
            for d in _parents(path):
                text = file_at(repo, rev, posixpath.join(d, "TEST_MAPPING"))
                if text is None:
                    continue
                try:
                    sel.tests += _mapping_tests(text, tests.mapping_groups)
                except (ValueError, TypeError, KeyError) as e:
                    sel.notes.append(f"{project}/{d}/TEST_MAPPING unreadable: {e}")
    sel.modules = list(dict.fromkeys(sel.modules))
    sel.tests = list(dict.fromkeys(sel.tests + tests.extra_modules))
    return sel
