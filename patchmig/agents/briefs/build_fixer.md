# Task: fix the build of one migrated cluster of OEM Android patches

A cluster of OEM patches was migrated from Android {source_version} to Android
{target_version}, and building it on the target fails. Fix the cluster's change so it builds,
without changing what it does.

Your working directory holds the cluster's git worktrees, one per target project at the
project's path (for example `frameworks/base/`), with the migrated change committed. Only
the directories the cluster touches are checked out; use the `{server}` tools (read_file,
search_code, find_files, upstream_fate) to read anything else in the target tree.

## Cluster: {name}

{members}

## Intent

{intent}
{engineer_notes}
## The migrated change

```diff
{migrated_diff}
```

## Build

Command: `{command}`

```
{errors}
```

## Rules

- Edit files only inside the worktrees. Do not run shell commands.
- Fix the cause in the cluster's change: adapt to the target's APIs (renamed methods, new
  parameters, moved classes). Keep the intent; keep the fix minimal.
- If the errors are not caused by the cluster's change (they are in code the cluster does not
  touch and would fail without it), change nothing and answer `not_caused_by_cluster`.
- Answer `fixed` when you changed the code, `cannot_fix` when you could not.
{previous}
