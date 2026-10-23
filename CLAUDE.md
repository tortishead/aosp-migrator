# aosp-migrator: notes for coding agents

- Python 3.12+, package `patchmig`. Install with `pip install -e '.[dev]'`.
- Before committing: `ruff format . && ruff check . && pytest`. CI runs the same.
- GitHub Copilot CLI is the only LLM runtime the tool uses. Keep every LLM call behind the
  adapter in `patchmig/agents/` (from M3); never call a model from other modules.
- The ledger is the single source of truth. Change patch/cluster state only through
  `LedgerStore.transition`, which enforces `patchmig/ledger/states.py` and records history.
- Secrets in config are references (`env:`/`file:`), never literals; never log resolved secrets.
- Tests must not need real Android trees, Copilot, a build server or Jira. Use
  `tests/fixtures/scenario.py` and fakes. If a new behaviour needs a new situation, add a patch
  to the scenario together with an `EXPECTED` entry and a test proving the situation exists.
- Git is driven through the `git` CLI (subprocess), not a Python git library.
