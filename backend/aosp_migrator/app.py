from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse

from aosp_migrator.api import health

DEFAULT_STATIC_DIR = Path(__file__).parent / "static"


def create_app(static_dir: Path | None = None) -> FastAPI:
    app = FastAPI(title="AOSP Migrator")
    app.include_router(health.router, prefix="/api")

    static_root = (static_dir or DEFAULT_STATIC_DIR).resolve()

    @app.get("/api/{path:path}", include_in_schema=False)
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
