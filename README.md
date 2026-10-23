# aosp-migrator

Agentic migration of OEM Android framework patches from one Android version to the next.

`patchmig` evaluates each OEM patch (already upstream? obsolete? still needed?), clusters
related patches (patch, fix, improvement, revert), migrates each cluster onto the target tree
(plain merge, conflict resolution, intent-based rewrite), verifies the build and runs relevant
tests. GitHub Copilot CLI is the only agent runtime; a deterministic orchestrator owns
scheduling, the credit budget, builds and the migration ledger. Humans review every result.

Status: **M3, Copilot CLI adapter and spike kit** (on top of M1 and M2). See the milestones below.

## Quick start

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e '.[dev]'

migrate init myproject                       # writes myproject/project.yaml
migrate config validate -c myproject/project.yaml
migrate ingest -c myproject/project.yaml     # new run: read all patch sources (+ Jira tickets)
migrate patches -c myproject/project.yaml    # list the run's patches in order
migrate doctor -c myproject/project.yaml --mcp   # check Copilot CLI headless + MCP tools
migrate intents -c myproject/project.yaml --limit 20 --export review.md
migrate status -c myproject/project.yaml
```

## Layout

| Path | Purpose |
| --- | --- |
| `patchmig/config/` | Project config schema (Pydantic), YAML loading, validation, hashing |
| `patchmig/ledger/` | Ledger database: runs, patches, clusters, state machine and audit history |
| `patchmig/ingest/` | Patch-source adapters, manifest parsing, diff footprints, normalisation and linking |
| `patchmig/gitutil.py` | Thin helpers over the git CLI |
| `patchmig/jira_client.py` | Minimal Jira REST client (read) |
| `patchmig/agents/` | LLM adapter (Copilot CLI, fake), runner with budgets and retries, intent writer, briefs |
| `patchmig/mcp_server/` | Read-only MCP tools for agents: trees at the migration refs, ledger, Jira cache |
| `docs/spike-m3.md` | Steps to run the M3 spike on your machine |
| `patchmig/templates/` | Example `project.yaml` used by `migrate init` |
| `patchmig/cli.py` | The `migrate` command |
| `tests/fixtures/scenario.py` | Generates small synthetic source/target trees with known migration cases |

## Configuration

One `project.yaml` per migration. Relative paths resolve against the file's folder. Secrets are
always references (`env:VAR` or `file:/path`); literal values are rejected. Every run stores a
snapshot and hash of the config it used. See `patchmig/templates/project.example.yaml`.

## Ingest

`migrate ingest` reads every configured patch source into a new run:

| Source | What it reads |
| --- | --- |
| `git_range` | `base_ref..head_ref` of every project in the source manifest (or a listed subset) |
| `diff_folder` | `.patch`/`.diff` files; `git format-patch` mails or plain diffs. A subfolder named after a project path assigns the project; otherwise tree-relative paths are mapped to the owning project |
| `spreadsheet` | CSV or Excel rows with a commit and/or Jira key (column names configurable) |
| `jira_query` | Commits in the source range that mention tickets returned by a JQL query |

Each patch gets its Change-Id, `git patch-id` fingerprint, Jira keys (from ticket trailers and
the subject only), a footprint (files with add/modify/delete/rename, line counts, declared and
touched symbols), and links: reverts, `Fixes:` references and cherry-pick origins. Chain roles
are `base`, `fix` or `revert`. The same patch listed by several sources is merged (by commit,
or by patch-id for diff files). Referenced Jira tickets are cached in the ledger.

## Agents

Every LLM call goes through `patchmig/agents/adapter.py`. The Copilot CLI adapter runs one
headless process per task (`-p`, `-s`, `--no-ask-user`, model, allowed and denied tools, and the
MCP config for the `patchmig` tool server); long briefs are passed as an attachment. The runner
checks the run and cluster credit budgets before each call, validates the JSON answer against
the task's schema, retries once with a correction, and logs every call (credits, duration,
output tail) in the ledger. Credits are estimates from `llm.credits_per_call`, calibrated by the
spike.

The intent writer gives each patch a short, sourced intent and reuses intents by patch
fingerprint, so a patch never costs twice, also across runs.

## Test fixtures

`python -m tests.fixtures.scenario /tmp/demo` writes a `source/` tree (Android 14 base plus eight
OEM patches) and a `target/` tree (the same base plus "Android 15" upstream changes). The cases:
a conflicting signature change, a fix chain, a file moved upstream, a file deleted upstream, a
patch with its revert, a change already upstream, and a cross-project symbol dependency.

## Milestones

| # | Milestone | Status |
| --- | --- | --- |
| M1 | Foundation: config, ledger, CLI, fixtures, CI | In review (#1) |
| M2 | Ingest adapters and patch normalisation | In review (#2) |
| M3 | Copilot CLI spike, LLM adapter, MCP tool server (gate) | In review; spike pending |
| M4 | Knowledge bootstrap and analysis | Planned |
| M5 | Read-only front end | Planned |
| M6 | Cluster agent | Planned |
| M7 | Build and test over SSH | Planned |
| M8 | Publishing, review loop, Jira export | Planned |
| M9 | Migration memory and pilot | Planned |
