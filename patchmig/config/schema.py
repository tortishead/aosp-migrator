"""Pydantic schema for a migration project's configuration.

One YAML file describes one migration project: where the trees are, where the patch
list comes from, how to build and test, which LLM runtime to use, and where results go.
The same schema backs the CLI, the web app's config forms and validation.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Annotated, Literal

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

AGENT_NAMES = ("intent_writer", "cluster_agent", "reviewer", "build_fixer", "memory_curator")


def _check_secret_ref(value: str) -> str:
    scheme, sep, rest = value.partition(":")
    if not sep or scheme not in ("env", "file") or not rest:
        raise ValueError(
            "secrets must be references like 'env:JIRA_TOKEN' or 'file:/path/to/key', "
            "never literal values"
        )
    return value


SecretRef = Annotated[
    str,
    AfterValidator(_check_secret_ref),
    Field(json_schema_extra={"x-secret": True}),  # the web forms offer a token store for it
]


def resolve_secret(ref: str) -> str:
    """Return the secret a reference points to. Raises KeyError or OSError if missing."""
    scheme, _, rest = ref.partition(":")
    if scheme == "env":
        return os.environ[rest]
    return Path(rest).expanduser().read_text().strip()


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class MigrationSpec(Model):
    source_version: str = Field(description="Android version of the source tree, e.g. '14'")
    target_version: str = Field(description="Android version of the target tree, e.g. '15'")
    label: str | None = Field(default=None, description="Jira label/fix version for this migration")

    @model_validator(mode="after")
    def _default_label(self) -> MigrationSpec:
        if self.label is None:
            self.label = f"mig-A{self.source_version}-A{self.target_version}"
        return self


HostKind = Literal["github", "gitlab", "bitbucket_cloud", "bitbucket_server"]


class RepoHost(Model):
    kind: HostKind = Field(description="GitHub (incl. Enterprise), GitLab, Bitbucket Cloud or "
                           "Bitbucket Server/Data Center")  # fmt: skip
    url: str = Field(description="Web base URL, e.g. https://github.com or https://git.oem.com")
    api_url: str | None = Field(default=None, description="Only if not the usual one for the kind")
    user: str | None = Field(
        default=None,
        description="User for basic auth (Bitbucket Cloud: your username); none: token only",
    )
    token: SecretRef | None = Field(
        default=None, description="Personal or project access token (read; write for pushing)"
    )


class TreeConfig(Model):
    path: Path = Field(description="Root of the checked-out tree")
    manifest: str | None = Field(
        default=None,
        description="repo manifest, relative to the tree root (e.g. .repo/manifest.xml)",
    )
    base_ref: str = Field(
        description="Tag or branch of the unmodified Android base in every project"
    )
    head_ref: str | None = Field(
        default=None, description="Tag or branch with the OEM patches (source tree only)"
    )
    remote: str | None = Field(
        default=None,
        description="Remote that `migrate history` deepens shallow projects from; default: the "
        "project's only remote",
    )
    host: RepoHost | None = Field(
        default=None,
        description="Git host of this tree's repositories. Its token is used for HTTPS fetches "
        "(migrate history) and for links to it in tickets; optional where the remotes need none",
    )


class TreesConfig(Model):
    source: TreeConfig
    target: TreeConfig
    history_commits: int = Field(
        default=20_000,
        ge=100,
        description="Target commits searched for an upstream fix when the trees share no "
        "history; also how deep `migrate history` fetches a shallow target project",
    )

    @model_validator(mode="after")
    def _source_needs_head(self) -> TreesConfig:
        if self.source.head_ref is None:
            raise ValueError(
                "trees.source.head_ref is required: it marks the end of the OEM patches"
            )
        return self


class GitRangeSource(Model):
    type: Literal["git_range"]
    projects: list[str] | Literal["all"] = "all"


class DiffFolderSource(Model):
    type: Literal["diff_folder"]
    path: Path


class SpreadsheetSource(Model):
    type: Literal["spreadsheet"]
    path: Path
    sheet: str | None = None
    columns: dict[str, str] = Field(
        default_factory=dict,
        description="Maps our fields (commit, project, jira, subject) to spreadsheet column names",
    )


class JiraQuerySource(Model):
    type: Literal["jira_query"]
    jql: str
    attachments: bool = Field(
        default=True, description="Import .diff/.patch attachments of the matching tickets"
    )
    linked_tickets: bool = Field(
        default=True,
        description="Follow linked tickets for commit and PR links (GitHub, GitLab, Bitbucket)",
    )
    max_attachment_kb: int = Field(default=2048, ge=1)


class CodeHost(RepoHost):
    repo_map: dict[str, str] = Field(
        default_factory=dict, description="Host repo (owner/name) -> source tree project path"
    )


PatchSource = Annotated[
    GitRangeSource | DiffFolderSource | SpreadsheetSource | JiraQuerySource,
    Field(discriminator="type"),
]


class JiraCloseConfig(Model):
    transition: str = Field(description="Workflow transition name, e.g. 'Close' or 'Done'")
    resolution: str | None = Field(
        default=None, description="Resolution to set if the transition screen asks, e.g. 'Won't Do'"
    )
    outcomes: list[Literal["upstream", "obsolete", "reverted"]] = Field(
        default_factory=lambda: ["upstream", "obsolete", "reverted"],
        description="Which not-needed outcomes close a ticket",
    )


class JiraConfig(Model):
    url: str
    project: str
    deployment: Literal["auto", "server", "cloud"] = Field(
        default="auto",
        description="Jira Server/Data Center or Jira Cloud; auto: cloud for *.atlassian.net",
    )
    user: str | None = Field(
        default=None, description="Basic auth user (Cloud: your account email); none: Bearer token"
    )
    token: SecretRef = Field(description="Server/DC: personal access token; Cloud: API token")
    label_prefix: str = "mig"
    comment_marker: str = "[migration-agent]"
    issue_type: str = Field(
        default="Task", description="Type of tracking tickets created for untracked patches"
    )
    close_not_needed: JiraCloseConfig | None = Field(
        default=None,
        description="Close tickets whose patches a human confirmed are not needed; off if unset",
    )


class SshConfig(Model):
    host: str
    user: str
    port: int = Field(default=22, ge=1, le=65535)
    key: SecretRef | None = None
    workdir: str = Field(description="Root of the prepared target tree on the build server")


class BuildConfig(Model):
    transport: Literal["ssh", "local"] = Field(
        default="ssh",
        description="'ssh' to a build server; 'local' when the orchestrator runs on the build host",
    )
    ssh: SshConfig | None = None
    slots: list[str] = Field(
        default_factory=list,
        description="Prepared target tree roots on the build host, one per parallel build. "
        "Default: ssh.workdir, or workdir-1..N with parallelism N",
    )
    lunch_target: str = Field(description="e.g. 'aosp_cf_x86_64_auto-trunk_staging-userdebug'")
    setup_cmd: str = Field(
        default="source build/envsetup.sh && lunch {lunch_target}",
        description="Run in the tree root before every build or test command",
    )
    module_build_cmd: str = "m {modules}"
    full_build_cmd: str | None = Field(
        default=None,
        description="Not used: clusters are only ever built per affected module",
        json_schema_extra={"deprecated": True},
    )
    parallelism: int = Field(default=1, ge=1, description="Parallel worktrees/builds on the server")
    timeout_minutes: int = Field(default=240, ge=1)
    module_map: dict[str, list[str]] = Field(
        default_factory=dict,
        description="Glob over tree paths -> modules to build, overriding Android.bp lookup",
    )
    log_tail_lines: int = Field(default=200, ge=10, description="Log lines kept in the ledger")

    @field_validator("module_build_cmd")
    @classmethod
    def _needs_modules_placeholder(cls, value: str) -> str:
        if "{modules}" not in value:
            raise ValueError("module_build_cmd must contain the '{modules}' placeholder")
        return value

    @model_validator(mode="after")
    def _transport_configured(self) -> BuildConfig:
        if self.transport == "ssh" and self.ssh is None:
            raise ValueError("build.transport is 'ssh' but build.ssh is missing")
        if self.transport == "local" and not self.slots:
            raise ValueError("build.transport 'local' needs build.slots (local tree roots)")
        return self

    def tree_slots(self) -> list[str]:
        if self.slots:
            return self.slots
        root = self.ssh.workdir
        if self.parallelism == 1:
            return [root]
        return [f"{root}-{i}" for i in range(1, self.parallelism + 1)]


class TestsConfig(Model):
    enabled: bool = True
    use_test_mapping: bool = True
    mapping_groups: list[str] = Field(
        default_factory=lambda: ["presubmit"], description="TEST_MAPPING groups to run"
    )
    extra_modules: list[str] = Field(default_factory=list)
    test_cmd: str = Field(default="atest {tests}", description="Must contain '{tests}'")
    max_tests: int = Field(default=20, ge=1)
    cuttlefish: bool = False
    baseline: bool = Field(
        default=True, description="Also run selected tests on the unpatched target"
    )

    @field_validator("test_cmd")
    @classmethod
    def _needs_tests_placeholder(cls, value: str) -> str:
        if "{tests}" not in value:
            raise ValueError("test_cmd must contain the '{tests}' placeholder")
        return value


class LlmConfig(Model):
    runtime: Literal["copilot-cli"] = "copilot-cli"
    executable: str = "copilot"
    github_host: str | None = Field(
        default=None,
        description="Host the Copilot licence is on: none for github.com, e.g. 'oem.ghe.com' "
        "for GHE.com, or a GitHub Enterprise Server host name",
    )
    github_token: SecretRef | None = Field(
        default=None,
        description="Fine-grained PAT with the 'Copilot Requests' permission (classic ghp_ "
        "tokens do not work); none: the CLI's own login",
    )
    models: dict[str, str] = Field(
        default_factory=dict, description="Model per agent; unset agents use the CLI default"
    )
    timeout_seconds: int = Field(default=900, ge=10)
    max_retries: int = Field(
        default=1, ge=0, description="Extra calls when an answer is not valid JSON for its schema"
    )
    credits_per_call: dict[str, float] = Field(
        default_factory=lambda: {"default": 1.0},
        description="Estimated credits per call by model name ('default' for any other); "
        "calibrate with the M3 spike",
    )
    mcp_flag: str = Field(
        default="--additional-mcp-config",
        description="Copilot CLI flag that takes an MCP server config as JSON",
    )
    inline_prompt_max_chars: int = Field(
        default=60_000, ge=1000, description="Longer briefs are passed as an attachment file"
    )
    extra_args: list[str] = Field(default_factory=list, description="Appended to every call")
    intent_max_diff_chars: int = Field(default=20_000, ge=1000)

    def credits_for(self, model: str | None) -> float:
        return self.credits_per_call.get(
            model or "default", self.credits_per_call.get("default", 1.0)
        )

    @field_validator("models")
    @classmethod
    def _known_agents(cls, value: dict[str, str]) -> dict[str, str]:
        unknown = sorted(set(value) - set(AGENT_NAMES))
        if unknown:
            raise ValueError(f"unknown agents {unknown}; known: {list(AGENT_NAMES)}")
        return value


class BudgetConfig(Model):
    run_credits: float | None = Field(default=None, gt=0, description="Cap per run; None = no cap")
    cluster_credits: float | None = Field(
        default=None,
        gt=0,
        description="Cap per cluster and run (cluster agent, reviewer and build fixes); a "
        "cluster that reaches it goes to needs_decision and the others go on",
    )
    max_fix_attempts: int = Field(
        default=3, ge=0, description="Build-fix rounds per cluster in verify"
    )


class ThresholdsConfig(Model):
    quick_approve: float = Field(default=0.85, ge=0, le=1)
    careful_review: float = Field(default=0.5, ge=0, le=1)

    @model_validator(mode="after")
    def _ordered(self) -> ThresholdsConfig:
        if self.careful_review > self.quick_approve:
            raise ValueError("careful_review must not be above quick_approve")
        return self


class RepoOutput(Model):
    owner: str = Field(
        description="GitHub owner or org, GitLab group path, Bitbucket Cloud workspace, or "
        "Bitbucket Server project key"
    )
    url: str | None = Field(
        default=None,
        description="Web base URL of the host; default: trees.target.host.url, else github.com, "
        "gitlab.com or bitbucket.org",
    )
    api_url: str | None = Field(default=None, description="Only if not the usual one for the host")
    user: str | None = Field(
        default=None, description="User for basic auth; default: trees.target.host.user"
    )
    token: SecretRef | None = Field(
        default=None,
        description="Token that may push branches and open PRs; default: trees.target.host.token",
    )
    repo_map: dict[str, str] = Field(
        default_factory=dict, description="Maps tree project paths to repository names (slugs)"
    )
    base_branch: str = Field(
        description="Branch of the target tree in these repos; PRs are opened against it"
    )
    base_branch_map: dict[str, str] = Field(
        default_factory=dict, description="Per-repo base branch, where it differs"
    )
    git_url: str | None = Field(
        default=None,
        description="Push URL template with {owner} and {repo}; default: the host's HTTPS URL",
    )
    reviewers: list[str] = Field(
        default_factory=list,
        description="Requested on every PR: user names (Bitbucket Cloud: account ids or {uuid})",
    )
    labels: list[str] = Field(
        default_factory=lambda: ["migration"], description="GitHub and GitLab only"
    )


GithubOutput = RepoOutput  # the GitHub section came first; same fields as the others


class GerritOutput(Model):
    url: str
    user: str
    token: SecretRef


class OutputConfig(Model):
    target: Literal["github", "gitlab", "bitbucket_cloud", "bitbucket_server", "gerrit"] = Field(
        default="github", description="Where PRs (GitLab: merge requests) are opened"
    )
    branch_prefix: str = "migration/"
    draft: bool = True
    github: RepoOutput | None = None
    gitlab: RepoOutput | None = None
    bitbucket_cloud: RepoOutput | None = None
    bitbucket_server: RepoOutput | None = None
    gerrit: GerritOutput | None = None

    @model_validator(mode="after")
    def _target_configured(self) -> OutputConfig:
        if getattr(self, self.target) is None:
            raise ValueError(
                f"output.target is '{self.target}' but output.{self.target} is missing"
            )
        return self


class PathRule(Model):
    pattern: str = Field(description="Glob over tree-relative paths, e.g. 'vendor/qcom/**'")
    rule: Literal["overlay_only", "needs_decision", "forbidden"]
    reason: str


class AnalysisConfig(Model):
    max_cluster_patches: int = Field(
        default=15, ge=2, description="Larger clusters are split at their weakest links"
    )
    max_cluster_projects: int = Field(default=3, ge=1)
    hot_spot_min_patches: int = Field(
        default=3, ge=2, description="A file touched by this many patches is a hot spot"
    )
    min_symbol_length: int = Field(
        default=4, ge=1, description="Shorter symbols are ignored for dependencies"
    )
    textual_min_line_chars: int = Field(
        default=6, ge=1, description="Shorter lines (braces, 'break;') don't count as evidence"
    )


class MigrateConfig(Model):
    agent: bool = Field(
        default=True, description="Call the cluster agent when cherry-pick and net-diff fail"
    )
    review: bool = Field(
        default=True, description="Have an independent reviewer agent check agent-made changes"
    )
    guardrail_retries: int = Field(
        default=1, ge=0, description="Agent retries with feedback after a guardrail failure"
    )
    max_change_ratio: float = Field(
        default=3.0, gt=0, description="Agent change may be this many times the net diff's size"
    )
    min_change_lines: int = Field(
        default=40, ge=1, description="Changed lines always allowed, whatever the ratio"
    )
    brief_max_diff_chars: int = Field(default=30_000, ge=1000)


class MemoryConfig(Model):
    path: Path | None = Field(default=None, description="Local clone of the migration memory repo")
    brief_max_chars: int = Field(
        default=4000, ge=500, description="Memory text given to the cluster agent per cluster"
    )
    max_lessons: int = Field(default=5, ge=1, description="Lessons the curator may propose per run")


class ProjectConfig(Model):
    version: Literal[1] = 1
    name: str
    workdir: Path = Field(default=Path(".patchmig"), description="Ledger, worktrees and logs")
    migration: MigrationSpec
    trees: TreesConfig
    patch_sources: list[PatchSource] = Field(min_length=1)
    jira: JiraConfig | None = None
    code_hosts: list[CodeHost] = Field(
        default_factory=list, description="Git hosts that tickets link patches on (commits, PRs)"
    )
    build: BuildConfig | None = None
    tests: TestsConfig = Field(default_factory=TestsConfig)
    llm: LlmConfig = Field(default_factory=LlmConfig)
    budget: BudgetConfig = Field(default_factory=BudgetConfig)
    thresholds: ThresholdsConfig = Field(default_factory=ThresholdsConfig)
    output: OutputConfig | None = None
    path_rules: list[PathRule] = Field(default_factory=list)
    analysis: AnalysisConfig = Field(default_factory=AnalysisConfig)
    migrate: MigrateConfig = Field(default_factory=MigrateConfig)
    memory: MemoryConfig = Field(default_factory=MemoryConfig)

    @model_validator(mode="after")
    def _jira_source_needs_jira(self) -> ProjectConfig:
        if self.jira is None and any(s.type == "jira_query" for s in self.patch_sources):
            raise ValueError("a jira_query patch source needs the 'jira' section")
        return self

    def hosts(self) -> list[RepoHost]:
        """Every configured Git host: code_hosts first, then the trees' hosts."""
        found = list(self.code_hosts)
        for tree in (self.trees.source, self.trees.target):
            if tree.host and all(h.url.rstrip("/") != tree.host.url.rstrip("/") for h in found):
                found.append(tree.host)
        return found

    @property
    def ledger_path(self) -> Path:
        return self.workdir / "ledger.db"
