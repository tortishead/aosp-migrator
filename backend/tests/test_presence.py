import hashlib
import re
import shutil
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


def lines(*body: str, filler: str = "line") -> str:
    """A file with 20 filler lines around `body`, so hunks have context."""
    before = [f"// {filler} {n}" for n in range(10)]
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
    result = check_presence(project, patch)
    assert result.status == "not_present"
    assert result.detail["files"] == {CPP: "not_present", H: "not_present"}
    assert result.detail["history"] == [{"subject": "camera: fix resume", "match": None, "sha": None}]


def test_partial(tree: RepoTree, project: Path) -> None:
    patch = tree.format_patch(PROJECT, fix(tree))
    tree.git(PROJECT, "reset", "-q", "--hard", "HEAD~1")
    tree.commit(PROJECT, {CPP: lines("int resume() {", "  if (!client) return -1;", "  return 0;", "}")}, "camera: cpp half only")
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


def other_fix(patch: str) -> str:
    """Same subject and Change-Id, different change: only a subject lookup could match it."""
    patch = re.sub(r"Change-Id: I\w+", "Change-Id: I" + "0" * 40, patch)
    return patch.replace("return -1;", "return -9;")


def test_oem_cherry_pick_found_by_change_id(tree: RepoTree, project: Path) -> None:
    sha = fix(tree)
    patch = tree.format_patch(PROJECT, sha)
    drift(tree)
    result = check_presence(project, patch)
    assert result.status == "present"
    assert result.detail["history"] == [{"subject": "camera: fix resume", "match": "change_id", "sha": sha}]


def test_oem_patch_with_drifted_context_found_by_patch_id(tree: RepoTree, project: Path) -> None:
    patch = tree.format_patch(PROJECT, fix(tree))
    tree.git(PROJECT, "reset", "-q", "--hard", "HEAD~1")
    # Picked again after the files grew at the top: other line numbers, other Change-Id.
    header = [f"// header {n}" for n in range(30)]
    tree.commit(PROJECT, {CPP: "\n".join(header) + "\n" + lines("int resume() {", "  return 0;", "}"), H: "\n".join(header) + "\n" + lines("int resume();")}, "camera: license headers")
    sha = tree.commit(
        PROJECT,
        {CPP: "\n".join(header) + "\n" + lines("int resume() {", "  if (!client) return -1;", "  return 0;", "}"), H: "\n".join(header) + "\n" + lines("int resume() const;")},
        "camera: port resume fix",
        OEM,
    )
    tree.commit(PROJECT, {CPP: "\n".join(header) + "\n" + lines("int resume() {", "  if (!client) return -2;", "  return 0;", "}"), H: "\n".join(header) + "\n" + lines("long resume() const;")}, "camera: drift")
    result = check_presence(project, patch)
    assert result.status == "present"
    assert result.detail["history"] == [{"subject": "camera: fix resume", "match": "patch_id", "sha": sha}]


def test_oem_subject_alone_is_not_a_match(tree: RepoTree, project: Path) -> None:
    patch = other_fix(tree.format_patch(PROJECT, fix(tree)))
    drift(tree)
    result = check_presence(project, patch)
    assert result.status == "not_present"
    assert result.detail["history"] == [{"subject": "camera: fix resume", "match": None, "sha": None}]


def test_upstream_subject_match_kept(tree: RepoTree, project: Path) -> None:
    sha = fix(tree, author=None)
    patch = other_fix(tree.format_patch(PROJECT, sha))
    drift(tree)
    result = check_presence(project, patch)
    assert result.status == "present"
    assert result.detail["history"] == [{"subject": "camera: fix resume", "match": "subject", "sha": sha}]


def test_upstream_domains_are_configurable(tree: RepoTree, project: Path) -> None:
    sha = fix(tree)
    patch = other_fix(tree.format_patch(PROJECT, sha))
    drift(tree)
    assert check_presence(project, patch, ["example.com"]).detail["history"][0]["sha"] == sha
    assert check_presence(project, patch, ["mple.com"]).detail["history"][0]["sha"] is None


