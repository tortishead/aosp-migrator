# Publish, review and Jira export (M8)

After `migrate verify`, the clusters go to people. A human reviews and approves each cluster
first; only approved clusters are pushed to the target repos as PRs and written to Jira.
Review sync then brings what happens on the PRs (merges, change requests) back into the
ledger.

## 1. Configure the PR host and Jira

```yaml
output:
  target: github
  branch_prefix: migration/
  draft: true
  github:
    owner: example-oem
    token: env:GITHUB_TOKEN          # needs contents:write and pull_requests:write on the repos
    base_branch: oem-a15             # the target tree's branch in the GitHub repos
    repo_map:                        # every target project a cluster may touch
      frameworks/base: platform_frameworks_base
      packages/services/Car: platform_packages_services_car
    reviewers: [migration-lead]
    # GitHub Enterprise:
    # api_url: https://github.example.com/api/v3
    # git_url: https://github.example.com/{owner}/{repo}.git
jira:
  url: https://jira.example.com      # Cloud: https://<site>.atlassian.net
  project: OEM                       # for tracking tickets of untracked patches
  token: env:JIRA_TOKEN              # Server/DC: personal access token; Cloud: API token
  # user: you@example.com            # Cloud: your account email (basic auth)
  # deployment: auto                 # auto (Cloud for *.atlassian.net), server or cloud
  comment_marker: "[migration-agent]"
```

`migrate jira-check` signs in, finds the project and runs one JQL search, read-only. Both
Jira Server/Data Center and Jira Cloud are supported through the REST API (v2; on Cloud the
JQL search uses v3 `search/jql`, because Cloud removed the old search endpoint).

GitLab and Bitbucket work the same way: set `target` to `gitlab`, `bitbucket_cloud` or
`bitbucket_server` and fill that section instead of `github`:

```yaml
output:
  target: bitbucket_server
  bitbucket_server:
    owner: AOSP                      # project key (GitLab: group path; Bitbucket Cloud: workspace)
    base_branch: oem-a15
    repo_map:
      frameworks/base: platform_frameworks_base
    reviewers: [migration.lead]      # user names (Bitbucket Cloud: account ids or {uuid})
    # url, user, token: default to trees.target.host (the target repo's host in the settings)
```

| Host | PR | Draft | Approves | Sends back | Labels |
| --- | --- | --- | --- | --- | --- |
| GitHub | pull request | draft PR | approving review or merge | review requesting changes (its text is the feedback) | yes |
| GitLab | merge request | "Draft:" title | approval or merge | reviewer state "requested changes" | yes |
| Bitbucket Cloud | pull request | draft PR | approval or merge | participant state "changes requested" | no |
| Bitbucket Server/DC | pull request | draft PR (8.18+) | approval or merge | reviewer status "needs work" | no |

GitLab and Bitbucket keep no text with a reviewer's state, so the feedback the agent gets is
that reviewer's latest three comments on the PR: write what should change as PR comments,
then set "request changes" / "needs work". Closing a PR without merging rejects the
cluster on every host.

The repos must hold the target tree's history (the cluster branches are pushed on top of the
target base). The token reaches `git push` in git's environment (an `http.<url>.extraHeader`
for the host's URLs only), never through a URL or a command line.

## 2. Review

```bash
migrate review list -c project.yaml
```

| Queue | What it holds | What to do |
| --- | --- | --- |
| quick approve | verified, confidence ≥ `thresholds.quick_approve` | skim, approve |
| careful review | verified, lower confidence | read the agent's risks and the diff |
| needs decision | a question for a human | `migrate review decide` |
| failed | could not be migrated or verified | `migrate review retry` (with a hint), or fix by hand |
| dropped | upstream, obsolete or reverted | confirm (approve) or `reject` if still needed |
| approved | approved by a human | publish the PRs, then the Jira export |

The cluster page (and `migrate clusters --show`) has everything to judge a cluster: the
backlog items and their analysis, the intents, how each project was migrated, the agents'
summaries and risks, the migrated diff, and the build and test results.

- **Approve:** `migrate review approve <id> --by <name>`, or Approve on the Review page.
- **Send back:** `migrate review reject <id> --by <name> --reason "..."`. The cluster goes back
  to `needed` and the next `migrate apply` hands your words to the cluster agent, even if a
  cherry-pick would apply. Rejecting a dropped cluster means its patches are needed after all.
  When the agent lacked information, add it to the backlog item instead (see the README,
  "Import and backlog") and analyse the item again.
- **Decide:** `migrate review decide <id> migrate|upstream|obsolete --by <name> --reason "..."`.
  `migrate` sends your answer to the agent; `upstream` and `obsolete` drop the cluster and
  count as approval.
- **Retry:** `migrate review retry <id> --by <name> --reason "hint"` for a failed cluster.

## 3. Publish the approved clusters

```bash
migrate publish -c project.yaml --dry-run      # which branches would go where
migrate publish -c project.yaml                # approved, verified clusters only
```

Nothing is pushed before the review: `publish` takes only clusters that are verified and
approved. Each becomes one PR per repository it touches, from
`migration/<project name>/r<run>-c<cluster>` against `base_branch`, linked to each other
(`output.draft` decides whether they open as drafts). The body says who approved it, and has
the original patches with their Jira keys, the intents, how each project was migrated, the
cluster agent's summary and risks, the reviewer agent's verdict, and the build and test
results. Publishing again skips clusters whose commits are unchanged; no duplicate PRs.

The PRs are the target's own review. `migrate review sync` reads them: a merge is recorded
(and that PR is never pushed to again); a change request, or closing the PR unmerged, sends
the cluster back to the agent with the review text as feedback. After the rework, build and
a new approval, `publish` force-pushes the branch (it belongs to the tool) to the same PR
and asks the reviewers again. Each review is acted on once. The Review page of
`migrate serve` shows the queues with their PRs and has the same buttons.

## 4. Jira export

```bash
migrate jira-export -c project.yaml --dry-run            # exactly what would be written
migrate jira-export -c project.yaml
migrate jira-export -c project.yaml --create-missing     # also tickets for untracked patches
```

Only approved clusters are exported, and only standard Jira fields are used:

- a label per outcome: `<label>-migrated`, `-upstream`, `-obsolete`, `-reverted` (the label
  is `migration.label`, for example `mig-A14-A15`);
- a remote link to each PR;
- one comment per ticket starting with `<comment_marker> <label>`, updated in place;
- with `jira.close_not_needed` set (a transition name and optionally a resolution), the
  tickets whose patches were all confirmed not needed are closed. A ticket that also carries
  a migrated patch, or one not yet approved, stays open. `--dry-run` shows "(would close)".

Patches without a ticket get one tracking ticket per cluster with `--create-missing`; its
key is added to those patches, so the next migration finds them through Jira. The export can
be repeated: nothing changes when nothing changed.
