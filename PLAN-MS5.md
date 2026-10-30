# MS5 — Rework: dependency-aware patches, then Copilot

Sub-plan of [PLAN.md](PLAN.md); working rules there apply. Requirements: [spec.md](spec.md) (Migration level 3, guiding principle "save tokens").

For items whose diff does not apply (`conflicts`), make a patch that does: first without Copilot (stack the prerequisites, merge with a base found in the source tree), then with Copilot on only what is still failing. Results are `migration_patch` rows that go through the MS6 build. Comes before MS2-2: it needs the Copilot adapter, not the MS2-2 verdicts.

## Decisions

- **Ladder (cheapest first, per diff file):** each rung only runs for the files the rung before left failing.
  1. *Stack:* apply on HEAD plus the prerequisites' patches (plain, then 3-way). No Copilot.
  2. *Source base:* for a preimage blob the target lacks (`missing`), add the source project's object store as an alternate and 3-way merge again. No Copilot. Needs the workspace source tree.
  3. *Resolve (3a):* Copilot resolves the conflict markers a real 3-way left in the files.
  4. *Port (3b):* Copilot ports the `.rej` hunks of `git apply --reject` (no base for a 3-way) into the current files.
  5. *Re-implement (3c):* Copilot re-implements the file's change from intent (file moved, rewritten or gone). Only on explicit request.
  Rungs 1–2 make a patch with level `stacked` / `source3way`; 3–5 make one with source `ai` and level `resolve` / `port` / `reimpl`.
