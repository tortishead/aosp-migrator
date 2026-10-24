from __future__ import annotations

from patchmig.analysis.clustering import Edge, plan_clusters
from patchmig.analysis.diffs import overlaps, split_diff
from patchmig.analysis.fold import tree_relative
from patchmig.analysis.forecast import forecast
from patchmig.analysis.patches import PatchView
from patchmig.analysis.reverts import cancelled
from patchmig.knowledge.upstream import FileFate, changed_ranges, signatures
from patchmig.ledger import Patch


def _view(pid: int, project: str = "p", **links) -> PatchView:
    patch = Patch(id=pid, seq=pid, project=project, subject=f"s{pid}", commit_sha=f"c{pid}",
                  links=links, footprint={}, jira_keys=[])  # fmt: skip
    return PatchView(patch=patch, diff="", files=[])


def test_revert_of_a_revert_brings_the_original_back():
    x = _view(1, reverted_by=["c2"])
    y = _view(2, reverts="c1", reverted_by=["c3"])
    z = _view(3, reverts="c2")
    assert cancelled([x, y, z]) == {"c2": "c3", "c3": "c2"}
    # A plain pair cancels; a revert of something outside the set stays.
    a, b = _view(1, reverted_by=["c2"]), _view(2, reverts="c1")
    assert cancelled([a, b, _view(4, reverts="base-sha")]) == {"c1": "c2", "c2": "c1"}


def test_oversized_cluster_is_split_at_its_weakest_edges_and_ordered():
    views = [_view(i, project=f"p{i % 2}") for i in range(1, 6)]
    edges = [
        Edge(1, 2, "jira", "K-1"),
        Edge(2, 3, "overlap", "f.java"),  # the weak link
        Edge(3, 4, "symbol", "foo"),
        Edge(4, 5, "revert"),
    ]
    plans = plan_clusters(views, edges, max_patches=3, max_projects=3)
    assert [[m.id for m in p.members] for p in plans] == [[1, 2], [3, 4, 5]]
    assert plans[1].depends_on == {0}
    assert [e.kind for e in plans[1].cut] == ["overlap"]
    # Within limits nothing is split.
    assert len(plan_clusters(views, edges, max_patches=5, max_projects=3)) == 1


def test_project_limit_also_splits_and_dependencies_order_clusters():
    views = [_view(1, "a"), _view(2, "b"), _view(3, "c")]
    edges = [Edge(1, 3, "overlap", "x"), Edge(2, 3, "symbol", "s")]
    plans = plan_clusters(views, edges, max_patches=10, max_projects=2)
    assert [[m.id for m in p.members] for p in plans] == [[1], [2, 3]]
    assert plans[1].depends_on == {0}


def test_interleaved_split_clusters_do_not_wait_for_each_other():
    from patchmig.analysis.clustering import ClusterPlan, _order

    v = {i: _view(i) for i in range(1, 5)}
    cut = [Edge(1, 2, "overlap", "a"), Edge(3, 4, "overlap", "b")]
    x, y = ClusterPlan([v[1], v[4]], cut=cut), ClusterPlan([v[2], v[3]], cut=cut)
    plans = _order([y, x])
    assert [[m.id for m in p.members] for p in plans] == [[1, 4], [2, 3]]
    assert plans[0].depends_on == set() and plans[1].depends_on == {0}


DIFF = """diff --git a/A.java b/A.java
index 1..2 100644
--- a/A.java
+++ b/A.java
@@ -2,4 +2,5 @@ class A {
 one
-two
+TWO
+three
 four
@@ -10,0 +12,1 @@
+appended
diff --git a/old/B.java b/new/B.java
similarity index 90%
rename from old/B.java
rename to new/B.java
diff --git a/C.java b/C.java
new file mode 100644
--- /dev/null
+++ b/C.java
@@ -0,0 +1 @@
+class C {}
"""


def test_split_diff_tracks_paths_lines_and_old_side_ranges():
    a, b, c = split_diff(DIFF)
    assert (a.path, a.added, a.removed) == ("A.java", ["TWO", "three", "appended"], ["two"])
    assert a.changed == [(3, 4), (10, 11)]
    assert (b.old_path, b.new_path) == ("old/B.java", "new/B.java")
    assert (c.old_path, c.new_path, c.path) == (None, "C.java", "C.java")
    assert overlaps([(3, 4)], [(5, 6)], pad=1) and not overlaps([(3, 4)], [(6, 7)], pad=1)


def test_tree_relative_prefixes_every_path():
    out = tree_relative(DIFF, "frameworks/base")
    assert "diff --git a/frameworks/base/A.java b/frameworks/base/A.java" in out
    assert "rename from frameworks/base/old/B.java" in out
    assert "--- /dev/null" in out and "+++ b/frameworks/base/C.java" in out


def test_signatures_and_changed_ranges():
    before = "class A {\n    public void run(int a) {\n    }\n    void stop() {}\n}\n"
    after = "class A {\n    public void run(int a, int b) {\n    }\n}\n"
    assert signatures(before)["run"] == {"public void run(int a)"}
    assert "if" not in signatures("    if (x) {\n")
    assert changed_ranges(before, after) == [(2, 2), (4, 4)]


def test_forecast_levels():
    fd = split_diff(DIFF)[0]
    untouched = FileFate("p", "A.java", "modified", upstream_changed=[(20, 22)])
    assert forecast([("p", fd)], {("p", "A.java"): untouched})["level"] == "clean"
    same_lines = FileFate("p", "A.java", "modified", upstream_changed=[(4, 4)])
    assert forecast([("p", fd)], {("p", "A.java"): same_lines})["level"] == "conflict"
    moved = FileFate("p", "A.java", "moved_project", new_project="q", new_path="A.java")
    assert forecast([("p", fd)], {("p", "A.java"): moved})["level"] == "rewrite"
    gone = FileFate("p", "A.java", "deleted")
    assert forecast([("p", fd)], {("p", "A.java"): gone})["level"] == "obsolete"
    ambiguous = FileFate("p", "A.java", "deleted", candidates=["q:x/A.java", "r:A.java"])
    result = forecast([("p", fd)], {("p", "A.java"): ambiguous})
    assert result["level"] == "rewrite" and "candidates" in result["reasons"][0]
