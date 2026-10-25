# M3 spike: Copilot CLI on your machine

This is the go/no-go gate of the plan. It checks three things that can only be checked on your
machine, with your Copilot plan and your trees:

1. Copilot CLI runs headless with the flags the tool uses, and returns parseable JSON.
2. Copilot CLI can call the tool's MCP server (and your enterprise policy allows it).
3. Intents written for about 20 real patches are useful, and what they cost in credits.

Nothing in the spike changes your trees, Jira or Git hosting: every step is read-only, except
the ledger and briefs written under the project's `.patchmig/` folder.

## 1. Prerequisites

- Python 3.12 or newer and git on the machine that has the source and target trees.
- GitHub Copilot CLI installed and signed in. Check with `copilot --version`, then run
  `copilot` once interactively and sign in if asked, or set `COPILOT_GITHUB_TOKEN`.
- Optional: a Jira token in an environment variable (for example, `JIRA_TOKEN`).

## 2. Install

```bash
git clone https://github.com/tortishead/aosp-migrator && cd aosp-migrator
python3 -m venv .venv && . .venv/bin/activate
pip install -e .
migrate init ~/spike            # creates ~/spike/project.yaml
```

## 3. Configure `~/spike/project.yaml`

- `trees.source`: path, manifest, `base_ref` (the AOSP/QSSI base tag) and `head_ref` (the end of
  the OEM patches).
- `trees.target`: path, manifest and `base_ref` of the new base.
- `patch_sources`: keep the spike to about 20 patches. The simplest way is a `git_range` limited to
  one or two projects, or a small spreadsheet with 20 commit rows.
- `jira`: URL, project key, and `token: env:JIRA_TOKEN`. Remove the section to skip Jira.
- `llm.models`: optional, for example `{analyst: <a cheaper model>}`.
- Remove the `build` and `output` sections; the spike doesn't use them.

Then run `migrate config validate -c ~/spike/project.yaml`.

## 4. Check Copilot CLI headless

```bash
migrate doctor -c ~/spike/project.yaml
```

Expected: `ok   plain JSON answer (…s)`. If it fails:

- "not runnable": set `llm.executable` to the full path of `copilot`.
- "exited with …": the message shows Copilot's error. An unknown flag means your CLI version
  differs; tell me the message, or adjust `llm.extra_args`.
- "WRONG" or "no JSON object": note the output and send it to me.

## 5. Ingest and check the MCP tools

```bash
migrate ingest -c ~/spike/project.yaml      # add --no-jira to skip Jira
migrate patches -c ~/spike/project.yaml
migrate doctor -c ~/spike/project.yaml --mcp
```

Check that the ingest summary (patch count, roles, warnings) matches what you expect.
`ok   MCP tool call` means Copilot CLI started the tool server and used it. If it fails:

- An unknown `--additional-mcp-config` flag: run `copilot help` and look for the flag that takes
  an MCP JSON config. Set `llm.mcp_flag` to it.
- A policy message about MCP servers: your enterprise policy blocks MCP in Copilot CLI. Tell me;
  the fallback is giving agents a short list of allowed read-only shell commands instead.

## 6. Analyse and review the analyses

Note your current premium-request usage in your Copilot settings first, so you can compare
afterwards.

```bash
migrate analyse -c ~/spike/project.yaml --limit 20 --export ~/spike/analysis-review.md
```

Open `analysis-review.md` and tick each analysis that is correct and useful; add a note where
it is wrong. Then check the premium-request usage again and set `llm.credits_per_call` in the config
to the measured usage per call (for example, `{default: 1.0}`).

## 7. What to send back

- The output of the two `doctor` runs and the ingest summary.
- How many of the 20 analyses you ticked, and the main kinds of mistakes. Leave out code or ticket
  text if it is confidential.
- Premium requests used for the 20 analyses, and the average duration from the review sheet.

Gate: the analyses are useful enough to build on, both doctor checks pass (or there is a
workable fallback for MCP), and the credit cost per item is acceptable.