- **Dependencies:** prerequisites are the items making the `pending` blobs of the item's `diff_dependency` rows, followed transitively; plus the cluster members before it when the item is in a cluster (cluster order). Ordered topologically (ties: cluster order, then key). A cycle, or a set the user selects as one unit, is reworked together: one scratch checkout, one Copilot session.
- **Base of a patch:** HEAD plus the patches below it. `migration_patch` gains `base_patch_ids` (ordered, empty = on HEAD alone). A patch is stale when the HEAD moved, any base patch is no longer its item's current patch, or the item has a newer diff version. A prerequisite's patch: its current accepted patch (`original` or `ai`); one still `proposed` may be used as a base, but the dependent's patch stays blocked (cannot be accepted) until that base is accepted.
- **Scratch checkout, never the user tree:** per project, `git clone --shared --no-checkout` of the target project into `<data>/workspaces/<id>/rework/<attempt>/<project>`, checked out at HEAD, base patches applied and committed there (so `git diff <base commit>` is the item's patch alone). Deleted after the attempt (kept on failure with a setting `rework.keep_scratch`). Nothing written to the user's project: `--shared` only reads its object store. The pre-scan rule "never mutate" holds.
- **Patches come from git, not from the model:** Copilot edits files in the scratch checkout; the patch is `git diff --binary -M <base commit>` there. Copilot is never asked to write a diff.
- **Copilot adapter:** standalone `copilot` CLI behind an interface (`CopilotClient`), as in the first MS2 plan (`ms2` commit `40cd8a2`, not in this clone: rewritten). Run in the scratch checkout as cwd, non-interactive, file write tools allowed only inside it, shell and network denied. Exact flags settled by the 5.2 spike against the installed CLI version. Settings: binary path, model per rung (cheap for 3a, stronger for 3b/3c), token or premium-request cap per attempt, build-fix rounds, a "Test Copilot" button. A fake CLI (a script that applies canned edits) for tests.
- **Context bundle (per Copilot call, fixed priority, cut at the cap):** ticket summary and description (trimmed); the failing files only: conflict regions with N lines around, or `.rej` hunks plus the current code where they would go; the original diff's sections for those files; for each prerequisite touching the same files, its original diff against its migrated patch for those files (shows renames and API changes the dependent needs too) and its summary; for 3c, the source tree's before/after of the file. Stored with the attempt. Asked to end with a short explanation per file (stored, shown in review).
- **Checks before review (no Copilot):** no conflict markers or `.rej` files left; the patch `git apply --check`s on its base; touches only the item's files (others flagged, not refused); size against the original's (large difference flagged). A failing check counts as a failed round.
- **Build-fix loop:** a rework attempt may build its patch (MS6 runner, stacked on its base) and feed the build error summary back to Copilot for up to `rework.build_rounds` rounds (default 2), stopping early when a round changes nothing or the errors repeat. A build error naming a symbol that another item's diff adds is reported as a likely missing dependency instead of another round.
- **Review gate:** `ai`, `stacked` and `source3way` patches are `proposed`; the user accepts or rejects (`migration_patch.review`: `proposed` / `accepted` / `rejected`; `original` patches are `accepted`). Build eligibility (MS6): triage `applies` / `3way`, or an accepted current patch. MS7 (human review of every item) is separate and later.
- **Ripple:** accepting a new patch for a prerequisite makes its dependents' patches stale (shown, with reason "base <key> changed"). Nothing reworked automatically: the user starts "Rework chain" for the item and everything depending on it.
- **Trigger:** user-started only. Bulk "Rework…" on a selection, item "Rework" button, "Rework chain". The dialog shows the rungs to try (3c off by default), the order and an estimate of Copilot calls. Rungs 1–2 always run first and cost nothing.
- **Job:** `rework_job` like `build_job` (one per workspace, progress, cancel, one failing item does not stop the job). Refused while a build or pre-scan runs in the workspace (the scratch checkout reads HEAD). Cancel kills the Copilot process and deletes the scratch.
- **Not now:** tests after the AI patch (MS6-2), semantic dependencies beyond blobs, symbol hint and clusters (MS2-2), editing a proposed patch by hand (`manual` source, later), Copilot via API instead of CLI.

## Data model (additions, sketch)

- `migration_patch` — `base_patch_ids` (JSON, default `[]`), `review` (`proposed` / `accepted` / `rejected`), `attempt_id` (nullable); `source` adds `ai`, `level` adds `stacked` / `source3way` / `resolve` / `port` / `reimpl`
- `rework_job` — like `build_job` (workspace_id, issue_keys in order, options: rungs, chain; status, done, total, errors, times, cancel_requested)
- `rework_attempt` — id, job_id, item_ids (JSON, several for a unit), project HEADs, base_patch_ids, rungs tried, per file outcome (rung that fixed it or why it failed), Copilot calls (model, prompt bundle path, response, explanation, usage if reported, duration), build rounds (run ids), status (`patched` / `failed` / `canceled` / `error`), reason, started_at, finished_at
- `item` — `rework_status`, `rework_reason`, `reworked_at` (backlog column and filter, like build)

## Steps

**5.1 Patches on a base**
- Migration adds `base_patch_ids`, `review` (existing rows `accepted`). `prescan/chain.py`: prerequisites of an item from `diff_dependency` (+ cluster order), topological order, cycles as units. Stale check extended by base patches. Stacked build switches from restacking the original diffs to the members' current patches (falling back to the diff when it has none, as today).
- Test: chain from fixture dependencies (PROJ series with `pending` rows), cycle detection, order ties, stale on base change, stacked build of patches with a base.

**5.2 Copilot adapter spike and client**
- Spike: run the installed `copilot` CLI in a scratch git checkout, find the flags for non-interactive use, write access limited to the cwd, shell denied, model choice, and whether usage is reported. Write the findings here.
- `copilot/client.py` (`CopilotClient.run(cwd, prompt, model, timeout) -> CopilotResult`), settings (binary, models per rung, caps), `POST /settings/copilot/test`, settings form. Fake CLI in `tests/fake_copilot/` (canned file edits by prompt marker).
- Test: client against the fake (success, timeout, non-zero exit, cancel kills it), settings validation.

**5.3 Scratch checkout and rungs 1–2**
- `rework/scratch.py`: shared clone at HEAD, base patches committed, real `git apply --3way` (leaves markers) / `--reject`, result per file (`applied`, `merged`, `conflict` with marker regions, `rejected` with `.rej` hunks), patch from `git diff`. Source base: source project's objects as alternate (project mapped by path in the source tree), 3-way again. Cleanup.
- Test: user tree and object store unchanged; a fixture item that only conflicts without its prerequisite gets a `stacked` patch; a `missing` blob present in a fixture source tree gets a `source3way` patch; conflict regions and `.rej` hunks read back correctly.

**5.4 Copilot rungs 3a / 3b**
- `rework/bundle.py` (context bundle under the cap, prerequisite original-vs-migrated deltas), `rework/checks.py`, attempt record, `ai` patch `proposed`. Migration adds `rework_attempt`, item rework fields.
- Test with the fake Copilot: resolve markers, port a `.rej` hunk, markers left → failed round, extra file flagged, bundle cut at the cap keeps priority order, prerequisite delta included only for shared files.

**5.5 Rework job, build-fix loop, API**
- `rework/runner.py` (pattern of `BuildRunner`): order, units, rungs per options, chain, build-fix rounds through the build runner on the scratch patch stacked on its base in the user tree (same prepare / restore), cancel, startup recovery (running jobs failed, scratch dirs removed). Mutual exclusion with build and pre-scan jobs.
- API: start (keys, chain, rungs), job status, cancel, attempts per item, accept / reject patch (refused while a base is not accepted), rework summary counts; items API `rework_status`, filter.
- Test: a chain where the prerequisite is reworked first and the dependent on top; build-fix round that fixes a compile error; no-progress stop; accept blocked by a proposed base; ripple marks dependents stale; cancel cleans up.

**5.6 Rework UI**
- Bulk "Rework…" dialog (order as in `StackedDialog`, rungs, chain, estimate), item "Rework" and "Rework chain", job panel in `JobsPopup`, backlog column, count bar and filter. Migration tab: patches with review state, base chain, the original next to the proposed patch per file, Copilot's explanation, attempt details (rungs, rounds, build logs), Accept / Reject.
- Test: component tests for the dialog, review actions, blocked accept, stale reason.

**5.7 Rung 3c re-implement**
- Opt-in rung: bundle with ticket intent, original diff, source before/after, and where the target has the file now (rename detection by content in the target, else by name). Same checks and loop.
- Test with the fake Copilot: moved file, file gone (Copilot asked where the change belongs; result flagged for review).

**5.8 End-to-end and acceptance**
- Playwright with the fake Copilot: pre-scan, a chain of two conflicting items, rework chain, review and accept both, stacked build green, tree clean.
- Manual acceptance by the user with the real Copilot CLI on a real tree: a few real `conflicts` items, Copilot calls and usage per item noted here.

## Open points (settle when the step starts)

- Whether the Copilot CLI can be limited to writes in the cwd with shell denied (5.2); if not, run it on a prompt-only bundle with search/replace blocks applied by us.
- How usage is counted: the CLI may report premium requests rather than tokens (5.2).
- Mapping a target project to its source project when paths differ between Android versions (5.3; reuse the pre-scan mapping hints).
