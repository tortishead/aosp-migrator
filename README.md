# aosp-migrator

Agentic migration of OEM Android framework patches from one Android version to the next.

`patchmig` evaluates each OEM patch (already upstream? obsolete? still needed?), clusters
related patches (patch, fix, improvement, revert), migrates each cluster onto the target tree
(plain merge, conflict resolution, intent-based rewrite), verifies the build and runs relevant
tests. GitHub Copilot CLI is the only agent runtime; a deterministic orchestrator owns
scheduling, the credit budget, builds and the migration ledger. Humans review every result.

Status: **M9, migration memory and pilot prep** (on top of M1 to M8). See the milestones below.

## Quick start

Everything below can also be done in the browser: start `migrate serve` (or install it as a
service with `migrate service-file`), set up the project under Settings, then run the steps on
the Run page and review on the Review page (see "Front end"). The command line stays for
scripts and automation; both run the same code.

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e '.[dev]'

migrate init myproject                       # writes myproject/project.yaml
migrate config validate -c myproject/project.yaml
migrate jira-check -c myproject/project.yaml     # Jira sign-in, project, JQL (Server/DC or Cloud)
migrate history -c myproject/project.yaml    # shallow (depth 1) trees: fetch the OEM range
migrate ingest -c myproject/project.yaml     # import tickets + patches into the backlog
migrate history -c myproject/project.yaml    # shallow trees: fetch target history for the checks
migrate patches -c myproject/project.yaml    # list the run's patches in order
migrate doctor -c myproject/project.yaml --mcp   # check Copilot CLI headless + MCP tools
migrate analyse -c myproject/project.yaml --export review.md  # checks, clusters; Copilot per item
migrate clusters -c myproject/project.yaml   # clusters in migration order (--show ID for detail)
migrate apply -c myproject/project.yaml --no-agent   # free rungs only: cherry-pick, net diff
migrate apply -c myproject/project.yaml --limit 3    # then the cluster agent where they fail
migrate build-check -c myproject/project.yaml        # SSH to the build server, slots prepared?
migrate verify -c myproject/project.yaml     # build affected modules, run TEST_MAPPING tests
migrate publish -c myproject/project.yaml    # one draft PR per verified cluster and repo
migrate review list -c myproject/project.yaml        # review queues; approve/reject/decide/sync
migrate jira-export -c myproject/project.yaml --dry-run   # approved results back to Jira
migrate check -c myproject/project.yaml      # trees, Git host tokens, Copilot sign-in, Jira, build host
migrate serve -c myproject/project.yaml      # web front end (every step, review, settings) on :8765
migrate service-file -c myproject/project.yaml   # systemd unit: start the front end at boot
migrate export-site site -c myproject/project.yaml   # static read-only copy of the front end
migrate status -c myproject/project.yaml
```

## Layout

| Path | Purpose |
| --- | --- |
| `patchmig/config/` | Project config schema (Pydantic), YAML loading, validation, hashing |
| `patchmig/ledger/` | Ledger database: runs, patches, clusters, state machine and audit history |
| `patchmig/ingest/` | Patch-source adapters, manifest parsing, diff footprints, normalisation and linking; import into the lasting backlog |
| `patchmig/backlog.py` | Backlog items (one per ticket, or per patch without one) and what engineers add to them |
| `patchmig/gitutil.py` | Thin helpers over the git CLI |
| `patchmig/jira_client.py` | Jira REST client for Server/Data Center and Cloud (reads and export writes) |
| `patchmig/agents/` | LLM adapter (Copilot CLI, fake), runner with budgets and retries, analyst, briefs |
| `patchmig/knowledge/` | Knowledge pack: tree access, OEM footprint map, upstream change map |
| `patchmig/analysis/` | Upstream rungs, obsolete rule, revert folding, clustering, net diffs, forecasts |
| `patchmig/migrate/` | Per-cluster sparse worktrees and branches, cherry-pick and net-diff rungs, guardrails, migration orchestration |
| `patchmig/verify/` | Build host over SSH or local, module and test selection, build/test/fix loop |
| `patchmig/publish/` | GitHub, GitLab and Bitbucket REST clients, branch push, one PR per cluster and repo |
| `patchmig/review.py` | Review queues, human decisions, PR review sync |
| `patchmig/jira_export.py` | Export of approved results to Jira (labels, links, one comment) |
| `patchmig/memory/` | Migration memory: a git repo of patch histories, file mappings, decisions and lessons |
| `patchmig/report.py` | Markdown summary of a run (`migrate report`) |
| `patchmig/steps.py` | The steps (ingest ... publish, Jira export, memory), shared by the CLI and the web jobs |
| `patchmig/web/` | Web front end: JSON API (FastAPI), background jobs, UI (`static/`), static export |
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

**[docs/settings.md](docs/settings.md) explains every field**: what it is, what to put in it
and what it is used for, section by section as on the Settings page.

## Import and backlog

`migrate ingest` (Import on the Run page) reads every configured source into the run's
backlog. The first import starts a run; every later one refreshes the same backlog: tickets are
read again, new patches and tickets are added, and patches already there keep their analysis
and state. `--new-run` starts a new backlog instead; `--item OEM-12` imports only that ticket
again, or adds a new ticket by its key.

| Source | What it reads |
| --- | --- |
| `git_range` | `base_ref..head_ref` of every project in the source manifest (or a listed subset) |
| `diff_folder` | `.patch`/`.diff` files; `git format-patch` mails or plain diffs. A subfolder named after a project path assigns the project; otherwise tree-relative paths are mapped to the owning project |
| `spreadsheet` | CSV or Excel rows with a commit and/or Jira key (column names configurable) |
| `jira_query` | For each ticket a JQL query returns: commits in the source range that mention it, its `.diff`/`.patch` attachments, and commit or PR links (GitHub, GitLab, Bitbucket Cloud/Server) in it and in the tickets it links to (description, comments, remote links). Linked commits are taken from the source tree; a PR's commits too, or its diff from the host's API when they are not there (squash merge). It works without a source checkout: commits that mention the ticket are then not searched, linked commits come from the host, and diffs are placed by the manifest (when it is readable, e.g. an absolute `manifest` path) and `repo_map` |

Each patch gets its Change-Id, `git patch-id` fingerprint, Jira keys (from ticket trailers and
the subject only), a footprint (files with add/modify/delete/rename, line counts, declared and
touched symbols), and links: reverts, `Fixes:` references and cherry-pick origins. Chain roles
are `base`, `fix` or `revert`. The same patch listed by several sources is merged (by commit,
or by patch-id for diff files). Every ticket is kept, with or without a patch: description,
status, all comments, remote links, the tickets it links to (one level) and every attachment,
downloaded up to `jira.attachment_max_kb` (larger ones are listed). The analysis sees all of it.

The backlog has one item per Jira ticket, and one per patch without a ticket. At any time an
engineer adds what the ticket leaves out, then imports and analyses the item again:

```
migrate backlog list
migrate backlog show OEM-12
migrate backlog add OEM-12 --by xuan --text "Only for the China variant"    # a note for the agents
migrate backlog add OEM-12 --by xuan --link https://github.com/oem/fw/pull/7 # becomes a patch
migrate backlog add OEM-12 --by xuan --file crash.log                       # context for the analysis
migrate backlog remove OEM-12 2 --by xuan                                   # kept in the history
migrate ingest --item OEM-12
```

Commit and PR links and `.diff`/`.patch` files added to a ticket become its patches at the next
import; other links and files are context for the analysis. The Backlog page of the web front
end shows each item with its ticket, comments, attachments, patches and additions, and has the
same form.

## Agents

Every LLM call goes through `patchmig/agents/adapter.py`. The Copilot CLI adapter runs one
headless process per task (`-p`, `-s`, `--no-ask-user`, model, allowed and denied tools, and the
MCP config for the `patchmig` tool server); long briefs are passed as an attachment. The runner
checks the run and cluster credit budgets before each call, validates the JSON answer against
the task's schema, retries once with a correction, and logs every call (credits, duration,
output tail) in the ledger. Credits are estimates from `llm.credits_per_call`, calibrated by the
spike.

Models are set per agent in `llm.models` (`analyst`, `cluster_agent`, `reviewer`,
`build_fixer`, `memory_curator`); unset agents use the CLI default. `budget.run_credits` stops a
step for every cluster; `budget.cluster_credits` caps one cluster's agent, reviewer and build-fix
calls in a run: a cluster that reaches it goes to `needs_decision` and the other clusters go on.

The analyst gets one call per backlog item that needs it (see Analysis) and writes the intent
of each of the item's patches. An item analysed with the same information in an earlier run is
reused, so it never costs twice. (`intent_writer` in an older config means the analyst.)

## Engineer notes

When the patches and tickets leave out something the agents need, add a note:

```
migrate notes add "Target is BMW QSSI 15; the OEM power HAL lives in vendor/oem/power." --by xuan
migrate notes add "Keep the old setMode(int) overload: the HMI app still calls it." \
    --patch 12 --for migrate --by xuan
