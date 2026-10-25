"""Run the fake Jira with the default fixtures, for manual testing and e2e.

    cd backend && PYTHONPATH=tests uv run python -m fake_jira --port 8090
"""

import argparse

import uvicorn

from fake_jira import create_fake_jira, default_store


def main() -> None:
    parser = argparse.ArgumentParser(prog="fake_jira")
    parser.add_argument("--port", type=int, default=8090)
    args = parser.parse_args()
    store = default_store()
    print(f"Fake Jira on http://127.0.0.1:{args.port}, PAT: {store.pat}", flush=True)
    uvicorn.run(create_fake_jira(store), host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
