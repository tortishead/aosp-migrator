# Task: write the intent of one OEM Android patch

You are helping migrate an automotive OEM's Android {source_version} framework patches to a
newer Android version. Before migrating, we record *why* each patch exists, so that later
steps can check whether the newer Android already meets that intent, and re-implement it if the
code changed.

Read the commit, the ticket and the diff below and state the intent. Be concrete and short.
Describe behaviour, not code: "Bluetooth reconnects keep trying for 12 s instead of 5 s so
slow phones reconnect after ignition" is good; "changes a constant" is not.

Rules:
- Use only the information below. Do not guess facts that are not supported by it.
- List in `sources` only what you actually used.
- Lower `confidence` when the ticket is missing or the purpose is unclear, and say what is
  unclear in `open_questions`.
- Do not run commands or edit files. Everything you need is in this brief.

## Commit

Project: {project}
Subject: {subject}

```
{message}
```

## Ticket

{tickets}

## Diff

```diff
{diff}
```
