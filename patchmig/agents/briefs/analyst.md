# Task: analyse one backlog item of an OEM Android migration

You are helping migrate an automotive OEM's Android {source_version} framework changes to
Android {target_version}. Each backlog item is one Jira ticket with the patches that implement
it (or one patch without a ticket, or a ticket for which no patch was found). Before anything
is migrated, analyse the item:

1. Understand it: what problem the ticket and its patches solve, and how. Describe behaviour,
   not code: "Bluetooth reconnects keep trying for 12 s instead of 5 s so slow phones
   reconnect after ignition" is good; "changes a constant" is not.
2. Decide whether it is still needed in Android {target_version}: is the behaviour already in
   the target tree (`already_in_target`), is the code it changes gone without a successor
   (`obsolete`), is it not wanted any more (`not_needed`, only when the ticket or a note says
   so), or is it still `needed`? Say `unclear` when the information below is not enough, and
   list what is missing in `missing_information`.
3. Find related items: backlog items or patches below that are the same change, depend on
   this one, fix it, revert it or duplicate it.
4. Propose a solution for the target: cherry-pick as it is, adapt it to changed code, rewrite
   it for the same behaviour, drop it, or `needs_patch` when the ticket has no patch yet.

Rules:
- Check the target tree with the `{server}` tools (search_code, read_file, find_files,
  file_history, upstream_fate on tree "target"; get_patch, get_ticket for the rest). Copy
  the target lines you base "already_in_target" or "obsolete" on into `target_evidence`.
- The deterministic checks below already compared the patches with the target's history.
  A check that concluded is strong evidence; say so if you disagree, and why.
- Use only the information below and what the tools show. Do not guess facts.
- Engineer notes come from people who know this code and product: follow them; if the code
  clearly contradicts a note, say so.
- Do not edit files and do not run shell commands.
- Refer to related items by their key exactly as listed (e.g. `OEM-12` or `patch:7`), and
  to patches by `#<id>`.

## Item {key}: {title}

{ticket}
{added}{notes}{memory}{earlier}
## Patches

{patches}

## Other backlog items (candidates for related)

{candidates}
