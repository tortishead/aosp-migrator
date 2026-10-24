# Pilot runbook and migration memory (M9)

A pilot runs the whole tool on a real but small slice of a migration, so you can judge the
results and the cost before trusting it with a few hundred patches. This page lists the steps
in order, what to check after each, and how the migration memory carries what you learn into
the next migration.

## 0. Pick the slice

Take 20 to 40 patches from one or two projects you know well, for example the car service
and part of `frameworks/base`. Include at least one fix chain, one revert, one patch you know
is already upstream and one that will conflict. Small enough to review every result by hand;
varied enough to exercise every rung.

Before you start, run the M3 spike (`docs/spike-m3.md`) on the machine that will run the
pilot. If `migrate doctor --mcp` fails, stop there: everything below depends on it.

## 1. Configure

`migrate init pilot` and edit `pilot/project.yaml`. Give it its own `name`: cluster
branches in the target tree and on GitHub are named after it, so the pilot and the full
migration later can share the same trees and repos. Beyond trees and patch sources:

```yaml
budget:
  run_credits: 150               # stop the run at this many premium requests
  cluster_credits: 15
  max_fix_attempts: 2
build:
  transport: ssh
  ssh:
    host: build01.example.com
    user: mig
    key: file:~/.ssh/patchmig_ed25519
    workdir: /work/oem-a15       # see docs/verify-m7.md for preparing the slots
  slots: [/work/oem-a15-1, /work/oem-a15-2]
  lunch_target: bmw_qssi-userdebug
  module_map:                    # optional; else the nearest Android.bp/.mk decides
    packages/services/Car/service/: [CarService]
memory:
  path: ~/patchmig-memory        # a clone of the shared memory repo, or a new folder
```

`migrate config validate` and `migrate build-check` must both pass.

## 2. Run

```bash
migrate ingest   -c pilot/project.yaml
migrate analyse  -c pilot/project.yaml
migrate clusters -c pilot/project.yaml        # check the clusters and forecasts
migrate intents  -c pilot/project.yaml        # needed patches only; memory reuse first
migrate apply    -c pilot/project.yaml        # cherry-pick, net diff, then the agent
migrate verify   -c pilot/project.yaml        # affected modules only, then tests
migrate publish  -c pilot/project.yaml
migrate report   -c pilot/project.yaml --out pilot/report-1.md
```

Each step can be limited (`--limit`, `--cluster`) to go slowly at first. `migrate serve`
shows the same ledger in the browser.

When an intent or a migration is wrong because the agent lacked context (an OEM convention,
where something moved in the target, which variant a ticket is for), add a note instead of
rejecting blindly, and rerun only what it affects:

```bash
migrate notes add "..." --patch N --by <you> -c pilot/project.yaml   # or --cluster/--ticket/--path
migrate notes stale     -c pilot/project.yaml
migrate intents --stale -c pilot/project.yaml
migrate apply --stale --by <you> -c pilot/project.yaml && migrate verify -c pilot/project.yaml
```

What to check:

| After | Check |
| --- | --- |
| `analyse` | Every patch you know is upstream is `upstream` with evidence; chains share a cluster |
| `apply` | Rungs: most clusters by cherry-pick or net diff, the agent only for real conflicts |
| `verify` | Build failures are blamed correctly (baseline reruns), fix attempts are few |
| `publish` | PR bodies are enough to review without opening the ledger |
| `report` | Credits per agent are within the budget you would accept for the full migration |

## 3. Review

Review every PR, also the quick-approve ones (the point of the pilot is to find out whether
the queue can be trusted). Use the PRs or `migrate review`; `migrate review sync` brings PR
outcomes in. Send back with concrete words: they reach the agent on the next `apply`, and
they become memory.

```bash
migrate review sync -c pilot/project.yaml
migrate apply  -c pilot/project.yaml          # only clusters sent back or retried
migrate verify -c pilot/project.yaml
migrate publish -c pilot/project.yaml
```

Repeat until every cluster is approved, decided or consciously left out, then
`migrate jira-export --dry-run` and `migrate jira-export`.

## 4. Remember

```bash
migrate memory record -c pilot/project.yaml   # facts from approved results; one commit
migrate memory curate -c pilot/project.yaml   # one agent call: proposed lessons
migrate memory list   -c pilot/project.yaml --proposed
migrate memory approve <name> --by <you> -c pilot/project.yaml   # or reject
```

Then push the memory repo so the next migration (and your colleagues) have it.

What goes in:

- `patches/<Change-Id>.yaml`: per patch, one entry per migration with the outcome, strategy,
  intent, the agent's summary, PRs, decisions, reviewer feedback and who approved it. Found
  again by Change-Id, or through `Original-Change-Id` when a patch was re-uploaded.
- `mappings/<project>.yaml`: files that were renamed, moved to another project or deleted
  upstream.
- `decisions/<label>.yaml`: human decisions and feedback of the migration, by run.
- `lessons/proposed/` and `lessons/approved/`: Markdown with `title` and `applies_to` globs.
  Only approved lessons are used.

How it is used next time: `migrate intents` reuses a remembered intent instead of asking the
agent (no credits); the cluster agent's brief gets each member's earlier outcomes, the
mappings of the files it touches and the approved lessons whose globs match, at most
`memory.brief_max_chars` characters. Only approved results are recorded, so the memory never
learns from work a human did not accept.

## 5. Judge the pilot

Compare `pilot/report-*.md` across iterations and answer:

1. How many clusters were approved without changes, and how many needed a human edit?
2. Were any `upstream`, `obsolete` or `reverted` verdicts wrong?
3. Credits per needed patch, extrapolated to the full patch list: within the plan's budget?
4. Hours of build per cluster, and how many build-fix attempts helped.

The answers decide the settings for the full run (thresholds, budgets, `migrate.agent`) and
whether browser editing should come before it.
