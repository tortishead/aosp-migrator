# aosp-migrator

Agentic migration of OEM Android framework patches from one Android version to the next.

`patchmig` evaluates each OEM patch (already upstream? obsolete? still needed?), clusters
related patches (patch, fix, improvement, revert), migrates each cluster onto the target tree
(plain merge, conflict resolution, intent-based rewrite), verifies the build and runs relevant
tests. GitHub Copilot CLI is the only agent runtime; a deterministic orchestrator owns
scheduling, the credit budget, builds and the migration ledger. Humans review every result.

Status: **M1, foundation** (config, ledger, CLI, test fixtures). See the milestone plan below.

## Quick start

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e '.[dev]'

migrate init myproject                       # writes myproject/project.yaml
migrate config validate -c myproject/project.yaml
migrate run new -c myproject/project.yaml    # records a run with a config snapshot
migrate status -c myproject/project.yaml
```

## Layout

| Path | Purpose |
| --- | --- |
| `patchmig/config/` | Project config schema (Pydantic), YAML loading, validation, hashing |
| `patchmig/ledger/` | Ledger database: runs, patches, clusters, state machine and audit history |
| `patchmig/templates/` | Example `project.yaml` used by `migrate init` |
| `patchmig/cli.py` | The `migrate` command |
| `tests/fixtures/scenario.py` | Generates small synthetic source/target trees with known migration cases |

## Configuration

One `project.yaml` per migration. Relative paths resolve against the file's folder. Secrets are
always references (`env:VAR` or `file:/path`); literal values are rejected. Every run stores a
snapshot and hash of the config it used. See `patchmig/templates/project.example.yaml`.

## Test fixtures

`python -m tests.fixtures.scenario /tmp/demo` writes a `source/` tree (Android 14 base plus eight
OEM patches) and a `target/` tree (the same base plus "Android 15" upstream changes). The cases:
a conflicting signature change, a fix chain, a file moved upstream, a file deleted upstream, a
patch with its revert, a change already upstream, and a cross-project symbol dependency.

## Milestones

| # | Milestone | Status |
| --- | --- | --- |
| M1 | Foundation: config, ledger, CLI, fixtures, CI | In review |
| M2 | Ingest adapters and patch normalisation | Planned |
| M3 | Copilot CLI spike, LLM adapter, MCP tool server (gate) | Planned |
| M4 | Knowledge bootstrap and analysis | Planned |
| M5 | Read-only front end | Planned |
| M6 | Cluster agent | Planned |
| M7 | Build and test over SSH | Planned |
| M8 | Publishing, review loop, Jira export | Planned |
| M9 | Migration memory and pilot | Planned |
