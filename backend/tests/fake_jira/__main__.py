"""Run the fake Jira with the default fixtures, for manual testing and e2e.

    cd backend && PYTHONPATH=tests uv run python -m fake_jira --port 8090

With `--prescan-tree DIR`, also builds a `repo` target tree at DIR (must not exist)
and serves the pre-scan scenario tickets that fit it; use DIR as the workspace target tree.
Add `--build` for the build scenario on top: a fake `build/envsetup.sh` in DIR, one compile
failure, one 3-way patch (see `fake_jira.build`). `--rework` instead: the build scenario plus a chain
of two conflicting items and a moved file (see `fake_jira.rework`); the fake Copilot picks its
answers by ticket key.
"""

import argparse
from pathlib import Path

import uvicorn

from fake_jira import create_fake_jira, default_store
from fake_jira.build import build_store, write_envsetup
from fake_jira.prescan import prescan_store
from fake_jira.rework import rework_store


def main() -> None:
    parser = argparse.ArgumentParser(prog="fake_jira")
    parser.add_argument("--port", type=int, default=8090)
    parser.add_argument("--prescan-tree", type=Path, help="build the pre-scan target tree here and serve its tickets")
    parser.add_argument("--build", action="store_true", help="with --prescan-tree: set the tree up for builds")
    parser.add_argument("--rework", action="store_true", help="with --prescan-tree: set the tree up for builds and rework")
    args = parser.parse_args()
    if (args.build or args.rework) and not args.prescan_tree:
        parser.error("--build and --rework need --prescan-tree")
    if args.prescan_tree:
        target = args.prescan_tree.expanduser().resolve()
        if target.exists():
            parser.error(f"{target} already exists")
        if args.rework:
            store = rework_store(target)
            write_envsetup(target)
        else:
            store = build_store(target) if args.build else prescan_store(target)
        print(f"Pre-scan target tree: {target}", flush=True)
    else:
        store = default_store()
    print(f"Fake Jira on http://127.0.0.1:{args.port}, PAT: {store.pat}", flush=True)
    uvicorn.run(create_fake_jira(store), host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
