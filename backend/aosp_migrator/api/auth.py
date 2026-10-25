"""Shared-token login, only active when the server runs with --lan."""

import secrets

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel

COOKIE_NAME = "aosp_migrator_token"
PUBLIC_API_PATHS = {"/api/health", "/api/auth/status", "/api/auth/login"}

router = APIRouter(prefix="/auth")


class LoginRequest(BaseModel):
    token: str


def is_authenticated(request: Request) -> bool:
    expected: str | None = request.app.state.lan_token
    if expected is None:
        return True
    presented = request.cookies.get(COOKIE_NAME)
    header = request.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        presented = header[7:]
    return presented is not None and secrets.compare_digest(presented, expected)


async def require_token(request: Request, call_next):
    path = request.url.path
    if path.startswith("/api/") and path not in PUBLIC_API_PATHS and not is_authenticated(request):
        return JSONResponse({"detail": "Login required"}, status_code=401)
    return await call_next(request)


@router.get("/status")
def status(request: Request) -> dict[str, bool]:
    return {
        "required": request.app.state.lan_token is not None,
        "authenticated": is_authenticated(request),
    }


@router.post("/login")
def login(body: LoginRequest, request: Request, response: Response) -> dict[str, bool]:
    expected: str | None = request.app.state.lan_token
    if expected is None:
        return {"authenticated": True}
    if not secrets.compare_digest(body.token, expected):
        raise HTTPException(status_code=401, detail="Invalid token")
    response.set_cookie(COOKIE_NAME, body.token, httponly=True, samesite="strict")
    return {"authenticated": True}


@router.post("/logout")
def logout(response: Response) -> dict[str, bool]:
    response.delete_cookie(COOKIE_NAME)
    return {"authenticated": False}
