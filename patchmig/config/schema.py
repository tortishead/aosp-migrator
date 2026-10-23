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

AGENT_NAMES = ("intent_writer", "cluster_agent", "reviewer", "memory_curator")


def _check_secret_ref(value: str) -> str:
    scheme, sep, rest = value.partition(":")
    if not sep or scheme not in ("env", "file") or not rest:
        raise ValueError(
            "secrets must be references like 'env:JIRA_TOKEN' or 'file:/path/to/key', "
            "never literal values"
        )
    return value


SecretRef = Annotated[str, AfterValidator(_check_secret_ref)]


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


class TreesConfig(Model):
    source: TreeConfig
    target: TreeConfig

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


PatchSource = Annotated[
    GitRangeSource | DiffFolderSource | SpreadsheetSource | JiraQuerySource,
    Field(discriminator="type"),
]


class JiraConfig(Model):
    url: str
    project: str
    user: str | None = None
    token: SecretRef
    label_prefix: str = "mig"
    comment_marker: str = "[migration-agent]"


class SshConfig(Model):
    host: str
    user: str
    port: int = Field(default=22, ge=1, le=65535)
    key: SecretRef | None = None
    workdir: str = Field(description="Root of the prepared target tree on the build server")


class BuildConfig(Model):
    ssh: SshConfig
    lunch_target: str = Field(description="e.g. 'aosp_cf_x86_64_auto-trunk_staging-userdebug'")
    module_build_cmd: str = "m {modules}"
    full_build_cmd: str = "m"
    parallelism: int = Field(default=1, ge=1, description="Parallel worktrees/builds on the server")
    timeout_minutes: int = Field(default=240, ge=1)

    @field_validator("module_build_cmd")
    @classmethod
    def _needs_modules_placeholder(cls, value: str) -> str:
        if "{modules}" not in value:
            raise ValueError("module_build_cmd must contain the '{modules}' placeholder")
        return value


class TestsConfig(Model):
    use_test_mapping: bool = True
    extra_modules: list[str] = Field(default_factory=list)
    cuttlefish: bool = False
    baseline: bool = Field(
        default=True, description="Also run selected tests on the unpatched target"
    )


class LlmConfig(Model):
    runtime: Literal["copilot-cli"] = "copilot-cli"
    executable: str = "copilot"
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
    cluster_credits: float | None = Field(default=None, gt=0)
    max_fix_attempts: int = Field(default=3, ge=0)


class ThresholdsConfig(Model):
    quick_approve: float = Field(default=0.85, ge=0, le=1)
    careful_review: float = Field(default=0.5, ge=0, le=1)

    @model_validator(mode="after")
    def _ordered(self) -> ThresholdsConfig:
        if self.careful_review > self.quick_approve:
            raise ValueError("careful_review must not be above quick_approve")
        return self


class GithubOutput(Model):
    owner: str
    token: SecretRef
    repo_map: dict[str, str] = Field(
        default_factory=dict, description="Maps tree project paths to GitHub repo names"
    )


class GerritOutput(Model):
    url: str
    user: str
    token: SecretRef


class OutputConfig(Model):
    target: Literal["github", "gerrit"] = "github"
    branch_prefix: str = "migration/"
    draft: bool = True
    github: GithubOutput | None = None
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


class ProjectConfig(Model):
    version: Literal[1] = 1
    name: str
    workdir: Path = Field(default=Path(".patchmig"), description="Ledger, worktrees and logs")
    migration: MigrationSpec
    trees: TreesConfig
    patch_sources: list[PatchSource] = Field(min_length=1)
    jira: JiraConfig | None = None
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

    @property
    def ledger_path(self) -> Path:
        return self.workdir / "ledger.db"
