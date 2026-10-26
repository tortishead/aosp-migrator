import hashlib
import re
import subprocess
from pathlib import Path

import pytest
from repo_tree import RepoTree

from aosp_migrator.config import UPSTREAM_DOMAINS, Config
from aosp_migrator.prescan.presence import HISTORY_UNAVAILABLE, Presence, check_presence, combine

PROJECT = "frameworks/av"
CPP = "services/camera/CameraService.cpp"
H = "services/camera/CameraService.h"
OEM = "dev@oem.example.com"


def lines(*body: str) -> str:
    """A file with 20 filler lines around `body`, so hunks have context."""
    before = [f"// line {n}" for n in range(10)]
    after = [f"// tail {n}" for n in range(10)]
    return "\n".join([*before, *body, *after]) + "\n"


@pytest.fixture
def tree(tmp_path: Path) -> RepoTree:
    tree = RepoTree(tmp_path / "aosp")
    tree.add_project(PROJECT, {CPP: lines("int resume() {", "  return 0;", "}"), H: lines("int resume();")})
    return tree


@pytest.fixture
def project(tree: RepoTree) -> Path:
    return tree.root / PROJECT


def fix(tree: RepoTree, author: str | None = OEM, subject: str = "camera: fix resume") -> str:
    """Commit a two-file fix and return its SHA."""
    return tree.commit(
        PROJECT,
        {CPP: lines("int resume() {", "  if (!client) return -1;", "  return 0;", "}"), H: lines("int resume() const;")},
        subject,
        author,
    )


def drift(tree: RepoTree) -> None:
    """Change the lines the fix touches, so the fix no longer reverse-applies."""
    tree.commit(PROJECT, {CPP: lines("int resume() {", "  if (!client) return -2;", "  return 0;", "}"), H: lines("long resume() const;")}, "camera: drift")


