"""FastAPI app — Mini-App backend + webhooks + JARVIS sync."""
import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

from api.routers import users, exchange, orders, offers, deals, webhooks, admin
from core.config import settings
from core.services import jarvis_sync

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("FastAPI starting...")
    # Background: периодический pull курса из JARVIS
    sync_task = asyncio.create_task(jarvis_sync.rate_sync_loop())
    logger.info("Started: jarvis rate_sync_loop")
    yield
    logger.info("FastAPI stopping...")
    sync_task.cancel()


app = FastAPI(
    title="PRIDE P2P API",
    version="0.1.0",
    description="Backend for PRIDE P2P Mini-App + JARVIS integration",
    lifespan=lifespan,
)

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


@app.get("/health")
async def health():
    return {"status": "ok", "service": "pride-p2p", "version": "0.1.0"}


app.include_router(users.router, prefix="/api/v1/users", tags=["users"])
app.include_router(exchange.router, prefix="/api/v1/exchange", tags=["exchange"])
app.include_router(orders.router, prefix="/api/v1/orders", tags=["orders"])
app.include_router(offers.router, prefix="/api/v1/offers", tags=["offers"])
app.include_router(deals.router, prefix="/api/v1/deals", tags=["deals"])
app.include_router(webhooks.router, prefix="/api/v1/webhooks", tags=["webhooks"])
app.include_router(admin.router, prefix="/api/v1/admin", tags=["admin"])


# ─── Mini-App статика ──────────────────────────────────────────────
MINIAPP_DIR = Path(__file__).parent.parent / "miniapp"
MINIAPP_DIST = MINIAPP_DIR / "dist"

if MINIAPP_DIST.exists():
    app.mount("/app", StaticFiles(directory=MINIAPP_DIST, html=True), name="miniapp")
else:
    INDEX_HTML = MINIAPP_DIR / "index.html"

    @app.get("/app", response_class=HTMLResponse)
    async def miniapp_root():
        if INDEX_HTML.exists():
            return FileResponse(INDEX_HTML)
        return HTMLResponse("<h1>PRIDE P2P</h1><p>Mini-App not built yet.</p>")

    @app.get("/")
    async def root():
        return {"service": "pride-p2p", "app": "/app", "api": "/api/v1"}
