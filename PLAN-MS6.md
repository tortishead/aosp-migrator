# MS6 — Migration patch and build

Sub-plan of [PLAN.md](PLAN.md); working rules there apply. Requirements: [spec.md](spec.md) (Migration level 1 + 2, Build and verify).

Comes before MS2-2: the whole path import → pre-scan → patch → build works without Copilot. Covers spec levels 1 (plain apply) and 2 (3-way); level 3 (MS5) later adds patches with source `ai` that go through the same build.

## Decisions

- **Migration patch:** the unit that gets built. Source `original` (from the Jira diff) now; `ai` (MS5) and maybe `manual` later. Level `plain` / `3way`. Made by the pre-scan, not the build: when a diff's apply check gives `applies`, the patch is the diff's sections for the files that are not already present; for `3way`, it is the diff of the temp index against HEAD after `git apply --3way --cached` (the merged result). `conflicts` gives no patch: those items are not built. Stored with its project, the HEAD it was made against, and the format-patch author and message (for MS8). Patches are immutable; a re-scan adds a new one only when the content or base changed.
- **Eligible items:** triage label `applies` or `3way` (later: an accepted `ai` patch). Others in a selection are skipped and listed. Stale items are allowed: when a patch's base HEAD differs from the project HEAD, or the item has a newer diff version, the build job re-scans that item first and uses the new patch (or reports `apply_failed` when it now conflicts).
- **Unit:** an item, or a whole cluster (members in cluster order, one build at the end). A multi-repo ticket applies every diff's patch in its project and builds the union of modules once. Cluster members' patches are each made against HEAD on their own, so the cluster applies them in order with `git apply --3way`; overlapping members that then conflict give `apply_failed` (dependency-ordered patches come with MS2-2).
- **Writes to the user tree (build jobs only):** the pre-scan rule "never mutate" still holds for pre-scan and import. Per unit: record HEAD of every affected project, `git apply` the patch(es) into the working tree (no commit, no branch), build, then restore: `git reset --hard <recorded HEAD>` and delete exactly the files the patches added. Never `git clean`. Every unit starts from the same clean base; nothing stacks.
- **Safety:** a unit whose affected projects have tracked changes, a HEAD different from the recorded one, or an untracked file where the patch adds one is refused before anything is touched (`apply_failed`, "project dirty: <project>"). The in-flight state (projects, HEADs, added files) is written to the DB before the first apply; cancel restores after killing the build; server startup restores any unit still in flight and logs it in the item history.
- **Build environment (per workspace):** `envsetup.sh` path (default `<repo root or target tree>/build/envsetup.sh`), `lunch` target (required to build), timeout (default 2 h). Build root = parent of the `build/` dir holding `envsetup.sh`. Command: `bash -c 'source <envsetup> && lunch <target> && m <modules>'` (or `mmm <dirs>`), cwd = build root, own process group. No `get_build_var`, no test-env button for now.
- **Affected modules:** `module-info.json` found by globbing `<build root>/out/target/product/*/module-info.json`. Exactly one: each changed file (incl. `Android.bp` / `.mk`) maps to the module(s) with the longest `path` that contains it, built with `m`. None or several: fall back to the nearest directory with an `Android.bp` / `Android.mk`, built with `mmm`. Files under no module (`OWNERS`, docs) build nothing; a unit with nothing to build counts as `build_successful` with the note "nothing to build". Paths are converted between target tree and build root (they differ with a `.repo` root above the target tree).
- **Build status (per item, own field, not local state, not triage):** `apply_failed`, `build_failed`, `build_successful`, with a reason (first error lines or the apply error). Migration done = `build_successful`. A canceled unit keeps the previous status. Backlog column, count bar and filter, like pre-scan.
- **Attempts:** every build run is kept (patch ids, modules, command, status, summary, log path, times). Full log in `<data>/workspaces/<id>/builds/<run id>.log`; error summary from `FAILED:` / `error:` lines into the DB.
- **Job:** user-started only (bulk "Build selected", item and cluster "Build" buttons). Same mechanics as import and pre-scan: progress, cancel, one build job per workspace, units one after another, a failed unit does not stop the job. Live log tail while running.
- **Not now:** tests (MS6-2), exclusion between build and pre-scan / import jobs or between workspaces sharing a build root (a pre-scan during a build reads the patched tree), product out dir field, batching several units into one build.

## Data model (additions, sketch)

