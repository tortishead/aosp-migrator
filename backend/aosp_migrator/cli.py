import argparse

import uvicorn

from aosp_migrator.app import create_app
from aosp_migrator.config import LAN_TOKEN, Config, resolve_data_dir


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="aosp-migrator")
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="Serve the API and web UI")
    serve.add_argument("--port", type=int, default=8000)
    serve.add_argument("--data-dir", help="Default: $AOSP_MIGRATOR_DATA_DIR or ~/.aosp-migrator")
    serve.add_argument(
        "--lan",
        action="store_true",
        help="Bind 0.0.0.0 instead of 127.0.0.1 and require the shared token to log in",
    )

    args = parser.parse_args(argv)
    if args.command == "serve":
        config = Config(resolve_data_dir(args.data_dir))
        app = create_app(config, lan=args.lan)
        host = "0.0.0.0" if args.lan else "127.0.0.1"
        if args.lan:
            _, source = config.get_with_source(LAN_TOKEN)
            where = LAN_TOKEN.env if source == "env" else f"{config.path} ([server] lan_token)"
            print(f"LAN mode: log in with the token from {where}", flush=True)
        uvicorn.run(app, host=host, port=args.port)


if __name__ == "__main__":
    main()
