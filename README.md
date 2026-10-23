# aosp-migrator

Agentic migration of OEM Android framework patches from one Android version to the next.

`patchmig` evaluates each OEM patch (already upstream? obsolete? still needed?), clusters
related patches (patch, fix, improvement, revert), migrates each cluster onto the target tree
(plain merge, conflict resolution, intent-based rewrite), verifies the build and runs relevant
tests. GitHub Copilot CLI is the only agent runtime; a deterministic orchestrator owns
scheduling, the credit budget, builds and the migration ledger. Humans review every result.

Status: **M4, knowledge and analysis** (on top of M1 to M3). See the milestones below.

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
migrate analyse -c myproject/project.yaml    # upstream/obsolete/needed, clusters, forecasts
migrate clusters -c myproject/project.yaml   # clusters in migration order (--show ID for detail)
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
| `patchmig/knowledge/` | Knowledge pack: tree access, OEM footprint map, upstream change map |
| `patchmig/analysis/` | Upstream rungs, obsolete rule, revert folding, clustering, net diffs, forecasts |
| `patchmig/mcp_server/` | Read-only MCP tools for agents: trees at the migration refs, ledger, knowledge, Jira cache |
| `docs/spike-m3.md` | Steps to run the M3 spike on your machine |
| `docs/analysis-m4.md` | How to run and check the analysis on your trees |
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

## Analysis

`migrate analyse` is deterministic and spends no credits. It first builds the knowledge pack:
the OEM footprint map (files, symbols and hot spots per project) and, for every touched file,
its upstream fate between the source base and the target base (unchanged, modified, renamed,
moved to another project, deleted), with the upstream commits, changed line ranges and changed
method signatures. Then each patch gets a verdict with evidence:

| Verdict | Decided by |
| --- | --- |
| `reverted` | A revert in the patch set cancels it (a revert of a revert brings the original back) |
| `upstream` | identity: same patch-id or Change-Id in the target's history; provenance: its "cherry picked from" commit is in the target; textual: all its added lines are in the target and its removed lines gone |
| `obsolete` | Every file it touches was deleted upstream with no successor |
| `needs_decision` | It touches a path with a `needs_decision` or `forbidden` path rule |
| `needed` | Nothing above concluded; the semantic check comes with the cluster agent (M6) |

All patches are then clustered: explicit links (revert, `Fixes:`), shared Jira keys, linked
Jira tickets, symbols one patch introduces and a later one uses, and overlapping changed lines.
Oversized clusters are split at their weakest links, and clusters are ordered so dependencies
come first. For each cluster, the patches still needed are replayed on the source base to get
its net diff (fixes fold into what they fix), the net diff gets a textual upstream check of its
own, and a forecast: clean, conflict, rewrite or obsolete, with reasons.

Without shared history between the trees (for example, an imported vendor drop), the analysis
compares file contents instead; identity and provenance then rarely conclude, so more patches
stay `needed` for review.

## Test fixtures

`python -m tests.fixtures.scenario /tmp/demo` writes a `source/` tree (Android 14 base plus ten
OEM patches) and a `target/` tree (the same base plus "Android 15" upstream changes). The cases:
a conflicting signature change, a fix chain, a file moved upstream, a file deleted upstream, a
patch with its revert, a change already upstream (same patch-id), a line change upstream inside
a larger commit, a backport whose original is in the target, and a cross-project symbol
dependency. `EXPECTED` in the module lists the verdict and cluster for each.

## Milestones

| # | Milestone | Status |
| --- | --- | --- |
| M1 | Foundation: config, ledger, CLI, fixtures, CI | Done |
| M2 | Ingest adapters and patch normalisation | Done |
| M3 | Copilot CLI spike, LLM adapter, MCP tool server (gate) | Done; spike pending |
| M4 | Knowledge bootstrap and analysis | In review |
| M5 | Read-only front end | Planned |
| M6 | Cluster agent | Planned |
| M7 | Build and test over SSH | Planned |
| M8 | Publishing, review loop, Jira export | Planned |
| M9 | Migration memory and pilot | Planned |