migrate notes add "BT moved to packages/modules/Bluetooth." --path 'frameworks/base/services/*Bt*' --by xuan
migrate notes list [--patch N | --cluster N] / edit N --text ... / remove N   (all with --by)
```

A note applies to every patch, a path glob over `<project>/<file>`, one patch (by Change-Id or
patch-id, so it follows the patch into later runs), one cluster, or the patches of one Jira
ticket (`--ticket`); and to `--for analyse` (analyst), `--for migrate` (cluster agent,
reviewer, build fixer) or both. Notes live in the ledger, outlive runs, and keep their edit
history. The web front end shows them on patch and cluster pages.

Every result records a fingerprint of the notes (and for clusters the member intents) it was
made with, so after a change only the affected work is redone:

```
migrate notes stale            # what changed knowledge affects
migrate analyse                # analyse again the items with new information (or --item KEY)
migrate apply --stale --by xuan    # reopen and migrate only those clusters, plus clusters built on them
migrate apply --redo --cluster N --by xuan   # rerun chosen clusters regardless
```

A cluster the agent worked on is stale when any of its notes or intents changed. A cluster
applied by cherry-pick or net diff is stale only when a note about it specifically (its
patches, the cluster or a ticket) changed; such a note also brings the agent in, since a
cherry-pick cannot follow it. Approved clusters are never reopened by `--stale`. The
deterministic checks of `migrate analyse` do not read notes; its Copilot part does.

## Analysis

`migrate analyse` has two parts: deterministic checks of the patches (no credits), then
Copilot per backlog item. It can run any number of times: patches already checked keep their
verdict, and clusters already migrated or approved are left as they are.

### Checks

The checks first build the knowledge pack:
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

Run again after an import brought new patches, the checks decide only the new ones; a new
patch joins a cluster not migrated yet that it is linked to, or forms a new cluster after the
existing ones. A new patch linked to a cluster already migrated gets its own cluster, which
depends on that one, with a warning to rerun the migrated cluster if both must go together.

### Copilot: the analyst

Then the analyst gets one call per backlog item that needs it: never analysed, or given new
information since (a comment, a refreshed ticket, an added link or file, a note, a new patch).
An item whose patches the checks all dropped is not sent; `--item KEY` sends any item again.
The analyst gets everything the import kept and engineers added, the patches with their diffs
and check results, other backlog items as candidates, earlier analyses of the item and what the
migration memory remembers, and reads the target tree through the MCP tools. It answers with:

- an understanding of the item, and of each patch (written as the patch's intent);
- whether it is still needed: `needed`, `already_in_target`, `obsolete`, `not_needed` or
  `unclear`, with the reason and target code as evidence;
- related backlog items and patches (checked against the backlog);
- a solution: cherry-pick, adapt, rewrite, drop, or `needs_patch` for a ticket without one;
- what information is missing, and its confidence.

A confident "not needed" (confidence at least `thresholds.careful_review`) moves the item's
patches that the checks left `needed` to `upstream` or `obsolete`; an unsure or `unclear`
answer moves them to `needs_decision` with the missing information as the question; a later
`needed` moves them back. Verdicts of the checks themselves stay (a disagreement is recorded
on the patch), and clusters already migrated are not moved: their intents change, which makes
them stale for `migrate apply --stale`. Each analysis is kept, with the earlier ones, on the
item (`migrate backlog show KEY`, the Backlog page). `--export review.md` writes a sheet to
judge the analyses; `--no-agent` runs only the checks.

Without shared history between the trees (for example, an imported vendor drop), the analysis
compares file contents instead; identity and provenance then rarely conclude, so more patches
stay `needed` for review.

Trees synced with `repo sync --depth=1` hold one commit per project, which is not enough: the
OEM range cannot be listed, and the identity and provenance rungs cannot find a Google fix
that is already in the target. They find nothing rather than fail, so the fix looks needed.
`migrate history` deepens only what the checks need, in growing `git fetch --deepen` steps:
each source project until `base_ref..head_ref` is complete, and each target project with
patches until its history reaches the source base or `trees.history_commits` commits
(default 20000, also the identity rung's search depth without shared history). Run it before
ingest (for `git_range`) and again after ingest. It fetches from the project's only remote,
or `trees.<side>.remote`. `ingest` refuses a shallow `git_range` project and `analyse` warns
about every target project still too shallow.

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

The agents that implement and check the change (cluster agent, reviewer, build fixer) get the
analyst's solution for the cluster's backlog items: approach, steps, target files, risks and
related items, next to the patches' intents.

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

`migrate publish` pushes each verified cluster and opens one draft PR per repository it
touches, with everything a reviewer needs in the body. `output.target` is `github` (also
Enterprise), `gitlab` (merge requests, marked "Draft:"), `bitbucket_cloud` or
`bitbucket_server` (Data Center); its section (`output.github`, `output.gitlab`, ...) takes the
owner (org, group path, workspace or project key), `repo_map`, base branch and reviewers.
URL, user and token default to `trees.target.host`, so the target repo's token set in the
settings serves for fetching, pushing and PRs. On GitLab and Bitbucket, a reviewer's state
counts as the review: approved approves; GitLab "request changes" and Bitbucket "needs work"
send the cluster back, with that reviewer's latest PR comments as the feedback. Labels exist only on GitHub and GitLab. Clusters sit in review queues
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
`migrate memory approve`. In the next migration the analyst's and the cluster agent's briefs
get the patch history, mappings and the approved lessons for the paths they touch (capped by
`memory.brief_max_chars`).

`migrate report` writes a Markdown summary of a run: outcomes, deciding rungs, strategies,
review queues, builds, tests, agent calls and credits, and what needs a person. See
`docs/pilot-m9.md` for the pilot runbook.

## Front end

`migrate serve` starts a web app over the project: everything the command line does can be
done there (below). It shows an overview of each run
(patches by state, clusters by forecast, what decided each verdict, recent activity), the patch
list with filters and each patch's evidence, intent, history and diff, the clusters in migration
order with a graph of how their patches are linked and the folded net diff, the knowledge pack
(hot spots and the upstream fate of every touched file), agent calls with credits, and the
config snapshot of the run, plus the review queues and each cluster's PRs, decisions and Jira
export. It listens on 127.0.0.1 by default.

### Doing the work in the browser

Every action needs your name, which is recorded with it (transitions show `human:<name>`,
reviews `source: web`), and, when the admin token is on, the token (the same one as for
Settings). Sign in once on the Run page; `--read-only` turns all actions off.

- **Run page.** Each step has a card with the same options as its command: import, history,
  analyse, apply (migrate clusters), verify (build and test), publish, PR review sync,
  Jira export, memory record and lesson proposals. "Whole migration" runs the chosen steps in
  order (import, history, analyse, apply, verify, publish; with a git range, history
  also runs before import) and stops at the first failure or when the credit budget stops a
  step. A step runs on the server as a background job, one job at a time: the page shows its
  live log (the lines the command prints), its estimated Copilot credits and agent calls, and
  can stop it before its next step (a running step is not cut off). You can close the page.
  Jobs are kept in `.patchmig/jobs/` next to the config; a job running when the server stopped
  is shown as interrupted. Steps are the functions in `patchmig/steps.py`, which the CLI calls
  too, so the browser never costs more credits than the command.
- **Review.** Approve, reject (with the reason the agent gets), retry, confirm a drop or answer a
  needs-decision question, per cluster on the Review page or the cluster page. These wait while
  a job that moves clusters is running. The Review page also syncs PR reviews and previews or
  runs the Jira export; the Report page shows and downloads `migrate report`.
- **Backlog, cluster and patch pages.** Add information to a backlog item and import and
  analyse it again; migrate a cluster (again), build and test it, publish it.
- **Notes.** The Notes page lists, adds, edits and removes engineer notes for all, a path glob,
  a patch, a cluster or a ticket, shows which analyses and clusters were made with information
  that changed since, and reruns only those. Patch and cluster pages add notes for themselves.
- **Memory.** Record the run, ask the curator for lessons (one agent call), approve or drop
  proposed lessons.
- **Start at boot.** `migrate service-file` prints a systemd unit for this config: it starts
  `migrate serve` at boot and restarts it on failure, with `PATCHMIG_ADMIN_TOKEN` (and any
  `env:` tokens) in `.patchmig/serve.env`.

### Settings in the browser

The Settings page edits the whole project config (every field is explained in
[docs/settings.md](docs/settings.md)): one form per section (trees and their Git
hosts, patch sources, Copilot, budget, Jira, build and tests, publishing, analysis), generated
from the config schema, plus the YAML text itself. A save is always written, even if the
config is incomplete (only literal secrets and broken YAML are refused); a red dot on Settings,
its sections and fields marks required fields still unset, and runs keep using the last valid
config until they are filled in. The file a save replaces is kept in `.patchmig/config-history/`
(named with the time and who saved). Saving a form rewrites the YAML without its comments;
saving on the YAML tab keeps them. Without a valid config, `migrate serve` starts with only
the settings, so a new project can be set up in the browser.

- **Admin token.** Off by default: anyone who can reach the server can act and save. With
  `migrate serve --admin-token`, or with `PATCHMIG_ADMIN_TOKEN` set, actions, saving and the
  connection tests need it: the one in `PATCHMIG_ADMIN_TOKEN`, or the one-time token it prints
  (`--no-admin-token` turns it off even with the variable set). The unit from
  `migrate service-file` always turns it on. `--read-only` turns editing off.
- **Tokens.** A secret field takes either an `env:`/`file:` reference or a token pasted into
  the page. A pasted token is written to its own file (mode 0600) in `.patchmig/secrets/` next
  to the config (`--secrets-dir` to change), which has its own `.gitignore`, and the config
  gets a `file:` reference to it. The page shows only whether a token is stored or a reference
  resolves, never the token. A replaced or removed token's file is deleted.
- **Copilot.** `llm.github_host` is the host the Copilot licence is on (empty for github.com,
  `<name>.ghe.com` for GHE.com data residency, or a GitHub Enterprise Server host name, which
  Copilot CLI supports as a technical preview) and `llm.github_token` a fine-grained PAT with
  the **Copilot Requests** permission. Classic `ghp_` PATs do not work with Copilot CLI. The
  token reaches the CLI in its environment (`COPILOT_GITHUB_TOKEN`; for GHES
  `COPILOT_PROVIDER_GHES_TOKEN` with `COPILOT_PROVIDER_GHES_HOST`), never on its command line.
  Without a token the CLI uses its own `copilot login`.
- **Source and target repositories.** `trees.source.host` (optional) and `trees.target.host`
  name the Git host (GitHub, GitLab, Bitbucket Cloud or Bitbucket Server/Data Center), its URL,
  an optional user and a token. `migrate history` signs in with it for HTTPS fetches (passed to
  git as an `http.<url>.extraHeader` in its environment, only for URLs under that host), and
  ticket links to these hosts are read with it like `code_hosts`.
- **Connection tests** (`migrate check` on the command line): the trees and their base refs,
  each Git host token, Copilot CLI and its token (a sign-in check against the GitHub API, no
  credits), Jira, and the build host. They use the saved settings.

### Several projects (workspace)

`migrate serve --workspace ~/migrations` serves every `<folder>/project.yaml` in that folder
from one server (say `pilot/` and `a14-to-a15/`). The page at `/` lists the projects: their
migration, latest run and any running job. A signed-in person can create a new one there,
which writes `<folder>/project.yaml` from the example config (as `migrate init`) and opens its
settings. Each project's pages are at `/p/<folder>/`, and the header of every page has a
project switcher. Each project keeps its ledger, jobs, settings and secrets in its own folder,
so switching saves nothing and loses nothing: come back and carry on. The command line works on
the same folders (`-c ~/migrations/pilot/project.yaml`). One admin token covers the whole
workspace, and one job runs at a time across all projects, because they spend the same credits
and may share a target tree. `migrate service-file --workspace <folder>` starts a workspace at
boot.

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
