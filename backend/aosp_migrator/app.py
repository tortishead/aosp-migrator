from pathlib import Path

import httpx2
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse

from aosp_migrator.api import auth, health, settings
from aosp_migrator.config import Config, resolve_data_dir

DEFAULT_STATIC_DIR = Path(__file__).parent / "static"


def create_app(
    config: Config | None = None,
    *,
    lan: bool = False,
    static_dir: Path | None = None,
    http_transport: httpx2.BaseTransport | None = None,
) -> FastAPI:
    config = config or Config(resolve_data_dir())

    app = FastAPI(title="AOSP Migrator")
    app.state.config = config
    app.state.lan_token = config.ensure_lan_token() if lan else None
    # Outgoing HTTP goes through this transport; tests swap in a mock.
    app.state.http_transport = http_transport

    app.middleware("http")(auth.require_token)
    app.include_router(health.router, prefix="/api")
    app.include_router(auth.router, prefix="/api")
    app.include_router(settings.router, prefix="/api")

    static_root = (static_dir or DEFAULT_STATIC_DIR).resolve()

    @app.api_route("/api/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"], include_in_schema=False)
    def api_not_found(path: str) -> None:
        raise HTTPException(status_code=404)

    @app.get("/{path:path}", include_in_schema=False)
    def frontend(path: str) -> FileResponse:
        # Serve a real file from the bundle if it exists, otherwise index.html
        # so client-side routes work on deep links and reloads.
        candidate = (static_root / path).resolve()
        if path and candidate.is_file() and candidate.is_relative_to(static_root):
            return FileResponse(candidate)
        index = static_root / "index.html"
        if not index.is_file():
            raise HTTPException(status_code=404, detail="Frontend bundle not built")
        return FileResponse(index)

    return app
