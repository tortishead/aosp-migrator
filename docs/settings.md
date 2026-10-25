# Settings guide

Every field of the project config (`project.yaml`), in the order of the tabs on the browser
Settings page. Each table says what the field is, what to put in it, and what the tool does
with it.

How to read it:

- **Required** fields are marked with `*` on the Settings page and in the tables below. Leave
  any other field empty to use its default.
- **Tokens** (marked 🔑) are never written into the config as plain text. On the Settings page
  you can paste the token and it is stored for you in `.patchmig/secrets/` (the config gets a
  `file:` reference to it). In the YAML you write a reference instead: `env:NAME` (an
  environment variable of the `migrate serve` process) or `file:/path/to/file` (a file holding
  only the token).
- **Relative paths** are relative to the folder that holds `project.yaml`.
- After saving, use **Connection tests** at the bottom of a tab to check the trees, Git host
  tokens, Copilot, Jira and the build host. They cost no Copilot credits.

The admin token you type at the top of the page is not part of the config: it is
`PATCHMIG_ADMIN_TOKEN`, or the one-time token `migrate serve` prints when it starts. It is
asked for only when the token is on (`migrate serve --admin-token`, or `PATCHMIG_ADMIN_TOKEN`
set); by default saving needs only your name.

Contents: [General](#general) · [Trees and repos](#trees-and-repos) ·
[Patch sources](#patch-sources) · [Copilot](#copilot) · [Jira](#jira) ·
[Build and tests](#build-and-tests) · [Publishing](#publishing) ·
[Analysis and agents](#analysis-and-agents) · [Which token goes where](#which-token-goes-where)

---

## General

| Field | What it is | Example | Used for |
|---|---|---|---|
| `name` * | Short name of this migration project | `bmw-a14-to-a15` | Shown in the UI; part of every branch name the tool pushes |
| `workdir` | Folder for the tool's own data | `.patchmig` (default) | Holds the ledger database, worktrees, logs and jobs |
| `migration.source_version` * | Android version you migrate **from** | `"14"` | PR titles, labels, reports |
| `migration.target_version` * | Android version you migrate **to** | `"15"` | Same |
| `migration.label` | Name of this migration in Jira | `mig-A14-A15` (default: `mig-A<from>-A<to>`) | Jira labels (`mig-A14-A15-migrated`, `-upstream`, ...), the marker comment, PR bodies |

Quote version numbers (`"14"`), so YAML keeps them as text.

---

## Trees and repos

Two checked-out Android trees: the **source** (old version, with your OEM patches) and the
**target** (new version, e.g. the OEM build of Android 15).

### Source tree (`trees.source`) and target tree (`trees.target`)

| Field | What it is | Example | Used for |
|---|---|---|---|
| `path` * | Root of the checked-out tree (where `.repo` is) | `/work/oem-a14` | Reading patches, upstream checks, migration worktrees |
| `manifest` | The repo manifest, relative to the tree root | `.repo/manifests/default.xml` | Finding the projects (git repos) in the tree |
| `base_ref` * | Tag or branch of the **unmodified** Android base, present in every project | source: `android-14.0.0_r1`, target: `android-15.0.0_r1` | Source: start of the OEM patches. Target: what "upstream" means in the checks |
| `head_ref` | Tag or branch with the OEM patches. **Required for the source tree**, not used for the target | `oem-a14-release` | End of the OEM patch range (`base_ref..head_ref`) |
| `remote` | Git remote to fetch more history from | `aosp` | `migrate history`, when a project has more than one remote. Empty: the project's only remote |
| `host` | The Git server these repos live on (see below). Optional for the source; for the target it also serves publishing | | HTTPS fetches with a token, reading links in tickets, and (target) pushing branches and opening PRs |

### Git host (`trees.source.host`, `trees.target.host`, and each `code_hosts` entry)

| Field | What it is | Example | Used for |
|---|---|---|---|
| `kind` * | Type of server | `github` (also GitHub Enterprise), `gitlab`, `bitbucket_cloud`, `bitbucket_server` (Bitbucket Data Center) | Which API the tool talks to |
| `url` * | Web address of the server | `https://github.com`, `https://gitlab.oem.com`, `https://bitbucket.oem.com` | Fetching, links, API calls |
| `api_url` | API address, only if it is not the usual one | `https://git.oem.com/api/v3` | Default: `api.github.com` or `<url>/api/v3` (GitHub), `<url>/api/v4` (GitLab), `api.bitbucket.org/2.0` (Bitbucket Cloud), `<url>/rest/api/1.0` (Bitbucket Server) |
| `user` | User name for basic auth | Bitbucket Cloud: your Bitbucket username | Leave empty for GitHub, GitLab and Bitbucket Server: the token alone is enough |
| `token` 🔑 | Personal or project access token | | Read access for fetching and links; **write** access (push, open PRs) on the target host if it is used for publishing |

Token per host type:

- **GitHub / GitHub Enterprise:** a fine-grained PAT with *Contents: read* (target: *Contents:
  read and write* and *Pull requests: read and write*), or a classic PAT with `repo`.
- **GitLab:** a personal or project access token with `read_repository` (target: `api` and
  `write_repository`).
- **Bitbucket Cloud:** `user` = your username, `token` = an app password or API token with
  repository read (target: repository write and pull request write).
- **Bitbucket Server / Data Center:** an HTTP access token with *Repository read* (target:
  *Repository write*).

### Other tree settings

| Field | What it is | Example | Used for |
|---|---|---|---|
| `trees.history_commits` | How many target commits to search for an upstream fix | `20000` (default, at least 100) | Upstream check when the trees share no history; also how deep `migrate history` fetches shallow (depth 1) target projects |
| `code_hosts` | Extra Git servers that **Jira tickets link to** (commits, PRs, merge requests). Same fields as a Git host, plus `repo_map` | | Importing patches from links in tickets. Public github.com, gitlab.com and bitbucket.org need no entry |
| `code_hosts[].repo_map` | Repository on that host → project path in the source tree | `oem/platform_frameworks_base: frameworks/base` | Knowing which tree project a linked commit belongs to |

---

## Patch sources

Where the list of patches to migrate comes from. Add one or more; their patches are merged.
Pick the `type` first, the other fields depend on it.

| `type` | Fields | Example | What it reads |
|---|---|---|---|
| `git_range` | `projects`: `all` (default) or a list of project paths | `[frameworks/base, packages/services/Car]` | Every commit between `base_ref` and `head_ref` in the source tree |
| `diff_folder` | `path` * : a folder | `diffs/` | Every `.diff` / `.patch` file in it. Put each file in a subfolder named after the project path (`diffs/frameworks/base/0001.patch`), unless its paths start with the project path |
| `spreadsheet` | `path` * : an `.xlsx` or `.csv` file (first row = column names); `sheet`: sheet name (default: the first); `columns`: your column names | `columns: {commit: "Commit", project: "Repo", jira: "Ticket", subject: "Title"}` | One patch per row. `commit` is looked up in the source tree; `jira` keys are read from Jira. Columns you don't map are expected under their own names (`commit`, `project`, `jira`, `subject`) |
| `jira_query` | `jql` * : a Jira search | `project = OEM AND labels = migrate-a15` | The matching tickets. Needs the [Jira](#jira) section |
|  | `attachments` | `true` (default) | Imports `.diff` / `.patch` files attached to the tickets |
|  | `linked_tickets` | `true` (default) | Also follows linked tickets for commit and PR links |
|  | `max_attachment_kb` | `2048` (default) | Larger attachments are skipped |

---

## Copilot

Copilot CLI is the only AI the tool uses. This tab also holds the credit budget.

### Copilot CLI (`llm`)

| Field | What it is | Example | Used for |
|---|---|---|---|
| `github_host` | Where your Copilot licence lives | empty for github.com; `oem.ghe.com` for GHE.com; or your GitHub Enterprise Server host name | Copilot CLI signs in there |
| `github_token` 🔑 | **Fine-grained personal access token with the "Copilot Requests" permission**, created on that host by a user with a Copilot licence. Classic `ghp_` tokens do **not** work | | Every Copilot call. Empty: the CLI uses its own `copilot login` on the server |
| `executable` | The Copilot CLI program | `copilot` (default) or a full path | Running the agents |
| `runtime` | Agent runtime | `copilot-cli` (the only choice) | |
| `models` | Model per agent; agents not listed use the CLI's default | `{cluster_agent: <model>, intent_writer: <cheaper model>}` | Agents: `intent_writer`, `cluster_agent`, `reviewer`, `build_fixer`, `memory_curator`. Use model names exactly as Copilot CLI lists them |
| `credits_per_call` | Estimated Copilot credits one call costs, per model name (`default` for any other) | `{default: 1.0}` | Credit estimates and the budget caps. Calibrate it in the M3 spike (`docs/spike-m3.md`) |
| `timeout_seconds` | Longest one call may run | `900` (default) | A call over it is stopped |
| `max_retries` | Extra calls when an answer is not valid JSON | `1` (default) | |
| `inline_prompt_max_chars` | Longer briefs are passed as an attached file | `60000` (default) | |
| `intent_max_diff_chars` | Diff text given to the intent writer per patch | `20000` (default) | Keeps credits and prompt size down |
| `mcp_flag` | The Copilot CLI flag that takes the tool's MCP server config | `--additional-mcp-config` (default) | Change only if a Copilot CLI release renames it |
| `extra_args` | Extra arguments added to every call | `["--log-level", "debug"]` | |

How to create the Copilot token: on the host where your Copilot licence is, open *Settings →
Developer settings → Personal access tokens → Fine-grained tokens → Generate new token*, and
under *Permissions* (Account permissions) set **Copilot Requests** to read-only. No repository
access is needed.

### Budget (`budget`)

| Field | What it is | Example | Used for |
|---|---|---|---|
| `run_credits` | Most Copilot credits one run may use | `500`; empty = no cap | A step stops when the run reaches it |
| `cluster_credits` | Most credits one cluster may use per run (cluster agent, reviewer, build fixes) | `20`; empty = no cap | That cluster goes to *needs decision*, the others go on |
| `max_fix_attempts` | Build-fix rounds per cluster in verify | `3` (default); `0` = no fixing | |

---

## Jira

Needed for the `jira_query` patch source, reading ticket keys from a spreadsheet, and the Jira
export. Leave the section off if you don't use Jira.

| Field | What it is | Example | Used for |
|---|---|---|---|
| `url` * | Jira base address | `https://jira.oem.com` or `https://oem.atlassian.net` | All Jira calls |
| `project` * | Project key | `OEM` | Where tracking tickets are created |
| `deployment` | Server / Data Center or Cloud | `auto` (default: cloud for `*.atlassian.net`), `server`, `cloud` | How to sign in and which API to use |
| `user` | User for basic auth | Cloud: your account email. Server/DC: leave empty | Empty: the token is sent as a Bearer token |
| `token` 🔑 * | Server/DC: a personal access token. Cloud: an API token (from id.atlassian.com) | | Reading tickets and attachments, writing the export |
| `comment_marker` | Text that marks the tool's one comment per ticket | `[migration-agent]` (default) | Finding that comment again to update it, instead of adding new ones |
| `issue_type` | Type of the tracking tickets the tool creates for patches without a ticket | `Task` (default) | Jira export |
| `label_prefix` | Reserved | `mig` (default) | Not used yet; labels come from `migration.label` |
| `close_not_needed` | Turn on to close tickets whose patches a human confirmed are not needed. Off when unset | | Jira export |
| `close_not_needed.transition` * | Name of the workflow transition | `Close` or `Done` | |
| `close_not_needed.resolution` | Resolution, if the transition screen asks for one | `Won't Do` | |
| `close_not_needed.outcomes` | Which outcomes close a ticket | `[upstream, obsolete, reverted]` (default) | |

The tool never needs custom fields: it writes labels, links and one comment.

---

## Build and tests

Optional until you run verify. Only the modules a cluster touches are built, never the whole
tree.

### Build (`build`)

| Field | What it is | Example | Used for |
|---|---|---|---|
| `transport` | How builds are run | `ssh` (default) to a build server, or `local` when the tool runs on the build host | |
| `ssh.host` * | Build server name | `build01.oem.com` | SSH connection |
| `ssh.user` * | SSH user | `builder` | |
| `ssh.port` | SSH port | `22` (default) | |
| `ssh.key` 🔑 | Private SSH key | `file:~/.ssh/build_key` | Empty: the SSH agent or default key of the server's user |
| `ssh.workdir` * | Root of the prepared target tree on the build server | `/srv/aosp/oem-a15` | Where builds run |
| `slots` | Prepared tree roots on the build host, one per parallel build | `[/srv/aosp/a15-1, /srv/aosp/a15-2]` | Default: `ssh.workdir`, or `<workdir>-1` to `-N` when `parallelism` is N. **Required** for `local` |
| `parallelism` | Builds at the same time | `1` (default) | One tree slot per build |
| `lunch_target` * | The lunch target | `aosp_cf_x86_64_auto-trunk_staging-userdebug` | `setup_cmd` |
| `setup_cmd` | Run in the tree before every build or test | `source build/envsetup.sh && lunch {lunch_target}` (default) | |
| `module_build_cmd` | Builds the affected modules; must contain `{modules}` | `m {modules}` (default) | |
| `module_map` | File pattern → modules to build, when the `Android.bp` lookup finds the wrong ones or none | `{"packages/services/Car/service/**": [CarService]}` | Choosing modules |
| `timeout_minutes` | Longest one build or test run may take | `240` (default) | |
| `log_tail_lines` | Lines of build log kept | `200` (default) | Shown with failed builds and given to the build fixer |
| `full_build_cmd` | Deprecated, not used | | Kept so old configs still load |

### Tests (`tests`)

| Field | What it is | Example | Used for |
|---|---|---|---|
| `enabled` | Run tests after a build | `true` (default) | |
| `use_test_mapping` | Pick tests from the `TEST_MAPPING` files near the changed files | `true` (default) | |
| `mapping_groups` | Which `TEST_MAPPING` groups | `[presubmit]` (default) | |
| `extra_modules` | Tests always added | `[CarServiceUnitTest]` | |
| `test_cmd` | Runs the tests; must contain `{tests}` | `atest {tests}` (default) | |
| `max_tests` | Most tests per cluster | `20` (default) | |
| `baseline` | Also run the same tests on the unpatched target | `true` (default) | Tells failures the patch caused from ones that were already there |
| `cuttlefish` | Reserved | `false` | Not used yet |

---

## Publishing

Where the migrated clusters go: one branch and one draft PR (GitLab: merge request) per
cluster and repository.

| Field | What it is | Example | Used for |
|---|---|---|---|
| `output.target` | Where PRs are opened | `github` (default), `gitlab`, `bitbucket_cloud`, `bitbucket_server`. (`gerrit` is reserved for later and not supported yet) | Fill in the section with that name below |
| `output.branch_prefix` | Start of every pushed branch name | `migration/` (default) | Branches look like `migration/<name>/r1-c7` |
| `output.draft` | Open PRs as drafts | `true` (default) | |

### The host section (`output.github`, `output.gitlab`, `output.bitbucket_cloud` or `output.bitbucket_server`)

URL, user and token default to `trees.target.host` when it is the same kind of host, so often
you only need `owner`, `repo_map` and `base_branch`.

| Field | What it is | Example | Used for |
|---|---|---|---|
| `owner` * | Who owns the repos: GitHub org or user, GitLab group path, Bitbucket Cloud workspace, or Bitbucket Server project key | `oem-android`, `android/oem`, `AND` | Repo addresses |
| `base_branch` * | Branch of the target tree in these repos | `oem-a15` | PRs are opened against it |
| `repo_map` | Tree project path → repository name, where they differ | `frameworks/base: platform_frameworks_base` | Finding the repo for each project |
| `base_branch_map` | Per-repo base branch, where it differs from `base_branch` | `platform_packages_services_Car: oem-a15-car` | |
| `url` | Web address of the host | `https://github.oem.com` | Default: `trees.target.host.url`, else github.com, gitlab.com or bitbucket.org |
| `api_url` | API address, only if unusual | `https://github.oem.com/api/v3` | |
| `user` | Basic auth user | Bitbucket Cloud username | Default: `trees.target.host.user` |
| `token` 🔑 | Token that may push branches and open PRs | | Default: `trees.target.host.token` |
| `git_url` | Push address template with `{owner}` and `{repo}` | `ssh://git@git.oem.com/{owner}/{repo}.git` | Default: the host's HTTPS address |
| `reviewers` | Requested on every PR | `[alice, bob]` (Bitbucket Cloud: account ids) | |
| `labels` | PR labels (GitHub and GitLab only) | `[migration]` (default) | |

---

## Analysis and agents

Tuning. The defaults are a good start; change them after the pilot.

### Review thresholds (`thresholds`)

Confidence is a number from 0 to 1 that the agents give a migrated cluster.

| Field | What it is | Example | Used for |
|---|---|---|---|
| `quick_approve` | Verified clusters at or above it go to the *quick approve* queue | `0.85` (default) | Review queues |
| `careful_review` | Must not be above `quick_approve` | `0.5` (default) | Review queues |

### Path rules (`path_rules`)

Rules for sensitive parts of the tree. Each rule has:

| Field | What it is | Example |
|---|---|---|
| `pattern` * | File pattern over tree paths | `vendor/qcom/**` |
| `rule` * | `overlay_only`: allowed, but flagged for the reviewer. `needs_decision`: a person must decide before the patch is migrated. `forbidden`: the agent may never change these files | `overlay_only` |
| `reason` * | Why; shown to reviewers and the agent | `QSSI files change only through overlays` |

### Analysis (`analysis`)

| Field | What it is | Default | Used for |
|---|---|---|---|
| `max_cluster_patches` | Largest cluster; bigger ones are split at their weakest links | `15` | Clustering |
| `max_cluster_projects` | Most projects (repos) in one cluster | `3` | Clustering |
| `hot_spot_min_patches` | A file touched by this many patches is a hot spot | `3` | Knowledge pack, cluster briefs |
| `min_symbol_length` | Shorter symbol names are ignored when linking patches | `4` | Dependency detection |
| `textual_min_line_chars` | Shorter lines (braces, `break;`) don't count as evidence | `6` | Upstream check |

### Migration (`migrate`)

| Field | What it is | Default | Used for |
|---|---|---|---|
| `agent` | Call the cluster agent when cherry-pick and net diff fail | `true` | `false`: no agent calls; those clusters wait as *needs agent* |
| `review` | An independent reviewer agent checks agent-made changes | `true` | One extra call per agent-made change |
| `guardrail_retries` | Agent retries with feedback after a guardrail failure | `1` | |
| `max_change_ratio` | The agent's change may be at most this many times the size of the original patches | `3.0` | Guardrail against runaway rewrites |
| `min_change_lines` | Changed lines always allowed, whatever the ratio | `40` | |
| `brief_max_diff_chars` | Diff text given to the cluster agent | `30000` | Keeps credits and prompt size down |

### Migration memory (`memory`)

| Field | What it is | Example | Used for |
|---|---|---|---|
| `path` | Local clone of the migration memory git repo | `../migration-memory` | Carrying intents, decisions and lessons to the next migration. `migrate memory record` creates it |
| `brief_max_chars` | Memory text given to the cluster agent per cluster | `4000` (default) | |
| `max_lessons` | Lessons the curator may propose per run | `5` (default) | |

---

## Which token goes where

| Token | Field | Needs |
|---|---|---|
| Copilot | `llm.github_token` | Fine-grained PAT with **Copilot Requests**, on the Copilot licence host |
| Source repos | `trees.source.host.token` | Read (optional: only if fetching needs a sign-in) |
| Target repos | `trees.target.host.token` | Read, plus write and PRs if you publish there |
| Publishing (if different) | `output.<host>.token` | Push branches, open PRs |
| Ticket-linked hosts | `code_hosts[].token` | Read |
| Jira | `jira.token` | Server/DC: personal access token. Cloud: API token, with `jira.user` = email |
| Build server | `build.ssh.key` | SSH private key |

A complete example config with comments is in
[`patchmig/templates/project.example.yaml`](../patchmig/templates/project.example.yaml)
(`migrate init` writes it for a new project).
