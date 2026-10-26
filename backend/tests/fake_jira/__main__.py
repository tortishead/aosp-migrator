"""Run the fake Jira with the default fixtures, for manual testing and e2e.

    cd backend && PYTHONPATH=tests uv run python -m fake_jira --port 8090

With `--prescan-tree DIR`, also builds a `repo` target tree at DIR (must not exist)
and serves the pre-scan scenario tickets that fit it; use DIR as the workspace target tree.
"""

import argparse
from pathlib import Path

import uvicorn

from fake_jira import create_fake_jira, default_store
from fake_jira.prescan import prescan_store


def main() -> None:
    parser = argparse.ArgumentParser(prog="fake_jira")
    parser.add_argument("--port", type=int, default=8090)
    parser.add_argument("--prescan-tree", type=Path, help="build the pre-scan target tree here and serve its tickets")
    args = parser.parse_args()
    if args.prescan_tree:
        target = args.prescan_tree.expanduser().resolve()
        if target.exists():
            parser.error(f"{target} already exists")
        store = prescan_store(target)
        print(f"Pre-scan target tree: {target}", flush=True)
    else:
        store = default_store()
    print(f"Fake Jira on http://127.0.0.1:{args.port}, PAT: {store.pat}", flush=True)
    uvicorn.run(create_fake_jira(store), host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
