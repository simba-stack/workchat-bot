#!/usr/bin/env python3
"""
FastAPI proxy for dynamic Guacamole RDP connections.

Endpoint:
    POST /create_session   body: {"ip": "...", "user": "...", "password": "..."}
    Возвращает: {"url": "/guacamole/#/client/<token>?token=<auth_token>"}

Реализация:
  1) Логинимся в Guacamole как admin (GUAC_ADMIN_USER/GUAC_ADMIN_PASS env)
     → получаем authToken
  2) Создаём временный RDP-connection с переданными параметрами
  3) Возвращаем ссылку которая авто-залогинит и подключит к этой connection

Прокси слушает на 9000, наружу выставляется через nginx на /api/proxy/
"""
import os
import json
import base64
import logging
import time
from typing import Optional

import httpx
from fastapi import FastAPI, HTTPException, Header
from fastapi.responses import JSONResponse
from pydantic import BaseModel
import uvicorn

logging.basicConfig(level=logging.INFO, format="[proxy] %(asctime)s %(message)s")
log = logging.getLogger("proxy")

GUAC_URL_INTERNAL = "http://127.0.0.1:8080/guacamole"
GUAC_ADMIN_USER = os.getenv("GUAC_ADMIN_USER", "guacadmin")
GUAC_ADMIN_PASS = os.getenv("GUAC_ADMIN_PASS", "guacadmin")
# Источник кто может запрашивать создание сессии (общий секрет).
# Должен совпадать с переменной в основном приложении (api.py).
SHARED_SECRET = os.getenv("PRIDE_GUAC_SECRET", "")

# DATA_SOURCE — какой backend использует Guacamole. Для flcontainers — обычно
# 'postgresql' с встроенным Postgres, либо 'mysql', либо 'sqlite'. Сначала
# пробуем postgresql, потом fallback.
DATA_SOURCES_TO_TRY = ("postgresql", "mysql", "sqlite", "sqlserver")

app = FastAPI(title="Guacamole Auth Proxy", version="1.0")


class SessionRequest(BaseModel):
    ip: str
    user: str = "Administrator"
    password: str = ""
    width: int = 1920
    height: int = 1080


class _GuacClient:
    """Тонкая обёртка над Guacamole REST API."""
    def __init__(self):
        self._token: Optional[str] = None
        self._data_source: Optional[str] = None
        self._token_ts: float = 0
        self._client = httpx.AsyncClient(timeout=15)

    async def _login(self):
        """POST /tokens — получает authToken и определяет data_source."""
        r = await self._client.post(
            f"{GUAC_URL_INTERNAL}/api/tokens",
            data={"username": GUAC_ADMIN_USER, "password": GUAC_ADMIN_PASS},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        if r.status_code != 200:
            raise HTTPException(
                status_code=502,
                detail=f"guac login failed: {r.status_code} {r.text[:200]}",
            )
        data = r.json()
        self._token = data.get("authToken")
        # availableDataSources — список, из которого надо выбрать первый рабочий
        sources = data.get("availableDataSources") or []
        if sources:
            self._data_source = sources[0]
        else:
            self._data_source = "postgresql"  # default
        self._token_ts = time.time()
        log.info("guac admin token acquired, data_source=%s", self._data_source)

    async def _ensure_token(self):
        if not self._token or (time.time() - self._token_ts) > 1500:
            await self._login()

    async def create_connection(
        self,
        ip: str,
        user: str,
        password: str,
        width: int = 1920,
        height: int = 1080,
    ) -> str:
        """Создаёт RDP connection. Возвращает identifier."""
        await self._ensure_token()
        # Имя со временем чтобы было уникальное
        name = f"rdp-{ip.replace('.', '_')}-{int(time.time())}"
        body = {
            "name": name,
            "protocol": "rdp",
            "parameters": {
                "hostname": ip,
                "port": "3389",
                "username": user,
                "password": password,
                "width": str(width),
                "height": str(height),
                "ignore-cert": "true",
                "security": "any",
                "resize-method": "display-update",
                "enable-wallpaper": "false",
                "enable-theming": "false",
                "enable-drive": "false",
                "enable-printing": "false",
                "color-depth": "24",
            },
            "attributes": {
                "max-connections": "1",
                "max-connections-per-user": "1",
            },
        }
        url = (
            f"{GUAC_URL_INTERNAL}/api/session/data/{self._data_source}"
            f"/connections?token={self._token}"
        )
        r = await self._client.post(url, json=body)
        if r.status_code not in (200, 201):
            # Если token устарел — рефрешим и retry один раз
            if r.status_code == 401:
                await self._login()
                url = (
                    f"{GUAC_URL_INTERNAL}/api/session/data/{self._data_source}"
                    f"/connections?token={self._token}"
                )
                r = await self._client.post(url, json=body)
            if r.status_code not in (200, 201):
                raise HTTPException(
                    status_code=502,
                    detail=f"guac create_connection failed: {r.status_code} {r.text[:200]}",
                )
        ident = (r.json() or {}).get("identifier")
        if not ident:
            raise HTTPException(status_code=502, detail="guac no identifier in response")
        log.info("created connection: %s → %s@%s", ident, user, ip)
        return ident

    async def build_client_url(self, identifier: str) -> str:
        """Собирает URL для iframe, который сразу подключается к connection.
        Формат Guacamole 1.5+: /guacamole/#/client/<base64-encoded-id>?token=..."""
        # ID для URL: base64(identifier + null + 'c' + null + data_source)
        raw = f"{identifier}\0c\0{self._data_source}".encode("utf-8")
        b64 = base64.b64encode(raw).decode("ascii").rstrip("=")
        return f"/guacamole/#/client/{b64}?token={self._token}"

    async def close(self):
        try:
            await self._client.aclose()
        except Exception:
            pass


guac = _GuacClient()


@app.get("/")
async def root():
    return {"service": "guacamole-proxy", "status": "ok"}


@app.get("/health")
async def health():
    """Простой liveness check — НЕ зависит от Guacamole/Tomcat (они стартуют ~30-60s).
    Railway healthcheck бьёт сюда; Tomcat-ready check вынесен в /health/full."""
    return {"status": "ok"}


@app.get("/health/full")
async def health_full():
    """Глубокий health: проверяет что Guacamole REST API отвечает."""
    try:
        await guac._ensure_token()
        return {"status": "ok", "data_source": guac._data_source}
    except HTTPException as e:
        return JSONResponse(status_code=503, content={"status": "error", "detail": e.detail})
    except Exception as e:
        return JSONResponse(status_code=503, content={"status": "error", "detail": str(e)})


@app.post("/create_session")
async def create_session(
    req: SessionRequest,
    x_pride_secret: Optional[str] = Header(default=None),
):
    """Создаёт RDP-сессию. Защищено SHARED_SECRET (если задан в env)."""
    if SHARED_SECRET and x_pride_secret != SHARED_SECRET:
        raise HTTPException(status_code=403, detail="invalid shared secret")
    if not req.ip:
        raise HTTPException(status_code=400, detail="ip required")
    try:
        ident = await guac.create_connection(
            ip=req.ip,
            user=req.user or "Administrator",
            password=req.password or "",
            width=req.width or 1920,
            height=req.height or 1080,
        )
        url = await guac.build_client_url(ident)
        return {"url": url, "identifier": ident}
    except HTTPException:
        raise
    except Exception as e:
        log.exception("create_session failed: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


@app.on_event("shutdown")
async def _shutdown():
    await guac.close()


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=9000, log_level="info")
