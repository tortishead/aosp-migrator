from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from patchmig.cli import app
from patchmig.config.schema import GithubOutput, JiraConfig, OutputConfig
from patchmig.jira_export import export, plan
from patchmig.ledger import Patch, State
from patchmig.publish import MARKER, publish
from patchmig.review import Reviewer, ReviewError, queues, sync_github
from tests.fixtures.scenario import BT_NEW, CAR, FRAMEWORKS, PROJECTS
from tests.test_migrate import answer, resolve_power
from tests.test_verify import Built

REPOS = {FRAMEWORKS: "platform_frameworks_base", CAR: "platform_packages_services_car"}


class FakeGithub:
    def __init__(self):
        self.prs: dict[str, list[dict]] = {}
        self.review_list: dict[tuple[str, int], list[dict]] = {}
        self.labels: dict[tuple[str, int], list[str]] = {}
        self.requested: dict[tuple[str, int], list[str]] = {}
        self.request_calls: list[tuple[str, int]] = []

    def find_pr(self, repo, head):
        return next((p for p in self.prs.get(repo, []) if p["head"] == head), None)

    def create_pr(self, repo, title, body, head, base, draft):
        prs = self.prs.setdefault(repo, [])
        pr = {"number": len(prs) + 1, "title": title, "body": body, "head": head, "base": base,
              "draft": draft, "state": "open", "merged": False, "merged_at": None,
              "html_url": f"https://github.example/oem/{repo}/pull/{len(prs) + 1}"}  # fmt: skip
        prs.append(pr)
        return pr

    def update_pr(self, repo, number, title, body):
        pr = self.prs[repo][number - 1]
        pr.update(title=title, body=body)
        return pr

    def get_pr(self, repo, number):
        return self.prs[repo][number - 1]

    def reviews(self, repo, number):
        return self.review_list.get((repo, number), [])

    def request_reviewers(self, repo, number, reviewers):
        self.requested[(repo, number)] = reviewers
        self.request_calls.append((repo, number))

    def add_labels(self, repo, number, labels):
        self.labels[(repo, number)] = labels


class FakeJiraWriter:
    def __init__(self):
        self.labels: dict[str, set[str]] = {}
        self.links: dict[str, dict[str, str]] = {}
        self.comment_list: dict[str, list[dict]] = {}
        self.created: list[dict] = []
        self.updates = 0

    def add_labels(self, key, labels):
        self.labels.setdefault(key, set()).update(labels)

    def comments(self, key):
        return self.comment_list.get(key, [])

    def add_comment(self, key, body):
        items = self.comment_list.setdefault(key, [])
        items.append({"id": str(len(items) + 1), "body": body})
        return items[-1]["id"]

    def update_comment(self, key, comment_id, body):
        self.updates += 1
        next(c for c in self.comment_list[key] if c["id"] == comment_id)["body"] = body

    def add_link(self, key, url, title, global_id):
        self.links.setdefault(key, {})[global_id] = url

    def create_issue(self, project, issue_type, summary, description, labels):
        key = f"{project}-{900 + len(self.created)}"
        self.created.append({"key": key, "summary": summary, "type": issue_type})
        return key


class Published(Built):
    """Migrated and verified scenario, with GitHub (local bare repos) and Jira configured."""

    def __init__(self, root: Path, verify: bool = True):
        super().__init__(root)
        hub = root / "github"
        for project, repo in REPOS.items():
            subprocess.run(["git", "clone", "-q", "--bare", str(self.scenario.target / project),
                            str(hub / f"{repo}.git")], check=True)  # fmt: skip
        github = GithubOutput(owner="oem", token="env:GH_TOKEN", repo_map=REPOS,
                              base_branch="main", git_url=str(hub / "{repo}.git"),
                              reviewers=["lead"])  # fmt: skip
        self.hub = hub
        self.config = self.config.model_copy(update={
            "output": OutputConfig(github=github),
            "jira": JiraConfig(url="https://jira.example", project="OEM", token="env:JIRA_TOKEN"),
        })  # fmt: skip
        self.config_path.write_text(
            yaml.safe_dump(self.config.model_dump(mode="json", exclude_none=True))
        )
        self.gh = FakeGithub()
        if verify:
            self.migrate(self.runner(resolve_power))
            self.verify()

    def publish(self, **kw):
        return publish(self.config, self.store, self.run, self.gh, **kw)

    def remote_head(self, project: str, branch: str) -> str:
        return subprocess.run(
            ["git", "rev-parse", branch], cwd=self.hub / f"{REPOS[project]}.git",
            capture_output=True, text=True, check=True,
        ).stdout.strip()  # fmt: skip

    def reviewer(self) -> Reviewer:
        return Reviewer(self.store, self.run)


