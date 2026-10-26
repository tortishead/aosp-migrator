"""Synthetic `repo` target trees for pre-scan tests: `.repo/project.list` plus small git projects."""

import hashlib
import os
import subprocess
from pathlib import Path

# Fixed identity and dates so commit SHAs are reproducible.
_GIT_ENV = {
    "GIT_AUTHOR_NAME": "Test Author",
    "GIT_AUTHOR_EMAIL": "author@android.com",
    "GIT_COMMITTER_NAME": "Test Author",
    "GIT_COMMITTER_EMAIL": "author@android.com",
    "GIT_AUTHOR_DATE": "2026-09-01T12:00:00+00:00",
    "GIT_COMMITTER_DATE": "2026-09-01T12:00:00+00:00",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
}


def change_id(seed: str) -> str:
    return "I" + hashlib.sha1(seed.encode()).hexdigest()


class RepoTree:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.projects: list[str] = []

    def add_project(self, path: str, files: dict[str, str], subject: str = "Initial commit") -> str:
        """Create a git project at `path` with one commit of `files`; listed in project.list."""
        (self.root / path).mkdir(parents=True)
        self.git(path, "init", "-q", "-b", "main")
        self.projects.append(path)
        self._write_project_list()
        return self.commit(path, files, subject)

    def commit(self, path: str, files: dict[str, str | None], subject: str, author: str | None = None) -> str:
        """Write `files` (None deletes) into project `path` and commit with a Change-Id; return the SHA.

        `author` is an email; the default is an upstream (android.com) one.
        """
        for name, content in files.items():
            file = self.root / path / name
            if content is None:
                file.unlink()
                continue
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_text(content)
        self.git(path, "add", "-A")
        message = f"{subject}\n\nChange-Id: {change_id(path + subject)}\n"
        env = {"GIT_AUTHOR_EMAIL": author} if author else {}
        self.git(path, "commit", "-q", "-m", message, env=env)
        return self.git(path, "rev-parse", "HEAD").strip()

    def format_patch(self, path: str, revs: str) -> str:
        """`git format-patch --stdout` of `revs` (a SHA for one commit, or a range)."""
        if ".." not in revs:
            revs = f"{revs}~1..{revs}"
        return self.git(path, "format-patch", "--stdout", "-M", "--binary", revs)

    def git(self, path: str, *args: str, env: dict[str, str] | None = None) -> str:
        return subprocess.run(
            ["git", *args],
            cwd=self.root / path,
            env={**os.environ, **_GIT_ENV, **(env or {})},
            check=True,
            capture_output=True,
            text=True,
        ).stdout

    def _write_project_list(self) -> None:
        (self.root / ".repo").mkdir(exist_ok=True)
        (self.root / ".repo" / "project.list").write_text("".join(f"{p}\n" for p in self.projects))


def default_repo_tree(root: Path) -> RepoTree:
    """A few AOSP-like projects, including a nested one (`frameworks/base/...` vs `frameworks/base`)."""
    tree = RepoTree(root)
    tree.add_project(
        "frameworks/base",
        {
            "core/java/android/app/Activity.java": "class Activity {}\n",
            "services/core/Android.bp": "java_library {}\n",
        },
    )
    tree.add_project(
        "frameworks/base/packages/SystemUI",
        {"src/com/android/systemui/Dependency.java": "class Dependency {}\n"},
    )
    tree.add_project(
        "frameworks/av",
        {
            "services/camera/libcameraservice/CameraService.cpp": "// camera\n",
            "services/camera/libcameraservice/CameraService.h": "// camera\n",
        },
    )
    tree.add_project("system/core", {"init/init.cpp": "// init\n", "Android.bp": "cc_binary {}\n"})
    tree.add_project("hardware/interfaces", {"Android.bp": "hidl_package_root {}\n"})
    return tree
