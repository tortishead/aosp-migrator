# Migrate (M6): trying it on your trees

`migrate apply` turns the analysed clusters into branches in the target tree. Start with the
free rungs, look at what is left, and only then let the agent spend credits.

## 1. Free rungs only

```bash
migrate apply -c project.yaml --no-agent
```

For each needed cluster (in dependency order) this creates a sparse worktree per target project
under `<workdir>/worktrees/run-<run>/cluster-<id>/<project>/` on branch
`patchmig/r<run>/c<id>`, then tries:

- **cherry-pick** of the member commits, oldest first (`-x`, original author, committer
  `patchmig`). Git's rename detection carries changes into files that moved upstream.
  A commit that turns out empty (already in the target) is skipped and recorded.
- **net diff**: a 3-way apply of the cluster's folded net diff. When it conflicts, the
  conflict markers stay in the worktree; this is what the agent starts from.

Clusters that apply become `applied` (confidence 0.9 for cherry-pick, 0.85 for net diff).
The others stay `needed`; `migrate serve` shows, per project, which files conflicted.

The branches live in the target projects' own repositories (`git branch --list 'patchmig/*'`).
The source commits are fetched into each target repository as `refs/patchmig/source-head`.
Running `apply` again for a cluster that is still `needed` drops its worktrees and branches
and starts over.

## 2. With the cluster agent

```bash
migrate apply -c project.yaml --limit 3      # a few clusters first; check the credits
```

For a cluster the free rungs could not finish, one Copilot CLI call runs in the cluster's
folder, with the `write` tool allowed, `shell` denied and the `patchmig` MCP tools for reading
anything else in both trees. Its brief has the member patches and intents, what happened per
project, the upstream fate of each touched file (renames, changed signatures, upstream
commits) and the net diff. It answers one of:

| Answer | Result |
| --- | --- |
| `resolved` | Conflicts resolved, the patches' own change kept → `resolved` |
| `rewritten` | The intent re-implemented in the target's code → `rewritten` |
| `upstream` | The target already does it; the quoted target code is checked → `upstream`, or `needs_decision` when the quote is not found |
| `needs_decision` | A product or architecture question for a human → `needs_decision` |
| `failed` | Could not do it → `failed` (worktree kept for a look) |

Edits are checked before they are committed:

- hard (sent back to the agent once, then `failed`): conflict markers, a `forbidden` path
  rule, a change larger than `migrate.max_change_ratio` times the net diff (at least
  `migrate.min_change_lines`), deleting a file the patches do not delete, binary files,
  no change at all;
- decision: a `needs_decision` path rule → `needs_decision`;
- soft (recorded, keeps the cluster out of quick approval): files the original patches did
  not touch, `overlay_only` path rules.

Agent-made changes then go to the reviewer agent (read-only). `reject` fails the cluster,
`concerns` keeps it below `thresholds.quick_approve`, `approve` sets the confidence to the
lower of the agent's and the reviewer's. Each cluster costs one agent call plus one review in
the good case, two plus one after a guardrail retry; `budget.cluster_credits` caps it.

## What to check on real trees

- Do the cherry-picked branches look right (`git log patchmig/r1/c<id>` in a target project)?
- For agent clusters: the cluster page in `migrate serve` shows the agent's summary, risks,
  guardrail findings, the review, and the migrated diff next to the original net diff.
- Copilot CLI tool names: the agent needs the `write` tool and must not get `shell`. If your
  Copilot CLI version names them differently, `migrate doctor` and `llm.extra_args` are the
  place to adjust (see `docs/spike-m3.md`).

Nothing is pushed anywhere yet: publishing branches as PRs comes with M8, builds with M7.