@pytest.fixture
def published(tmp_path: Path, monkeypatch) -> Published:
    monkeypatch.setenv("GH_TOKEN", "test-token")
    return Published(tmp_path)


def test_publish_opens_one_pr_per_repo_and_is_repeatable(published):
    p = published
    report = p.publish()
    assert {o.status for o in report.outcomes} == {"published"}
    power, bt = p.cluster_of("P1"), p.cluster_of("P3")
    assert power.state == bt.state == State.VERIFIED

    prs = power.result["publish"]["projects"]
    assert set(prs) == set(PROJECTS)  # one PR per repo the cluster touches
    branch = f"migration/migrate-test/r{p.run.id}-c{power.id}"
    for project in PROJECTS:
        assert prs[project]["branch"] == branch
        head = power.result["migration"]["projects"][project]["head"]
        assert p.remote_head(project, branch) == head
    fw_pr = p.gh.get_pr(REPOS[FRAMEWORKS], prs[FRAMEWORKS]["number"])
    assert fw_pr["title"] == "[A14→A15] Add OEM power mode" and fw_pr["draft"]
    body = fw_pr["body"]
    assert "Review queue: careful review" in body and "Fix OEM power mode check" in body
    assert "`frameworks/base` (this PR): resolved" in body and "Reviewer agent" in body
    assert "Modules: `framework`" in body and prs[CAR]["url"] in body  # sibling link
    assert MARKER in body
    assert p.gh.labels[(REPOS[FRAMEWORKS], 1)] == ["migration"]
    assert p.gh.requested[(REPOS[FRAMEWORKS], 1)] == ["lead"]

    again = p.publish()
    assert {o.status for o in again.outcomes} == {"updated"}
    assert len(p.gh.prs[REPOS[FRAMEWORKS]]) == 2  # power and bt; nothing new
    bt = p.cluster_of("P3")
    assert "RECONNECT_TIMEOUT_MS = 12000" in subprocess.run(
        ["git", "show", f"migration/migrate-test/r{p.run.id}-c{bt.id}:{BT_NEW}"],
        cwd=p.hub / f"{REPOS[FRAMEWORKS]}.git", capture_output=True, text=True, check=True,
    ).stdout  # fmt: skip


def test_publish_needs_a_repo_for_every_project_and_supports_dry_run(published):
    p = published
    gh = p.config.output.github.model_copy(update={"repo_map": {FRAMEWORKS: "fw"}})
    p.config = p.config.model_copy(
        update={"output": p.config.output.model_copy(update={"github": gh})}
    )
    report = p.publish(cluster_ids=[p.cluster_of("P1").id])
    assert report.outcomes[0].status == "failed" and CAR in report.outcomes[0].reason
    dry = p.publish(cluster_ids=[p.cluster_of("P3").id], dry_run=True)
    assert dry.outcomes[0].reason.startswith("would push fw:migration/")
    assert not p.gh.prs


def test_queues_and_direct_review_actions(published):
    p = published
    q = {name: [c.id for c in cs] for name, cs in queues(p.store, p.run.id,
                                                          p.config.thresholds).items()}  # fmt: skip
    power, bt, brightness = p.cluster_of("P1"), p.cluster_of("P3"), p.cluster_of("P7")
    assert bt.id in q["quick_approve"] and power.id in q["careful_review"]
    assert brightness.id in q["dropped"]

    r = p.reviewer()
    r.approve(bt.id, "xuan", "looks right")
    assert p.cluster_of("P3").result["review"]["decision"] == "approved"
    with pytest.raises(ReviewError, match="only failed clusters retry"):
        r.retry(power.id, "xuan")
    with pytest.raises(ReviewError, match="not in run"):
        r.approve(10_000, "xuan")
    with pytest.raises(ReviewError, match="needs a reason"):
        r.reject(power.id, "xuan", " ")

    # A dropped cluster rejected = its patches are needed after all.
    r.reject(brightness.id, "xuan", "OEM brightness differs from upstream")
    assert p.patch("P7").state == State.NEEDED and p.patch("P9").state == State.NEEDED
    assert p.cluster_of("P7").state == State.NEEDED
    with pytest.raises(ReviewError, match="only verified or dropped"):
        r.approve(brightness.id, "xuan")


