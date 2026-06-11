"""FastAPI app — Mini-App backend + webhooks + JARVIS sync."""
import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

from api.routers import users, exchange, orders, offers, deals, webhooks, admin, wallet
from core.config import settings
from core.services import jarvis_sync, tron_monitor, rates_service, sweep_service

logger = logging.getLogger(__name__)


async def _ensure_schema_and_seed():
    """Safety net: create_all для всех моделей + seed coins если пусто.
    Запускается после alembic upgrade. Идемпотентно — таблицы IF NOT EXISTS,
    coins seed только если пустая таблица.
    """
    from decimal import Decimal
    from sqlalchemy import select, func
    from core.db import Base, engine, AsyncSessionLocal
    from core.models import Coin

    # 1) create_all для всех моделей (idempotent, IF NOT EXISTS)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        logger.info("[schema] create_all OK")
    except Exception as e:
        logger.warning("[schema] create_all skipped: %s", e)

    # 2) Seed coins если таблица пуста
    # (code, name, coingecko_id, networks, decimals, icon_color, icon_url,
    #  min_deposit, min_withdraw, withdraw_fee, sort_order)
    # Комиссии конкурентны Crypto Bot (USDT там $5.5, у нас $3.5).
    # С Feee.io energy rental реальный газ USDT ~$0.35 → чистый профит $3.15.
    SEED = [
        ("USDT",  "Tether",       "tether",           ["TRC20","ERC20","BEP20","TON"], 6, "#26A17B",
         "https://assets.coingecko.com/coins/images/325/small/Tether.png", 1, 5, 3.5, 10),
        ("TON",   "Toncoin",      "the-open-network", ["TON"], 9, "#0098EA",
         "https://assets.coingecko.com/coins/images/17980/small/ton_symbol.png", 1, 0.5, 0.1, 20),
        ("TRX",   "TRON",         "tron",             ["TRC20"], 6, "#FF060A",
         "https://assets.coingecko.com/coins/images/1094/small/tron-logo.png", 5, 20, 5, 30),
        ("BTC",   "Bitcoin",      "bitcoin",          ["BTC"], 8, "#F7931A",
         "https://assets.coingecko.com/coins/images/1/small/bitcoin.png", 0.0001, 0.001, 0.0002, 40),
        ("ETH",   "Ethereum",     "ethereum",         ["ERC20"], 18, "#627EEA",
         "https://assets.coingecko.com/coins/images/279/small/ethereum.png", 0.01, 0.01, 0.005, 50),
        ("SOL",   "Solana",       "solana",           ["SPL"], 9, "#9945FF",
         "https://assets.coingecko.com/coins/images/4128/small/solana.png", 0.05, 0.1, 0.02, 60),
        ("USDC",  "USD Coin",     "usd-coin",         ["TRC20","ERC20","BEP20","SPL"], 6, "#2775CA",
         "https://assets.coingecko.com/coins/images/6319/small/USD_Coin_icon.png", 1, 5, 3.5, 15),
        ("BNB",   "Binance Coin", "binancecoin",      ["BEP20"], 18, "#F3BA2F",
         "https://assets.coingecko.com/coins/images/825/small/bnb-icon2_2x.png", 0.005, 0.01, 0.002, 70),
        ("DOGE",  "Dogecoin",     "dogecoin",         ["DOGE"], 8, "#C2A633",
         "https://assets.coingecko.com/coins/images/5/small/dogecoin.png", 5, 10, 5, 80),
        ("LTC",   "Litecoin",     "litecoin",         ["LTC"], 8, "#345D9D",
         "https://assets.coingecko.com/coins/images/2/small/litecoin.png", 0.001, 0.005, 0.001, 90),
        ("XAUT",  "Tether Gold",  "tether-gold",      ["ERC20"], 6, "#D4AF37",
         "https://assets.coingecko.com/coins/images/10481/small/Tether_Gold.png", 0.001, 0.005, 0.001, 95),
        ("RUB",   "Российский рубль", None,           [], 2, "#FF3B30",
         None, 100, 100, 0, 100),
    ]
    try:
        async with AsyncSessionLocal() as db:
            cnt = (await db.execute(select(func.count(Coin.id)))).scalar() or 0
            if cnt == 0:
                for (code, name, cg_id, nets, dec, color, icon, mind, minw, fee, sort_) in SEED:
                    db.add(Coin(
                        code=code, name=name, coingecko_id=cg_id,
                        networks=nets, decimals=dec, icon_color=color, icon_url=icon,
                        min_deposit=Decimal(str(mind)), min_withdraw=Decimal(str(minw)),
                        withdraw_fee=Decimal(str(fee)), sort_order=sort_, is_active=True,
                    ))
                await db.commit()
                logger.info("[schema] seeded %d coins", len(SEED))
            else:
                logger.info("[schema] coins already seeded (%d rows)", cnt)
    except Exception as e:
        logger.warning("[schema] seed skipped: %s", e)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("FastAPI starting...")
    # Safety: гарантируем что все таблицы существуют + seed coins
    await _ensure_schema_and_seed()
    # Background: периодический pull курса из JARVIS
    sync_task = asyncio.create_task(jarvis_sync.rate_sync_loop())
    logger.info("Started: jarvis rate_sync_loop")
    # Background: TRON monitor — polling входящих TRC20 каждые 30 сек
    tron_task = asyncio.create_task(tron_monitor.monitor_loop())
    logger.info("Started: tron_monitor")
    # Background: crypto rates polling (CoinGecko) каждые 60 сек
    rates_task = asyncio.create_task(rates_service.rate_loop())
    logger.info("Started: rates_service")
    # Background: sweep USDT с user HD-адресов в hot wallet раз в час
    sweep_task = asyncio.create_task(sweep_service.sweep_loop())
    logger.info("Started: sweep_service")
    yield
    logger.info("FastAPI stopping...")
    sync_task.cancel()
    tron_task.cancel()
    rates_task.cancel()
    sweep_task.cancel()


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


@app.get("/myip")
async def my_ip():
    """Возвращает egress IP сервиса (для Feee.io whitelist).
    Без auth — это публичный IP, не секрет."""
    import httpx
    try:
        async with httpx.AsyncClient(timeout=10.0) as cli:
            # Делаем 3 запроса к разным сервисам — Railway мог выделить разные IP
            ips = set()
            for url in ["https://api.ipify.org", "https://ifconfig.me/ip", "https://ipinfo.io/ip"]:
                try:
                    r = await cli.get(url, headers={"User-Agent": "PrideP2P-Bot/1.0"})
                    ips.add(r.text.strip())
                except Exception:
                    pass
            return {"ips": sorted(ips), "user_agent": "PrideP2P-Bot/1.0"}
    except Exception as e:
        return {"error": str(e)[:200]}


app.include_router(users.router, prefix="/api/v1/users", tags=["users"])
app.include_router(exchange.router, prefix="/api/v1/exchange", tags=["exchange"])
app.include_router(orders.router, prefix="/api/v1/orders", tags=["orders"])
app.include_router(offers.router, prefix="/api/v1/offers", tags=["offers"])
app.include_router(deals.router, prefix="/api/v1/deals", tags=["deals"])
app.include_router(webhooks.router, prefix="/api/v1/webhooks", tags=["webhooks"])
app.include_router(admin.router, prefix="/api/v1/admin", tags=["admin"])
app.include_router(wallet.router, prefix="/api/v1", tags=["wallet"])


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
