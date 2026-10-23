# Verify (M7): building and testing on your build server

`migrate verify` builds the affected modules of every migrated cluster on the build server,
runs the cluster's TEST_MAPPING tests, and lets the cluster agent fix build breaks. Only the
modules a cluster touches are ever built; there is no full build.

## 1. Prepare the build server

You need one prepared target tree per parallel build (a "slot"): the target tree synced to
the same revisions as `trees.target`, with `build/envsetup.sh`, ideally built once so module
builds are incremental. Verify moves the touched projects of a slot to the cluster's commits
and back to the target base afterwards; nothing else in the slot is changed.

```yaml
build:
  transport: ssh
  ssh:
    host: build01.example.com
    user: builder
    key: file:~/.ssh/build_key      # a key file; env:VAR may hold the path instead
    workdir: /srv/aosp/oem-a15      # the slot, or /srv/aosp/oem-a15-1..N with parallelism N
  parallelism: 2                    # or list the trees explicitly under `slots:`
  lunch_target: oem_car-trunk_staging-userdebug
  setup_cmd: "source build/envsetup.sh && lunch {lunch_target}"
  module_build_cmd: "m {modules}"
  timeout_minutes: 240
  module_map:                       # optional: modules for paths Android.bp lookup misses
    "vendor/oem/overlays/**": [OemFrameworkOverlay]
tests:
  test_cmd: "atest {tests}"
  mapping_groups: [presubmit]
  baseline: true
```

SSH runs through your system `ssh` with `BatchMode=yes`, so it never prompts: the key must
be usable without a passphrase prompt (an agent works). If the orchestrator runs on the build
host itself, use `transport: local` and list the tree roots under `slots`.

Check it:

```bash
migrate build-check -c project.yaml
```

It connects, then checks every slot for `build/envsetup.sh` and for the target base commit
of every project (a slot that is not synced fails here, not in the middle of a run).

## 2. Run it

```bash
migrate verify -c project.yaml --cluster <id>    # one cluster first
migrate verify -c project.yaml --no-fix           # builds and tests only, no agent calls
migrate verify -c project.yaml                    # all migrated clusters, fixing build breaks
```

Per cluster, on a free slot:

1. The touched projects go to the target base, and the cluster branch's commits are applied
   with `git am -3` (sent from this machine with `git format-patch`; the server needs no
   access to the orchestrator's repositories).
2. **Modules:** `build.module_map` globs first, else the modules of the nearest `Android.bp`
   (or `Android.mk`) above each changed file. `m <those modules>` runs after `setup_cmd`.
3. **Build break:** the cluster agent gets the compiler errors (an excerpt of the log) and
   fixes the change in the cluster's worktree; the fix passes the same guardrails as a
   migration, is committed on the cluster branch, re-applied and rebuilt, up to
   `budget.max_fix_attempts`. If the agent says the break is not the cluster's, the same
   modules are built on the unpatched target: if that fails too, the cluster waits for a
   decision (fix the target or the setup); if it passes, the agent is told and tries again.
4. **Tests:** TEST_MAPPING files from each changed file's directory up to its project root,
   in `tests.mapping_groups`, plus `tests.extra_modules`. When they fail and
   `tests.baseline` is on, they run again on the unpatched target: a failure there too does
   not fail the cluster (it is verified, below quick approval), a failure only with the
   cluster does.
5. The slot's projects go back to the target base.

Results: `verified`, `failed` or `needs_decision` per cluster and its patches, with every
command, exit code, duration and log tail on the cluster page of `migrate serve`. A cluster
with no affected module and no test asks for `build.module_map` entries.

## Notes

- Clusters run in parallel, one per slot. A cluster built on another cluster's branch gets
  that cluster's commits too, because commits are applied from the target base.
- A build fix after a dependent cluster was migrated does not reach that cluster's branch
  yet; verify the dependency first (clusters are verified in migration order).
- Device tests (Cuttlefish) are not started by verify; give `test_cmd` the options your
  setup needs, for example `atest --host {tests}` for host-side tests only.