- `migration_patch` — id, item_id, attachment_id (nullable for `ai`), project, source (`original` / `ai`), level (`plain` / `3way`), base_head, diff (text), author, message, created_at
- `build_job` — like `prescan_job` (workspace_id, scope, selection, status, done, total, errors, started_at, finished_at, cancel_requested)
- `build_run` — id, job_id, item_ids (JSON, several for a cluster), cluster_id (nullable), patch_ids (JSON), modules (JSON), command, status, summary, log_path, inflight (JSON: projects, heads, added files; cleared after restore), started_at, finished_at
- `item` — build_status, build_reason, built_at

## Steps

**6.1 Migration patch from the pre-scan** — done
- Migration adds `migration_patch`. Apply check returns the patch text for `applies` / `3way`; the runner stores it per diff (new row only when content or base changed). Item pre-scan API returns the current patch per diff.
- Test: `applies` stores the non-present sections; `3way` stores a merged diff that `git apply --check`s on HEAD; `conflicts` stores none; re-scan without change adds no row; user tree and object store unchanged.
- Done as: `ApplyCheck.patch`; the 3-way patch is a fresh temp index with every checked file merged in, `git diff --cached --binary -M HEAD` with fixed prefixes. `Patch.author` / `Patch.message` (first author; messages of a series in order, `[PATCH n/m]` dropped). The current patch (`current_patch`) is the diff's newest row when its last scan applies and the row matches the scan's project and HEAD; after a HEAD move it stays until the re-scan, the item shows stale meanwhile.

**6.2 Migration tab on the item page** — done
- API: item patches (newest first) with level, source, project, base HEAD, current flag; later its build runs.
- UI: item tab "Migration" listing patches; each opens in the existing diff viewer.
- Test: component tests for plain and 3-way patches, empty state for conflicts.
- Done as: `api/migration.py`: `GET …/items/{key}/patches` (no patch text; `current` = the current diff version's `current_patch`) and `GET …/patches/{id}` (parsed with hunks, like the diff endpoint). `DiffBody` takes an id, query key and loader; `useViewType` / `ViewToggle` shared. The tab (`?tab=migration`) opens the current patch first, superseded ones on click.

**6.3 Build settings per workspace**
- Migration adds `envsetup_path`, `lunch_target`, `build_timeout` to `workspace`. Validation: `envsetup.sh` is a file inside the target tree or repo root. Workspace form fields.
- Test: default path, validation errors, build root derivation with and without repo root.

**6.4 Affected modules**
- `build/modules.py`, pure: read `module-info.json`, map changed files to modules (longest path), fallback to nearest `Android.bp` / `Android.mk` directory, nothing-to-build, path conversion target tree ↔ build root.
- Test: synthetic `module-info.json`, nested modules, `Android.bp` change, no module, zero / several product dirs fallback, repo root above target tree.

**6.5 Apply and restore**
- `build/tree.py`: dirty check, record HEADs, apply patches (in order, `--3way` for clusters), restore (reset to recorded HEAD, delete added files), recovery from an in-flight record.
- Test on `RepoTree`: restore leaves the tree byte-identical; user's untracked files survive; dirty project refused before any write; moved HEAD refused; multi-project unit restores all on partial failure; recovery restores a half-applied tree.

**6.6 Build job runner**
- Migration adds `build_job`, `build_run`, item build fields. `BuildRunner` (pattern of `PrescanRunner`): eligibility, stale re-scan, units from items and clusters, in-flight record, build subprocess in its own process group with timeout, log file, error summary, restore, status per item, history events (`build_started`, `build_finished`, `tree_restored`). Startup: running jobs failed, in-flight units restored.
- Test with a fake `build/envsetup.sh` in the fixture tree (shell `lunch` / `m` / `mmm` functions that log the modules and fail on a marker in a source file): success, compile failure with summary, nothing to build, apply failure, cluster unit, cancel mid-build restores the tree, timeout, restart recovery, one job per workspace, a failing unit does not stop the job.

**6.7 Build API and UI**
- API: start (keys / cluster), job status with log tail, cancel, runs per item, build summary counts. Items API: `build_status`, `build_reason`, filter `build`.
- UI: bulk "Build selected" (skipped items listed), item and cluster "Build" buttons, build job panel (progress, live log tail, cancel), backlog "Build" column, count bar and filter, Migration tab shows runs with status, modules, summary and full log.
- Test: component tests for the bulk action, job panel, column and filter, run list.

**6.8 End-to-end and acceptance**
- Playwright: import, pre-scan, build selected against the fake envsetup tree, one success and one compile failure, item tab shows the 3-way patch and the log, tree clean afterwards.
- Manual acceptance by user on a real clean-built tree with a real `lunch` target.
