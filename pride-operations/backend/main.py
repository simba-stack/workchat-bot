"""PRIDE Operations — main FastAPI entry point."""
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from auth import (
    TgLoginPayload, UserSession, create_access_token, create_refresh_token,
    decode_token, determine_role, get_current_user, verify_telegram_login,
)
from config import settings

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("pride-operations")

BACKEND_DIR = Path(__file__).parent.resolve()
ROOT_DIR = BACKEND_DIR.parent
FRONTEND_DIR = ROOT_DIR / "frontend"
STATIC_DIR = FRONTEND_DIR / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("PRIDE Operations starting: bot=@%s admins=%s", settings.tg_bot_username, settings.admin_ids)
    yield
    logger.info("PRIDE Operations shutting down.")


app = FastAPI(title=settings.app_name, version="0.1.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])


@app.get("/healthz")
async def healthz():
    return {"status": "ok", "app": settings.app_name, "version": "0.1.0"}


@app.get("/api/config")
async def get_config():
    return {
        "bot_username": settings.tg_bot_username,
        "app_name": settings.app_name,
        "features": {"cabinet": settings.feature_cabinet_enabled, "admin": settings.feature_admin_enabled},
    }


@app.post("/api/auth/telegram")
async def auth_telegram(payload: TgLoginPayload, response: Response):
    if not verify_telegram_login(payload):
        raise HTTPException(status_code=401, detail="Invalid Telegram signature")
    role = determine_role(payload.id)
    user = UserSession(user_id=payload.id, username=payload.username, first_name=payload.first_name, role=role, issued_at=payload.auth_date)
    access_token = create_access_token(user)
    refresh_token = create_refresh_token(payload.id)
    response.set_cookie("access_token", access_token, max_age=settings.jwt_access_ttl_min*60, httponly=True, secure=True, samesite="strict", path="/")
    response.set_cookie("refresh_token", refresh_token, max_age=settings.jwt_refresh_ttl_days*86400, httponly=True, secure=True, samesite="strict", path="/api/auth")
    logger.info("Auth OK: user_id=%s @%s role=%s", payload.id, payload.username, role)
    return {"ok": True, "user": user.model_dump()}


@app.post("/api/auth/refresh")
async def auth_refresh(request: Request, response: Response):
    refresh_token = request.cookies.get("refresh_token")
    if not refresh_token:
        raise HTTPException(401, "No refresh token")
    try:
        payload = decode_token(refresh_token)
    except Exception as e:
        raise HTTPException(401, f"Invalid refresh: {e}")
    if payload.get("type") != "refresh":
        raise HTTPException(401, "Wrong token type")
    user_id = int(payload["sub"])
    role = determine_role(user_id)
    user = UserSession(user_id=user_id, username=None, first_name="", role=role, issued_at=int(payload["iat"]))
    new_access = create_access_token(user)
    response.set_cookie("access_token", new_access, max_age=settings.jwt_access_ttl_min*60, httponly=True, secure=True, samesite="strict", path="/")
    return {"ok": True}


@app.post("/api/auth/logout")
async def auth_logout(response: Response):
    response.delete_cookie("access_token", path="/")
    response.delete_cookie("refresh_token", path="/api/auth")
    return {"ok": True}


@app.get("/api/me")
async def get_me(user: UserSession = Depends(get_current_user)):
    return {"ok": True, "user": user.model_dump()}


if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
async def root():
    index_path = FRONTEND_DIR / "index.html"
    if index_path.exists():
        return FileResponse(index_path)
    return JSONResponse({"status": "ok", "note": "frontend not built yet"})


@app.get("/{full_path:path}")
async def catch_all(full_path: str):
    if full_path.startswith("api/") or full_path.startswith("static/") or full_path == "healthz":
        raise HTTPException(404)
    index_path = FRONTEND_DIR / "index.html"
    if index_path.exists():
        return FileResponse(index_path)
    raise HTTPException(404)


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", settings.port))
    uvicorn.run("main:app", host="0.0.0.0", port=port, log_level="info")
