"""FastAPI app — Mini-App backend + webhooks + JARVIS sync."""
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

from api.routers import users, exchange, orders, offers, deals, webhooks, admin
from core.config import settings

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("FastAPI starting...")
    # TODO: на старте проверить БД-коннект, запустить background tasks
    # (timer для expires_at, retry webhook'ов, tron monitor)
    yield
    logger.info("FastAPI stopping...")


app = FastAPI(
    title="PRIDE P2P API",
    version="0.1.0",
    description="Backend for PRIDE P2P Mini-App + JARVIS integration",
    lifespan=lifespan,
)

# CORS — Mini-App открывается из telegram.org домена
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://web.telegram.org",
        "https://t.me",
        settings.miniapp_url,
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ─── Health ──────────────────────────────────────────────────────────
@app.get("/health")
async def health():
    return {"status": "ok", "service": "pride-p2p"}


# ─── Routers ────────────────────────────────────────────────────────
app.include_router(users.router, prefix="/api/v1/users", tags=["users"])
app.include_router(exchange.router, prefix="/api/v1/exchange", tags=["exchange"])
app.include_router(orders.router, prefix="/api/v1/orders", tags=["orders"])
app.include_router(offers.router, prefix="/api/v1/offers", tags=["offers"])
app.include_router(deals.router, prefix="/api/v1/deals", tags=["deals"])
app.include_router(webhooks.router, prefix="/api/v1/webhooks", tags=["webhooks"])
app.include_router(admin.router, prefix="/api/v1/admin", tags=["admin"])


# ─── Mini-App статика ──────────────────────────────────────────────
# В прод-сборке Vue 3 → /miniapp/dist
# Пока — отдаём `app/index.html` (vanilla HTML с заглушкой)
MINIAPP_DIST = Path(__file__).parent.parent / "miniapp" / "dist"
if MINIAPP_DIST.exists():
    app.mount("/app", StaticFiles(directory=MINIAPP_DIST, html=True), name="miniapp")
else:
    # Fallback — отдаём встроенный HTML-плейсхолдер
    PLACEHOLDER_HTML = Path(__file__).parent.parent / "miniapp" / "index.html"

    @app.get("/app", response_class=HTMLResponse)
    async def miniapp_placeholder():
        if PLACEHOLDER_HTML.exists():
            return FileResponse(PLACEHOLDER_HTML)
        return HTMLResponse(
            "<h1>PRIDE P2P Mini-App</h1>"
            "<p>Frontend ещё не собран. Запусти <code>cd miniapp && npm run build</code></p>"
        )