def test_changes_requested_on_the_pr_sends_the_cluster_back_with_feedback(published):
    p = published
    p.publish()
    bt = p.cluster_of("P3")
    pr = bt.result["publish"]["projects"][FRAMEWORKS]
    p.gh.review_list[(pr["repo"], pr["number"])] = [
        {
            "id": 7,
            "state": "CHANGES_REQUESTED",
            "user": {"login": "lead"},
            "body": "Use 15 s, our phones need it.",
        }  # fmt: skip
    ]
    changes = sync_github(p.store, p.run, p.gh)
    assert changes == [f"cluster {bt.id}: rejected by lead: Use 15 s, our phones need it. "
                       f"({pr['url']})"]  # fmt: skip
    bt = p.cluster_of("P3")
    assert bt.state == State.NEEDED and p.patch("P3").state == State.NEEDED
    assert sync_github(p.store, p.run, p.gh) == []  # each review acts once

    def follow_feedback(task, attempt):
        assert "What reviewers said about earlier attempts" in task.brief
        assert "Use 15 s" in task.brief
        path = task.workdir / FRAMEWORKS / BT_NEW
        path.write_text(path.read_text().replace("12000", "15000"))
        return answer("rewritten", confidence=0.8)

    p.migrate(p.runner(follow_feedback), cluster_ids=[bt.id])
    bt = p.cluster_of("P3")
    assert bt.state == State.REWRITTEN  # cherry-pick alone no longer counts
    assert "15000" in bt.result["migration"]["diff"]


def test_merge_or_approval_approves_and_a_closed_pr_rejects(published):
    p = published
    p.publish()
    bt, power = p.cluster_of("P3"), p.cluster_of("P1")
    bt_pr = bt.result["publish"]["projects"][FRAMEWORKS]
    p.gh.get_pr(bt_pr["repo"], bt_pr["number"]).update(merged=True, state="closed",
                                                      merged_by={"login": "lead"})  # fmt: skip
    for project in PROJECTS:
        e = power.result["publish"]["projects"][project]
        p.gh.get_pr(e["repo"], e["number"])["state"] = "closed"
    changes = sync_github(p.store, p.run, p.gh)
    assert f"cluster {bt.id}: approved by lead" in changes
    assert p.cluster_of("P3").result["review"]["source"] == "github"
    assert p.cluster_of("P1").state == State.NEEDED
    assert "PR closed without merge" in p.cluster_of("P1").result["feedback"][-1]["text"]


def test_decisions_answer_needs_decision_clusters(tmp_path, monkeypatch):
    p = Published(tmp_path, verify=False)
    question = "Keep OEM mode across suspend?"
    p.migrate(p.runner(lambda t, a: answer("needs_decision", question=question)))
    power = p.cluster_of("P1")
    assert power.state == State.NEEDS_DECISION
    r = p.reviewer()
    with pytest.raises(ReviewError, match="choice must be"):
        r.decide(power.id, "xuan", "maybe", "hm")
    r.decide(power.id, "xuan", "migrate", "Yes, keep it; reset only on cold boot.")
    power = p.cluster_of("P1")
    assert power.state == State.NEEDED and p.patch("P8").state == State.NEEDED
    assert power.result["decisions"][0]["question"] == question
    assert "reset only on cold boot" in power.result["feedback"][-1]["text"]

    p.migrate(p.runner(lambda t, a: answer("needs_decision", question=question)))
    r.decide(power.id, "xuan", "upstream", "Android 15 PowerHelper covers it.")
    power = p.cluster_of("P1")
    assert power.state == State.UPSTREAM and power.result["review"]["decision"] == "approved"


def test_jira_export_of_approved_clusters_is_idempotent(published):
    p = published
    p.publish()
    r = p.reviewer()
    bt, brightness = p.cluster_of("P3"), p.cluster_of("P7")
    r.approve(bt.id, "xuan")
    r.approve(brightness.id, "xuan", "both upstream")
    jira = FakeJiraWriter()
    report = export(p.config, p.store, p.run, jira)
    assert sorted(report.updated) == ["OEM-202", "OEM-505", "OEM-808"]
    assert jira.labels["OEM-202"] == {"mig-A14-A15-migrated"}
    assert jira.labels["OEM-505"] == {"mig-A14-A15-upstream"}
    url = bt.result["publish"]["projects"][FRAMEWORKS]["url"]
    assert list(jira.links["OEM-202"].values()) == [url]
    (comment,) = jira.comment_list["OEM-202"]
    assert comment["body"].startswith("[migration-agent] mig-A14-A15")
    assert "Tune BT reconnect timeout" in comment["body"] and f"PR: {url}" in comment["body"]
    assert "not needed: already in Android 15" in jira.comment_list["OEM-505"][0]["body"]
    assert "OEM-101" not in jira.labels  # power is not approved

    export(p.config, p.store, p.run, jira)
    assert len(jira.comment_list["OEM-202"]) == 1 and jira.updates == 0
    assert set(p.cluster_of("P3").result["jira_export"]) == {"OEM-202"}


