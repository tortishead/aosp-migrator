from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from aosp_migrator.app import create_app
from aosp_migrator.config import Config
from aosp_migrator.trees import detect_tree_type


@pytest.fixture
def client(config: Config) -> TestClient:
    return TestClient(create_app(config))


@pytest.fixture
def trees(tmp_path: Path) -> dict[str, Path]:
    repo = tmp_path / "aosp15"
    (repo / ".repo").mkdir(parents=True)
    git = tmp_path / "vendor"
    (git / ".git").mkdir(parents=True)
    plain = tmp_path / "plain"
    plain.mkdir()
    return {"repo": repo, "git": git, "plain": plain}


def payload(target: Path, **overrides: object) -> dict[str, object]:
    return {"name": "Android 15", "jql": "project = OEM", "target_tree": str(target), **overrides}


def test_tree_type_detection(tmp_path: Path, trees: dict[str, Path]) -> None:
    assert detect_tree_type(trees["repo"]) == "repo"
    assert detect_tree_type(trees["git"]) == "git"
    assert detect_tree_type(trees["plain"]) == "plain"
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    (worktree / ".git").write_text("gitdir: /elsewhere\n")
    assert detect_tree_type(worktree) == "git"


def test_crud(client: TestClient, trees: dict[str, Path]) -> None:
    assert client.get("/api/workspaces").json() == []

    created = client.post("/api/workspaces", json=payload(trees["repo"], source_tree=str(trees["git"])))
    assert created.status_code == 201
    workspace = created.json()
    assert workspace["target_tree_type"] == "repo"
    assert workspace["source_tree_type"] == "git"
    assert workspace["link_types"] == []

    workspace_id = workspace["id"]
    assert client.get(f"/api/workspaces/{workspace_id}").json() == workspace
    assert [w["id"] for w in client.get("/api/workspaces").json()] == [workspace_id]

    updated = client.put(
        f"/api/workspaces/{workspace_id}",
        json=payload(trees["plain"], name="Android 16", jql="project = NEW", source_tree=""),
    ).json()
    assert updated["name"] == "Android 16"
    assert updated["jql"] == "project = NEW"
    assert updated["target_tree"] == str(trees["plain"])
    assert updated["target_tree_type"] == "plain"
    assert updated["source_tree"] is None
    assert updated["source_tree_type"] is None

    assert client.delete(f"/api/workspaces/{workspace_id}").status_code == 204
    assert client.get(f"/api/workspaces/{workspace_id}").status_code == 404
    assert client.get("/api/workspaces").json() == []


def test_link_types_normalized(client: TestClient, trees: dict[str, Path]) -> None:
    body = client.post("/api/workspaces", json=payload(trees["repo"], link_types=[" relates to ", "", "Blocks", "RELATES TO"])).json()
    assert body["link_types"] == ["relates to", "Blocks"]
    updated = client.put(f"/api/workspaces/{body['id']}", json=payload(trees["repo"])).json()
    assert updated["link_types"] == []


def test_list_sorted_by_name(client: TestClient, trees: dict[str, Path]) -> None:
    client.post("/api/workspaces", json=payload(trees["repo"], name="b"))
    client.post("/api/workspaces", json=payload(trees["repo"], name="a"))
    assert [w["name"] for w in client.get("/api/workspaces").json()] == ["a", "b"]


def test_tilde_expanded(client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / "tree").mkdir()
    body = client.post("/api/workspaces", json=payload(Path("~/tree"))).json()
    assert body["target_tree"] == str(tmp_path / "tree")


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("target_tree", "/does/not/exist", "Target tree does not exist: /does/not/exist"),
        ("target_tree", "relative/path", "Target tree must be an absolute path: relative/path"),
        ("target_tree", "", "Target tree is required"),
        ("source_tree", "/does/not/exist", "Source tree does not exist: /does/not/exist"),
        ("name", "  ", "Name is required"),
        ("jql", "", "JQL is required"),
    ],
)
def test_validation_errors(client: TestClient, trees: dict[str, Path], field: str, value: str, message: str) -> None:
    response = client.post("/api/workspaces", json=payload(trees["repo"], **{field: value}))
    assert response.status_code == 422
    assert response.json()["detail"] == message
    assert client.get("/api/workspaces").json() == []


def test_repo_root_above_the_target_tree(client: TestClient, trees: dict[str, Path]) -> None:
    qssi = trees["repo"] / "qssi"
    qssi.mkdir()
    body = client.post("/api/workspaces", json=payload(qssi, repo_root=str(trees["repo"]))).json()
    assert (body["repo_root"], body["target_tree_type"]) == (str(trees["repo"]), "repo")
    # The `.repo` dir itself names its checkout; empty clears it.
    updated = client.put(f"/api/workspaces/{body['id']}", json=payload(qssi, repo_root=str(trees["repo"] / ".repo"))).json()
    assert updated["repo_root"] == str(trees["repo"])
    cleared = client.put(f"/api/workspaces/{body['id']}", json=payload(qssi, repo_root=" ")).json()
    assert (cleared["repo_root"], cleared["target_tree_type"]) == (None, "plain")


def test_repo_root_validation(client: TestClient, trees: dict[str, Path]) -> None:
    no_repo = client.post("/api/workspaces", json=payload(trees["plain"], repo_root=str(trees["git"])))
    assert (no_repo.status_code, no_repo.json()["detail"]) == (422, f".repo path has no .repo directory: {trees['git']}")
    elsewhere = client.post("/api/workspaces", json=payload(trees["plain"], repo_root=str(trees["repo"])))
    assert (elsewhere.status_code, elsewhere.json()["detail"]) == (422, f".repo path does not contain the target tree: {trees['repo']}")
    missing = client.post("/api/workspaces", json=payload(trees["plain"], repo_root="/does/not/exist"))
    assert missing.json()["detail"] == ".repo path does not exist: /does/not/exist"


def test_path_to_file_rejected(client: TestClient, tmp_path: Path) -> None:
    file = tmp_path / "file.txt"
    file.write_text("x")
    response = client.post("/api/workspaces", json=payload(file))
    assert response.status_code == 422
    assert response.json()["detail"] == f"Target tree is not a directory: {file}"


def test_update_validation_leaves_workspace_unchanged(client: TestClient, trees: dict[str, Path]) -> None:
    workspace = client.post("/api/workspaces", json=payload(trees["repo"])).json()
    response = client.put(f"/api/workspaces/{workspace['id']}", json=payload(Path("/does/not/exist"), name="x"))
    assert response.status_code == 422
    assert client.get(f"/api/workspaces/{workspace['id']}").json() == workspace


def test_duplicate_name_conflict(client: TestClient, trees: dict[str, Path]) -> None:
    client.post("/api/workspaces", json=payload(trees["repo"]))
    response = client.post("/api/workspaces", json=payload(trees["git"]))
    assert response.status_code == 409
    other = client.post("/api/workspaces", json=payload(trees["git"], name="other")).json()
    response = client.put(f"/api/workspaces/{other['id']}", json=payload(trees["git"]))
    assert response.status_code == 409


def test_unknown_workspace(client: TestClient, trees: dict[str, Path]) -> None:
    assert client.get("/api/workspaces/99").status_code == 404
    assert client.put("/api/workspaces/99", json=payload(trees["repo"])).status_code == 404
    assert client.delete("/api/workspaces/99").status_code == 404


def test_persists_across_restart(config: Config, trees: dict[str, Path]) -> None:
    TestClient(create_app(config)).post("/api/workspaces", json=payload(trees["repo"]))
    assert [w["name"] for w in TestClient(create_app(config)).get("/api/workspaces").json()] == ["Android 15"]
