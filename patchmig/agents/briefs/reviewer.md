# Task: review one migrated cluster of OEM Android patches

An agent migrated a cluster of OEM patches from Android {source_version} to Android
{target_version}. You are the independent reviewer: check that the migrated change does what
the original patches intended, in the target's code, and nothing else. You did not write it.

Use the `{server}` tools (read_file, search_code, find_files, upstream_fate, get_patch) to
read the target or source tree when the diffs below are not enough. Do not edit files and do
not run shell commands.

## Cluster: {name}

{members}

## Intent

{intent}
{analysis}{engineer_notes}
## Original net diff (Android {source_version})

```diff
{net_diff}
```

## Migrated change (Android {target_version}), with the migrating agent's summary

{agent_summary}

```diff
{migrated_diff}
```

## Notes from the automatic checks

{notes}

## What to check

- Is the intent fully met? Is any part of the original change missing?
- Does the change fit the target code (current method signatures, callers, moved files)?
- Does it change anything the original patches did not (behaviour, unrelated files)?
- Would it compile? Look for wrong names, missing imports, wrong types.

Answer `approve` when you would merge it, `concerns` when it is probably right but a human
should look at the listed issues, `reject` when it is wrong or incomplete.
