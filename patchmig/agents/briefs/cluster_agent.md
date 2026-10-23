# Task: migrate one cluster of OEM Android patches

You are migrating an automotive OEM's patches from Android {source_version} to Android
{target_version}. A cluster is a group of related patches (a change and its fixes) that must
move together. The cheap mechanical steps (cherry-pick, then a 3-way apply of the cluster's
net diff) have already been tried and did not fully work. Your job is to finish the
migration so the target tree does what the patches intended, in the target's current code.

Your working directory holds one git worktree per target project, at the project's path
(for example `frameworks/base/`). Only the directories the cluster touches are checked out.
Use the `{server}` tools (read_file, search_code, find_files, file_history, upstream_fate,
get_patch, get_ticket) to read anything else in the source or target tree.

## Cluster: {name}

{members}

## Intent

{intent}
{review_feedback}
## Where each project stands

{situation}

## What changed upstream in the touched files

{fates}

## The cluster's net diff (all patches folded together, against the Android {source_version} base)

```diff
{net_diff}
```

## Rules

- Edit files only inside the worktrees in your working directory. Do not run shell commands.
- Resolve every conflict: no `<<<<<<<`, `=======` or `>>>>>>>` lines may remain.
- Keep the OEM intent, and nothing more. Adapt to the target's code (renamed methods, new
  parameters, moved files) instead of restoring the old code. Keep the change minimal and in
  the target's style. Do not delete files the patches do not delete.
- First check whether the target already does what the intent asks. If it does, change
  nothing and answer `upstream`, quoting the target code that proves it in `evidence`
  (exact lines from the target tree; they are checked).
- If finishing needs a product or architecture choice you cannot make from the code and
  tickets (for example two plausible places, or the feature was redesigned), change nothing
  and answer `needs_decision` with one concrete `question`.
- Answer `resolved` when you completed the patches' own change after conflicts, `rewritten`
  when you had to re-implement the intent differently, `failed` when you could not do it.
- Be honest in `confidence`; list what a reviewer should check in `risks`.
{feedback}