def test_untracked_patches_get_a_tracking_ticket_once(published):
    p = published
    with p.store.session() as s, s.begin():
        s.get(Patch, p.patch("P3").id).jira_keys = []
    p.reviewer().approve(p.cluster_of("P3").id, "xuan")
    jira = FakeJiraWriter()
    report = export(p.config, p.store, p.run, jira)
    assert report.skipped and not jira.created
    (ticket,) = plan(p.config, p.store, p.run)
    assert ticket.key is None and ticket.summary.startswith("[mig-A14-A15] Tune BT")

    export(p.config, p.store, p.run, jira, create_missing=True)
    export(p.config, p.store, p.run, jira, create_missing=True)
    assert [c["key"] for c in jira.created] == ["OEM-900"]
    assert p.patch("P3").jira_keys == ["OEM-900"]


def test_cli_review_and_dry_runs(published):
    p = published
    run = CliRunner()
    cfg = ["--config", str(p.config_path)]
    out = run.invoke(app, ["review", "list", *cfg])
    assert out.exit_code == 0 and "quick approve (1)" in out.output
    bt = p.cluster_of("P3")
    out = run.invoke(app, ["review", "approve", str(bt.id), "--by", "xuan", *cfg])
    assert out.exit_code == 0 and "(approved)" in out.output
    out = run.invoke(app, ["review", "approve", str(p.cluster_of("P1").id + 100), "--by", "x",
                           *cfg])  # fmt: skip
    assert out.exit_code == 1
    out = run.invoke(app, ["jira-export", "--dry-run", *cfg])
    assert out.exit_code == 0 and "OEM-202  labels: mig-A14-A15-migrated" in out.output
    out = run.invoke(app, ["publish", "--dry-run", *cfg])
    assert out.exit_code == 0 and "would push platform_frameworks_base:migration/" in out.output


def test_web_review_view(published):
    from patchmig.web import data

    p = published
    p.publish()
    view = data.review(p.store, p.run.id)
    assert view["counts"]["quick_approve"] == 1 and view["counts"]["careful_review"] == 1
    item = view["queues"]["careful_review"][0]
    assert len(item["prs"]) == 2
    c = data.cluster(p.store, p.run.id, p.cluster_of("P1").id)
    assert c["queue"] == "careful_review" and len(c["publish"]["prs"]) == 2


def test_rework_asks_reviewers_again_and_approved_clusters_are_left_alone(published):
    p = published
    p.publish()
    bt, power = p.cluster_of("P3"), p.cluster_of("P1")
    bt_pr = bt.result["publish"]["projects"][FRAMEWORKS]
    key = (bt_pr["repo"], bt_pr["number"])
    assert p.gh.request_calls.count(key) == 1
    prs_before = {repo: len(prs) for repo, prs in p.gh.prs.items()}
    p.reviewer().approve(power.id, "lead")
    p.reviewer().reject(bt.id, "lead", "Use 15 s.")

    def rework(task, attempt):
        path = task.workdir / FRAMEWORKS / BT_NEW
        path.write_text(path.read_text().replace("12000", "15000"))
        return answer("rewritten", confidence=0.8)

    p.migrate(p.runner(rework), cluster_ids=[bt.id])
    p.verify(cluster_ids=[bt.id])
    pushed = {pr: p.remote_head(pr, f"migration/migrate-test/r{p.run.id}-c{power.id}")
              for pr in PROJECTS}  # fmt: skip
    report = p.publish()
    assert [o.cluster_id for o in report.outcomes] == [bt.id]  # approved power: not touched
    assert p.gh.request_calls.count(key) == 2  # same PR, reviewers asked again
    assert {repo: len(prs) for repo, prs in p.gh.prs.items()} == prs_before  # no new PRs
    for project, head in pushed.items():
        assert p.remote_head(project, f"migration/migrate-test/r{p.run.id}-c{power.id}") == head
