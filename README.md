# aosp-migrator

Agentic migration of OEM Android framework patches from one Android version to the next.

`patchmig` evaluates each OEM patch (already upstream? obsolete? still needed?), clusters
related patches (patch, fix, improvement, revert), migrates each cluster onto the target tree
(plain merge, conflict resolution, intent-based rewrite), verifies the build and runs relevant
tests. GitHub Copilot CLI is the only agent runtime; a deterministic orchestrator owns
scheduling, the credit budget, builds and the migration ledger. Humans review every result.

Status: **M9, migration memory and pilot prep** (on top of M1 to M8). See the milestones below.

## Quick start

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e '.[dev]'

migrate init myproject                       # writes myproject/project.yaml
migrate config validate -c myproject/project.yaml
migrate jira-check -c myproject/project.yaml     # Jira sign-in, project, JQL (Server/DC or Cloud)
migrate ingest -c myproject/project.yaml     # new run: read all patch sources (+ Jira tickets)
migrate patches -c myproject/project.yaml    # list the run's patches in order
migrate doctor -c myproject/project.yaml --mcp   # check Copilot CLI headless + MCP tools
migrate analyse -c myproject/project.yaml    # upstream/obsolete/needed, clusters, forecasts
migrate clusters -c myproject/project.yaml   # clusters in migration order (--show ID for detail)
migrate intents -c myproject/project.yaml --export review.md   # needed patches only
migrate apply -c myproject/project.yaml --no-agent   # free rungs only: cherry-pick, net diff
migrate apply -c myproject/project.yaml --limit 3    # then the cluster agent where they fail
migrate build-check -c myproject/project.yaml        # SSH to the build server, slots prepared?
migrate verify -c myproject/project.yaml     # build affected modules, run TEST_MAPPING tests
migrate publish -c myproject/project.yaml    # one draft PR per verified cluster and repo
migrate review list -c myproject/project.yaml        # review queues; approve/reject/decide/sync
migrate jira-export -c myproject/project.yaml --dry-run   # approved results back to Jira
migrate serve -c myproject/project.yaml      # web front end on http://127.0.0.1:8765/
migrate export-site site -c myproject/project.yaml   # static read-only copy of the front end
migrate status -c myproject/project.yaml
```

## Layout

| Path | Purpose |
| --- | --- |
| `patchmig/config/` | Project config schema (Pydantic), YAML loading, validation, hashing |
| `patchmig/ledger/` | Ledger database: runs, patches, clusters, state machine and audit history |
| `patchmig/ingest/` | Patch-source adapters, manifest parsing, diff footprints, normalisation and linking |
| `patchmig/gitutil.py` | Thin helpers over the git CLI |
| `patchmig/jira_client.py` | Jira REST client for Server/Data Center and Cloud (reads and export writes) |
| `patchmig/agents/` | LLM adapter (Copilot CLI, fake), runner with budgets and retries, intent writer, briefs |
| `patchmig/knowledge/` | Knowledge pack: tree access, OEM footprint map, upstream change map |
| `patchmig/analysis/` | Upstream rungs, obsolete rule, revert folding, clustering, net diffs, forecasts |
| `patchmig/migrate/` | Per-cluster sparse worktrees and branches, cherry-pick and net-diff rungs, guardrails, migration orchestration |
| `patchmig/verify/` | Build host over SSH or local, module and test selection, build/test/fix loop |
| `patchmig/publish/` | GitHub REST client, branch push, one PR per cluster and repo |
| `patchmig/review.py` | Review queues, human decisions, PR review sync |
| `patchmig/jira_export.py` | Export of approved results to Jira (labels, links, one comment) |
| `patchmig/memory/` | Migration memory: a git repo of patch histories, file mappings, decisions and lessons |
| `patchmig/report.py` | Markdown summary of a run (`migrate report`) |
| `patchmig/web/` | Read-only web front end: JSON API (FastAPI), UI (`static/`), static export |
| `patchmig/mcp_server/` | Read-only MCP tools for agents: trees at the migration refs, ledger, knowledge, Jira cache |
| `docs/spike-m3.md` | Steps to run the M3 spike on your machine |
| `docs/analysis-m4.md` | How to run and check the analysis on your trees |
| `docs/migrate-m6.md` | How the migrate step works and how to try it on your trees |
| `docs/verify-m7.md` | Preparing the build server and running builds and tests |
| `docs/review-m8.md` | Publishing PRs, the review loop and the Jira export |
| `docs/pilot-m9.md` | Runbook for a pilot migration end to end, and the migration memory |
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
| `jira_query` | For each ticket a JQL query returns: commits in the source range that mention it, its `.diff`/`.patch` attachments, and commit or PR links (GitHub, GitLab, Bitbucket Cloud/Server) in it and in the tickets it links to (description, comments, remote links). Linked commits are taken from the source tree; a PR's commits too, or its diff from the host's API when they are not there (squash merge) |

Each patch gets its Change-Id, `git patch-id` fingerprint, Jira keys (from ticket trailers and
the subject only), a footprint (files with add/modify/delete/rename, line counts, declared and
touched symbols), and links: reverts, `Fixes:` references and cherry-pick origins. Chain roles
are `base`, `fix` or `revert`. The same patch listed by several sources is merged (by commit,
or by patch-id for diff files). Referenced Jira tickets are cached in the ledger with their
latest comments, together with the tickets they link to; the intent writer sees all of it.

## Agents

Every LLM call goes through `patchmig/agents/adapter.py`. The Copilot CLI adapter runs one
headless process per task (`-p`, `-s`, `--no-ask-user`, model, allowed and denied tools, and the
MCP config for the `patchmig` tool server); long briefs are passed as an attachment. The runner
checks the run and cluster credit budgets before each call, validates the JSON answer against
the task's schema, retries once with a correction, and logs every call (credits, duration,
output tail) in the ledger. Credits are estimates from `llm.credits_per_call`, calibrated by the
spike.

Models are set per agent in `llm.models` (`intent_writer`, `cluster_agent`, `reviewer`,
`build_fixer`, `memory_curator`); unset agents use the CLI default. `budget.run_credits` stops a
step for every cluster; `budget.cluster_credits` caps one cluster's agent, reviewer and build-fix
calls in a run: a cluster that reaches it goes to `needs_decision` and the other clusters go on.

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
| `needed` | Nothing above concluded; the cluster agent does the semantic check while migrating |

All patches are then clustered: explicit links (revert, `Fixes:`), shared Jira keys, linked
Jira tickets, symbols one patch introduces and a later one uses, and overlapping changed lines.
Oversized clusters are split at their weakest links, and clusters are ordered so dependencies
come first. For each cluster, the patches still needed are replayed on the source base to get
its net diff (fixes fold into what they fix), the net diff gets a textual upstream check of its
own, and a forecast: clean, conflict, rewrite or obsolete, with reasons.

Without shared history between the trees (for example, an imported vendor drop), the analysis
compares file contents instead; identity and provenance then rarely conclude, so more patches
stay `needed` for review.

## Migrate

`migrate apply` moves each needed cluster onto the target tree, in dependency order, on its own
branch `patchmig/<project name>/r<run>/c<cluster>` in every target project it touches (a dependent cluster
branches from its dependency's branch). Worktrees are sparse: only the touched directories are
checked out, under `<workdir>/worktrees/`. Per project it tries, cheapest first:

1. cherry-pick of the member commits (`-x`, original authors, rename-aware), confidence 0.9;
2. a 3-way apply of the cluster's net diff, confidence 0.85;
3. only if both fail: one cluster agent call (Copilot CLI, write tool on, shell off, the
   `patchmig` MCP tools for reading) that resolves the conflict markers or rewrites the change
   for the same intent.

The agent's edits must pass guardrails (no conflict markers, path rules, a size limit relative
to the net diff, no unexpected deletions or binaries); a failure goes back to the agent once
with the findings, then fails the cluster. The agent can also answer `upstream` (its quoted
target code is checked, otherwise the cluster waits for a human decision), `needs_decision`
with a question, or `failed`. Agent-made changes then go to an independent read-only reviewer,
whose verdict caps the confidence or rejects the change. With `--no-agent`, clusters that
need the agent stay `needed` with their conflicts recorded. Everything lands in the ledger and
in the front end's cluster page. See `docs/migrate-m6.md`.

## Verify

`migrate verify` builds each migrated cluster on the build server (SSH, or `local` on the
build host), one cluster per prepared tree slot, in parallel up to `build.parallelism`. It
builds only the modules the cluster affects (nearest `Android.bp`, or `build.module_map`),
never the whole tree. Build errors go to the cluster agent, which fixes the change in the
cluster's worktree under the same guardrails, up to `budget.max_fix_attempts`. Then the
TEST_MAPPING tests run; failures are re-run on the unpatched target so existing failures are
not blamed on the cluster. Clusters end `verified`, `failed` or `needs_decision`, with logs
on the cluster page. See `docs/verify-m7.md`.

## Publish, review and Jira export

`migrate publish` pushes each verified cluster and opens one draft PR per GitHub repository
it touches, with everything a reviewer needs in the body. Clusters sit in review queues
(quick approve, careful review, needs decision, failed, dropped, approved). Reviewers decide
on the PRs (approve or merge; request changes; close) or with `migrate review`, and
`migrate review sync` brings PR outcomes into the ledger. A rejected cluster goes back to the
cluster agent with the reviewer's words. `migrate jira-export` writes approved results to
Jira with labels, PR links and one marker comment per ticket, updated in place, and can
create tracking tickets for untracked patches. See `docs/review-m8.md`.

## Migration memory and run report

The migration memory is a git repo (`memory.path`) that carries knowledge from one migration
to the next. `migrate memory record` writes the approved results of a run: per patch (keyed
by Change-Id, also found through `Original-Change-Id`) its outcome, strategy, intent, summary,
PRs, decisions and reviewer feedback; the files that moved or disappeared upstream; and the
human decisions. `migrate memory curate` makes one agent call that turns the run's feedback,
decisions and build fixes into proposed lessons, which reach the cluster agent only after
`migrate memory approve`. In the next migration, `migrate intents` reuses remembered intents
without an agent call and the cluster agent's brief gets the patch history, mappings and the
approved lessons for the paths it touches (capped by `memory.brief_max_chars`).

`migrate report` writes a Markdown summary of a run: outcomes, deciding rungs, strategies,
review queues, builds, tests, agent calls and credits, and what needs a person. See
`docs/pilot-m9.md` for the pilot runbook.

## Front end

`migrate serve` starts a read-only web app over the project's ledger: an overview of each run
(patches by state, clusters by forecast, what decided each verdict, recent activity), the patch
list with filters and each patch's evidence, intent, history and diff, the clusters in migration
order with a graph of how their patches are linked and the folded net diff, the knowledge pack
(hot spots and the upstream fate of every touched file), agent calls with credits, and the
config snapshot of the run, plus the review queues and each cluster's PRs, decisions and Jira
export. It listens on 127.0.0.1 by default and is read-only: decisions are made on the PRs or
with `migrate review`. Editing in the browser (review buttons, config forms) needs sign-in
and is not built yet.

The UI is plain JavaScript with Preact and htm vendored as one file (`static/htm-preact.js`), so
it needs no Node.js, no build step and no CDN. `migrate export-site <folder>` writes the same UI
plus every API answer as JSON files: a frozen, read-only snapshot for any static host, such as
a private GitHub Pages site. It contains commit messages and diffs, so only publish it where
the code may be seen. Serve it over HTTP (`python -m http.server -d <folder>` for a local look);
browsers don't load the data from `file://`.

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
| M4 | Knowledge bootstrap and analysis | Done |
| M5 | Read-only front end | Done |
| M6 | Cluster agent | Done |
| M7 | Build and test over SSH | Done |
| M8 | Publishing, review loop, Jira export | Done |
| M9 | Migration memory and pilot | In review; pilot pending |