def snapshot(root: Path) -> str:
    """Hash of every file under `root`, .git included, to prove a scan wrote nothing."""
    digest = hashlib.sha256()
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        digest.update(str(path.relative_to(root)).encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def test_present(tree: RepoTree, project: Path) -> None:
    patch = tree.format_patch(PROJECT, fix(tree))
    assert check_presence(project, patch) == Presence("present", {"files": {CPP: "present", H: "present"}})


def test_not_present(tree: RepoTree, project: Path) -> None:
    patch = tree.format_patch(PROJECT, fix(tree))
    tree.git(PROJECT, "reset", "-q", "--hard", "HEAD~1")
    assert check_presence(project, patch) == Presence("not_present", {"files": {CPP: "not_present", H: "not_present"}})


def test_partial(tree: RepoTree, project: Path) -> None:
    patch = tree.format_patch(PROJECT, fix(tree))
    tree.commit(PROJECT, {H: lines("int resume();")}, "camera: revert header")
    result = check_presence(project, patch)
    assert result.status == "partial"
    assert result.detail["files"] == {CPP: "present", H: "not_present"}


def test_upstream_found_by_change_id_after_drift(tree: RepoTree, project: Path) -> None:
    sha = fix(tree, author=None)
    patch = tree.format_patch(PROJECT, sha)
    drift(tree)
    result = check_presence(project, patch)
    assert result.status == "present"
    assert result.detail["history"] == [{"subject": "camera: fix resume", "match": "change_id", "sha": sha}]


def test_upstream_found_by_subject(tree: RepoTree, project: Path) -> None:
    sha = fix(tree, author=None)
    # Re-uploaded under a new Change-Id.
    patch = re.sub(r"Change-Id: I\w+", "Change-Id: I" + "0" * 40, tree.format_patch(PROJECT, sha))
    drift(tree)
    result = check_presence(project, patch)
    assert result.detail["history"] == [{"subject": "camera: fix resume", "match": "subject", "sha": sha}]


def test_upstream_found_by_patch_id(tree: RepoTree, project: Path) -> None:
    sha = fix(tree, author=None)
    patch = tree.format_patch(PROJECT, sha)
    patch = "\n".join(line for line in patch.splitlines() if not line.startswith("Change-Id:")) + "\n"
    patch = patch.replace("camera: fix resume", "cherry-picked: something else")
    drift(tree)
    result = check_presence(project, patch)
    assert result.status == "present"
    assert result.detail["history"][0]["match"] == "patch_id"
    assert result.detail["history"][0]["sha"] == sha


def test_upstream_not_in_history(tree: RepoTree, project: Path) -> None:
    patch = tree.format_patch(PROJECT, fix(tree, author=None))
    tree.git(PROJECT, "reset", "-q", "--hard", "HEAD~1")
    result = check_presence(project, patch)
    assert result.status == "not_present"
    assert result.detail["history"] == [{"subject": "camera: fix resume", "match": None, "sha": None}]


def test_oem_patch_skips_history(tree: RepoTree, project: Path) -> None:
    patch = tree.format_patch(PROJECT, fix(tree))
    drift(tree)
    result = check_presence(project, patch)
    assert result.status == "not_present"
    assert "history" not in result.detail


def test_upstream_domains_are_configurable(tree: RepoTree, project: Path) -> None:
    sha = fix(tree)
    patch = tree.format_patch(PROJECT, sha)
    drift(tree)
    assert check_presence(project, patch, ["example.com"]).detail["history"][0]["sha"] == sha
    assert "history" not in check_presence(project, patch, ["mple.com"]).detail


def test_shallow_history_unavailable(tree: RepoTree, tmp_path: Path) -> None:
    patch = tree.format_patch(PROJECT, fix(tree, author=None))
    drift(tree)
    shallow = tmp_path / "shallow"
    subprocess.run(["git", "clone", "-q", "--depth", "1", f"file://{tree.root / PROJECT}", str(shallow)], check=True)
    result = check_presence(shallow, patch)
    assert result.status == "unknown"
    assert result.detail["reason"] == HISTORY_UNAVAILABLE


def test_drifted_context_still_present(tree: RepoTree, project: Path) -> None:
    patch = tree.format_patch(PROJECT, fix(tree))
    # Lines two away from the change move; the changed lines stay.
    tree.commit(PROJECT, {CPP: lines("// new", "int resume() {", "  if (!client) return -1;", "  return 0;", "}").replace("// line 8", "// moved")}, "camera: drift context")
    tree.commit(PROJECT, {H: lines("int resume() const;").replace("// tail 1", "// moved")}, "camera: drift header context")
    assert check_presence(project, patch) == Presence("present", {"files": {CPP: "present", H: "present"}})


def test_shallow_but_cleanly_applicable_is_not_present(tree: RepoTree, tmp_path: Path) -> None:
    patch = tree.format_patch(PROJECT, fix(tree, author=None))
    tree.git(PROJECT, "reset", "-q", "--hard", "HEAD~1")
    tree.commit(PROJECT, {"OTHER.md": "x\n"}, "unrelated")
    shallow = tmp_path / "shallow"
    subprocess.run(["git", "clone", "-q", "--depth", "1", f"file://{tree.root / PROJECT}", str(shallow)], check=True)
    result = check_presence(shallow, patch)
    assert result.status == "not_present"
    assert "reason" not in result.detail


def test_series_with_same_file_twice(tree: RepoTree, project: Path) -> None:
    fix(tree)
    drift(tree)
    patch = tree.format_patch(PROJECT, "HEAD~2..HEAD")
    assert check_presence(project, patch).status == "present"
    tree.git(PROJECT, "reset", "-q", "--hard", "HEAD~2")
    assert check_presence(project, patch).status == "not_present"


def test_plain_diff_without_prefixes(tree: RepoTree, project: Path) -> None:
    fix(tree)
    patch = tree.git(PROJECT, "diff", "--no-prefix", "HEAD~1", "HEAD")
    assert check_presence(project, patch).status == "present"


def test_new_deleted_and_renamed_files(tree: RepoTree, project: Path) -> None:
    sha = tree.commit(PROJECT, {"NEW.md": "hello\n", H: None, "services/camera/Moved.cpp": lines("int resume() {", "  return 0;", "}"), CPP: None}, "camera: reshuffle", OEM)
    patch = tree.format_patch(PROJECT, sha)
    assert "rename from" in patch
    result = check_presence(project, patch)
    assert result.status == "present", result.detail
    tree.git(PROJECT, "reset", "-q", "--hard", "HEAD~1")
    assert check_presence(project, patch).status == "not_present"


def test_tree_unchanged_after_scan(tree: RepoTree, project: Path) -> None:
    sha = fix(tree, author=None)
    patch = tree.format_patch(PROJECT, sha)
    drift(tree)
    (project / CPP).write_text("local edit\n")  # Dirty working tree stays dirty
    before = snapshot(project)
    check_presence(project, patch)
    check_presence(project, tree.format_patch(PROJECT, "HEAD"))
    assert snapshot(project) == before


def test_combine() -> None:
    assert combine(["present", "present"]) == "present"
    assert combine(["present", "not_present"]) == "partial"
    assert combine(["partial"]) == "partial"
    assert combine(["not_present", "unknown"]) == "unknown"
    assert combine(["not_present"]) == "not_present"
    assert combine([]) == "unknown"


def test_upstream_domains_setting(config: Config, monkeypatch: pytest.MonkeyPatch) -> None:
    assert config.upstream_domains() == ["google.com", "android.com"]
    config.set({UPSTREAM_DOMAINS: "Google.com, partner.example ,"})
    assert config.upstream_domains() == ["google.com", "partner.example"]
    monkeypatch.setenv(UPSTREAM_DOMAINS.env, "x.org")
    assert config.upstream_domains() == ["x.org"]