@pytest.mark.parametrize("author", [None, OEM])
def test_shallow_history_unavailable(tree: RepoTree, tmp_path: Path, author: str | None) -> None:
    patch = tree.format_patch(PROJECT, fix(tree, author=author))
    drift(tree)
    shallow = tmp_path / "shallow"
    subprocess.run(["git", "clone", "-q", "--depth", "1", f"file://{tree.root / PROJECT}", str(shallow)], check=True)
    result = check_presence(shallow, patch)
    assert result.status == "unknown"
    assert result.detail["reason"] == HISTORY_UNAVAILABLE


def test_plain_tree_history_unavailable(tree: RepoTree, project: Path, tmp_path: Path) -> None:
    patch = tree.format_patch(PROJECT, fix(tree))
    drift(tree)
    plain = tmp_path / "plain"
    shutil.copytree(project, plain, ignore=shutil.ignore_patterns(".git"))
    result = check_presence(plain, patch)
    assert result.status == "unknown"
    assert result.detail["reason"] == HISTORY_UNAVAILABLE


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


def drifted_target(tree: RepoTree, cpp: list[str], h: list[str]) -> None:
    """Replace the fix with a target whose context lines moved on, committed under another name."""
    tree.git(PROJECT, "reset", "-q", "--hard", "HEAD~1")
    tree.commit(PROJECT, {CPP: lines(*cpp, filler="reworded"), H: lines(*h, filler="reworded")}, "camera: port of something")


FIXED_CPP = ["int resume() {", "  if (!client) return -1;", "  return 0;", "}"]


def test_drifted_context_is_likely_present(tree: RepoTree, project: Path, tmp_path: Path) -> None:
    patch = tree.format_patch(PROJECT, fix(tree))
    drifted_target(tree, FIXED_CPP, ["int  resume()  const;"])
    result = check_presence(project, patch)
    assert result.status == "likely_present"
    assert result.detail["files"] == {CPP: "likely_present", H: "likely_present"}
    assert result.detail["history"] == [{"subject": "camera: fix resume", "match": None, "sha": None}]
    assert result.detail["heuristic"][H] == {
        "added": {"found": 1, "total": 1, "missing": []},
        "removed": {"gone": 1, "total": 1, "remaining": []},
    }
    # No history to ask: the heuristic still answers.
    plain = tmp_path / "plain"
    shutil.copytree(project, plain, ignore=shutil.ignore_patterns(".git"))
    result = check_presence(plain, patch)
    assert (result.status, result.detail["reason"]) == ("likely_present", HISTORY_UNAVAILABLE)


def test_added_line_missing_is_not_present(tree: RepoTree, project: Path) -> None:
    patch = tree.format_patch(PROJECT, fix(tree))
    drifted_target(tree, FIXED_CPP, ["long resume() const;"])
    result = check_presence(project, patch)
    assert result.status == "not_present"
    assert result.detail["files"] == {CPP: "likely_present", H: "not_present"}
    assert result.detail["heuristic"][H]["added"] == {"found": 0, "total": 1, "missing": ["int resume() const;"]}


def test_removed_line_still_there_is_not_present(tree: RepoTree, project: Path) -> None:
    patch = tree.format_patch(PROJECT, fix(tree))
    drifted_target(tree, FIXED_CPP, ["int resume() const;", "int resume();"])
    result = check_presence(project, patch)
    assert result.status == "not_present"
    assert result.detail["files"][H] == "not_present"
    assert result.detail["heuristic"][H]["removed"] == {"gone": 0, "total": 1, "remaining": ["int resume();"]}


def test_brace_only_hunks_do_not_count(tree: RepoTree, project: Path) -> None:
    patch = tree.format_patch(PROJECT, tree.commit(PROJECT, {CPP: lines("int resume() {", "  return 0;", "}", "", "};")}, "camera: braces", OEM))
    drifted_target(tree, ["int resume() {", "  return 0;", "}", "};"], ["int resume();"])
    result = check_presence(project, patch)
    assert result.status == "not_present"
    assert "heuristic" not in result.detail


def test_combine() -> None:
    assert combine(["present", "present"]) == "present"
    assert combine(["present", "likely_present"]) == "likely_present"
    assert combine(["likely_present", "not_present"]) == "not_present"
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
