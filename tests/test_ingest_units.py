from __future__ import annotations

from pathlib import Path

from patchmig.ingest.diffparse import parse_diff
from patchmig.ingest.manifest import manifest_projects, project_for_path, scan_projects
from patchmig.ingest.normalize import (
    NormalizedPatch,
    jira_keys_in,
    link_patches,
    merge_duplicates,
)
from patchmig.ingest.sources import strip_project_prefix


def test_jira_keys_only_from_trailers_and_subject():
    message = (
        "Fix crash\n\nUses UTF-8 like ANDROID-15 docs say.\n\nBug: OEM-12, OEM-13\nJira: CAR-7"
    )
    assert jira_keys_in("[OEM-11] Fix crash", message) == ["OEM-11", "OEM-12", "OEM-13", "CAR-7"]
    assert jira_keys_in("[OEM-11] Fix crash", message, {"OEM"}) == ["OEM-11", "OEM-12", "OEM-13"]


def test_parse_diff_kinds_and_symbols():
    diff = """diff --git a/A.java b/A.java
new file mode 100644
--- /dev/null
+++ b/A.java
@@ -0,0 +1,3 @@
+public class Alpha {
+    public void start(int x) {
+    }
diff --git a/old/B.java b/new/B.java
similarity index 90%
rename from old/B.java
rename to new/B.java
--- a/old/B.java
+++ b/new/B.java
@@ -3,1 +3,1 @@ public void run() {
-    a();
+    b();
diff --git a/res/values.xml b/res/values.xml
deleted file mode 100644
--- a/res/values.xml
+++ /dev/null
@@ -1 +0,0 @@
-<bool name="config_x">true</bool>
"""
    fp = parse_diff(diff)
    assert [(f.status, f.path, f.old_path) for f in fp.files] == [
        ("A", "A.java", None),
        ("R", "new/B.java", "old/B.java"),
        ("D", "res/values.xml", None),
    ]
    assert fp.files[0].added == 3 and fp.files[1].removed == 1
    assert {"Alpha", "start"} <= fp.defined
    assert "run" in fp.touched


def test_manifest_include_and_remove(tmp_path: Path):
    (tmp_path / "extra.xml").write_text(
        '<manifest><project path="vendor/oem" name="oem/vendor" /></manifest>'
    )
    (tmp_path / "default.xml").write_text(
        '<manifest><include name="extra.xml" />'
        '<project path="frameworks/base" name="platform/frameworks/base" />'
        '<project name="device/x" />'
        '<remove-project name="device/x" /></manifest>'
    )
    assert manifest_projects(tmp_path / "default.xml") == ["frameworks/base", "vendor/oem"]


def test_scan_projects_without_manifest(scenario):
    assert scan_projects(scenario.source) == ["frameworks/base", "packages/services/Car"]


def test_project_for_path_prefers_longest():
    projects = ["frameworks", "frameworks/base"]
    assert project_for_path("frameworks/base/core/X.java", projects) == "frameworks/base"
    assert project_for_path("frameworks/av/Y.cpp", projects) == "frameworks"
    assert project_for_path("system/core/Z", projects) is None


def test_strip_project_prefix_rewrites_headers_only():
    diff = (
        "diff --git a/fw/base/x.c b/fw/base/x.c\n--- a/fw/base/x.c\n+++ b/fw/base/x.c\n"
        "@@ -1 +1 @@\n-fw/base/ stays\n+fw/base/ stays too\n"
    )
    out = strip_project_prefix(diff, "fw/base")
    assert out.startswith("diff --git a/x.c b/x.c\n--- a/x.c\n+++ b/x.c\n")
    assert "-fw/base/ stays\n+fw/base/ stays too" in out


def _np(sha, subject, keys=(), reverts=None, fixes=(), fingerprint=None, source="git_range"):
    from patchmig.ingest.diffparse import Footprint

    return NormalizedPatch(
        project="p", subject=subject, message=subject, commit_sha=sha, diff_path=None,
        author=None, authored_at=None, change_id=None, fingerprint=fingerprint,
        jira_keys=list(keys), footprint=Footprint(), sources=[source], reverts=reverts,
        fixes=list(fixes),
    )  # fmt: skip


def test_links_and_roles():
    patches = [
        _np("a" * 40, "Add feature", ["OEM-1"]),
        _np("b" * 40, "Fixup feature edge case", ["OEM-1"]),
        _np("c" * 40, "Tidy code", fixes=["aaaaaaa"]),
        _np("d" * 40, 'Revert "Add feature"', reverts="aaaaaaaa"),
        _np("e" * 40, "Fix unrelated thing", ["OEM-2"]),
        _np("f" * 40, 'Revert "something old"', reverts="1234567"),
    ]
    link_patches(patches)
    roles = [p.chain_role for p in patches]
    assert roles == ["base", "fix", "fix", "revert", "base", "revert"]
    assert patches[0].links["reverted_by"] == ["d" * 40]
    assert patches[2].links["fixes"] == ["a" * 40]
    assert patches[5].links["reverts_external"] == "1234567"


def test_merge_never_joins_two_commits_but_joins_diff_and_commit():
    diff_first = _np(None, "from diff", fingerprint="f1", source="diff_folder")
    commit = _np("a" * 40, "real subject", ["OEM-1"], fingerprint="f1")
    other_commit_same_change = _np("b" * 40, "cherry-pick", fingerprint="f1")
    again = _np("a" * 40, "real subject", ["OEM-9"], fingerprint="f1", source="spreadsheet")
    merged = merge_duplicates([diff_first, commit, other_commit_same_change, again])
    assert len(merged) == 2
    first = merged[0]
    assert first.commit_sha == "a" * 40 and first.subject == "real subject"
    assert first.sources == ["diff_folder", "git_range", "spreadsheet"]
    assert first.jira_keys == ["OEM-1", "OEM-9"]
