"""FastAPI app — Mini-App backend + webhooks + JARVIS sync.

Lifespan: всё тяжёлое в asyncio.create_task. /health отвечает сразу.
Safety-net миграция: ALTER TABLE IF NOT EXISTS на каждом старте.
"""
import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

from api.routers import (
    users, exchange, orders, offers, deals, webhooks,
    admin, wallet, owner, cheques, audit, payment_methods,
)
from core.config import settings
from core.services import (
    deal_lifecycle, feature_flags, jarvis_sync, maker_stats,
    rates_service, sweep_service, tron_monitor,
)

from p2p import models as p2p_models
from p2p.workers import outbox_publisher as p2p_outbox_worker, scheduler as p2p_scheduler_worker, reconciliation as p2p_recon_worker
from p2p.api import commands as p2p_commands_router, queries as p2p_queries_router, admin as p2p_admin_router  # noqa: E501  # noqa: F401 — регистрирует таблицы в Base.metadata

logger = logging.getLogger(__name__)


# SIMBA: $4.5-эквивалент withdraw fee для всех coin
COIN_FEES_UPDATE = [
    "UPDATE coins SET withdraw_fee=3        WHERE code='USDT'",
    "UPDATE coins SET withdraw_fee=4.5      WHERE code='USDC'",
    "UPDATE coins SET withdraw_fee=25       WHERE code='TRX'",
    "UPDATE coins SET withdraw_fee=0.9      WHERE code='TON'",
    "UPDATE coins SET withdraw_fee=0.00005  WHERE code='BTC'",
    "UPDATE coins SET withdraw_fee=0.0015   WHERE code='ETH'",
    "UPDATE coins SET withdraw_fee=0.03     WHERE code='SOL'",
    "UPDATE coins SET withdraw_fee=0.007    WHERE code='BNB'",
    "UPDATE coins SET withdraw_fee=28       WHERE code='DOGE'",
    "UPDATE coins SET withdraw_fee=0.05     WHERE code='LTC'",
    "UPDATE coins SET withdraw_fee=0.0015   WHERE code='XAUT'",
]

P2P_INDUSTRIAL_ALTERS = [
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS maker_tier VARCHAR(16) NOT NULL DEFAULT 'none'",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS maker_tier_updated_at TIMESTAMP WITH TIME ZONE",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS cancel_cooldown_until TIMESTAMP WITH TIME ZONE",
    "ALTER TABLE offers ADD COLUMN IF NOT EXISTS price_type VARCHAR(8) NOT NULL DEFAULT 'fixed'",
    "ALTER TABLE offers ADD COLUMN IF NOT EXISTS float_margin_pct NUMERIC(6,2)",
    "ALTER TABLE offers ADD COLUMN IF NOT EXISTS coin VARCHAR(16) NOT NULL DEFAULT 'USDT'",
    "ALTER TABLE offers ADD COLUMN IF NOT EXISTS fiat VARCHAR(8) NOT NULL DEFAULT 'RUB'",
    "ALTER TABLE offers ADD COLUMN IF NOT EXISTS pay_window_min INTEGER NOT NULL DEFAULT 30",
    "ALTER TABLE offers ADD COLUMN IF NOT EXISTS min_taker_completed INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE offers ADD COLUMN IF NOT EXISTS require_kyc BOOLEAN NOT NULL DEFAULT FALSE",
    "ALTER TABLE offers ADD COLUMN IF NOT EXISTS region VARCHAR(16)",
    "ALTER TABLE offers ADD COLUMN IF NOT EXISTS paused_reason VARCHAR(64)",
    "ALTER TABLE deals ADD COLUMN IF NOT EXISTS coin VARCHAR(16) NOT NULL DEFAULT 'USDT'",
    "ALTER TABLE deals ADD COLUMN IF NOT EXISTS fiat VARCHAR(8) NOT NULL DEFAULT 'RUB'",
    "ALTER TABLE deals ADD COLUMN IF NOT EXISTS pay_deadline_at TIMESTAMP WITH TIME ZONE",
    # Этап 2: Offer-level escrow — заморозка USDT под активный sell-offer
    "ALTER TABLE offers ADD COLUMN IF NOT EXISTS amount_usdt_total NUMERIC(16,4) NOT NULL DEFAULT 0",
    "ALTER TABLE offers ADD COLUMN IF NOT EXISTS amount_usdt_remaining NUMERIC(16,4) NOT NULL DEFAULT 0",
    "ALTER TABLE escrow_locks ADD COLUMN IF NOT EXISTS offer_id BIGINT REFERENCES offers(id) ON DELETE SET NULL",
    "ALTER TABLE escrow_locks ADD COLUMN IF NOT EXISTS reason VARCHAR(128)",
    "CREATE INDEX IF NOT EXISTS idx_escrow_offer ON escrow_locks(offer_id)",
]


async def _ensure_schema_and_seed():
    from decimal import Decimal
    from sqlalchemy import select, func, text as _sql
    from core.db import Base, engine, AsyncSessionLocal
    from core.models import Coin

    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        logger.info("[schema] create_all OK")
    except Exception as e:
        logger.warning("[schema] create_all skipped: %s", e)

    # P2P safety-net columns
    try:
        async with engine.begin() as conn:
            ok_count, fail_count = 0, 0
            for sql in P2P_INDUSTRIAL_ALTERS:
                try:
                    await conn.execute(_sql(sql))
                    ok_count += 1
                except Exception as e:
                    fail_count += 1
                    logger.warning("[schema] ALTER skipped: %s -> %s", sql[:60], e)
            logger.info("[schema] P2P alters: %d ok, %d fail", ok_count, fail_count)
    except Exception as e:
        logger.warning("[schema] industrial alter block failed: %s", e)

    # === Cleanup demo/seed P2P offers (Alex_Pro, CryptoPro, NataKZ, P2P_Master) ===
    # Эти юзеры засеяны на ранних этапах разработки и засоряют P2P-маркет.
    # Удаляем их offer'ы (active) — пользователи их видят как "старый дизайн" mock'ов.
    try:
        async with engine.begin() as conn:
            r = await conn.execute(_sql(
                "DELETE FROM offers WHERE user_id IN ("
                " SELECT id FROM users WHERE username IN ('Alex_Pro','CryptoPro','NataKZ','P2P_Master') "
                " OR full_name IN ('Alex_Pro','CryptoPro','NataKZ','P2P_Master')"
                ")"
            ))
            try:
                cnt = r.rowcount if hasattr(r, "rowcount") else "?"
            except Exception:
                cnt = "?"
            logger.info("[seed-cleanup] deleted demo offers: %s rows", cnt)
    except Exception as e:
        logger.warning("[seed-cleanup] skipped: %s", e)

    # Coin fees update -> $4.5 equivalent
    try:
        async with engine.begin() as conn:
            for sql in COIN_FEES_UPDATE:
                try:
                    await conn.execute(_sql(sql))
                except Exception as e:
                    logger.warning("[schema] fee update skipped: %s -> %s", sql[:60], e)
        logger.info("[schema] coin fees updated to $4.5 equivalent")
    except Exception as e:
        logger.warning("[schema] fees update block failed: %s", e)

    SEED = [
        ("USDT",  "Tether",       "tether",           ["TRC20","ERC20","BEP20","TON"], 6, "#26A17B",
         "https://assets.coingecko.com/coins/images/325/small/Tether.png", 1, 5, 3, 10),
        ("TON",   "Toncoin",      "the-open-network", ["TON"], 9, "#0098EA",
         "https://assets.coingecko.com/coins/images/17980/small/ton_symbol.png", 1, 0.5, 0.9, 20),
        ("TRX",   "TRON",         "tron",             ["TRC20"], 6, "#FF060A",
         "https://assets.coingecko.com/coins/images/1094/small/tron-logo.png", 5, 20, 25, 30),
        ("BTC",   "Bitcoin",      "bitcoin",          ["BTC"], 8, "#F7931A",
         "https://assets.coingecko.com/coins/images/1/small/bitcoin.png", 0.0001, 0.001, 0.00005, 40),
        ("ETH",   "Ethereum",     "ethereum",         ["ERC20"], 18, "#627EEA",
         "https://assets.coingecko.com/coins/images/279/small/ethereum.png", 0.01, 0.01, 0.0015, 50),
        ("SOL",   "Solana",       "solana",           ["SPL"], 9, "#9945FF",
         "https://assets.coingecko.com/coins/images/4128/small/solana.png", 0.05, 0.1, 0.03, 60),
        ("USDC",  "USD Coin",     "usd-coin",         ["TRC20","ERC20","BEP20","SPL"], 6, "#2775CA",
         "https://assets.coingecko.com/coins/images/6319/small/USD_Coin_icon.png", 1, 5, 4.5, 15),
        ("BNB",   "Binance Coin", "binancecoin",      ["BEP20"], 18, "#F3BA2F",
         "https://assets.coingecko.com/coins/images/825/small/bnb-icon2_2x.png", 0.005, 0.01, 0.007, 70),
        ("DOGE",  "Dogecoin",     "dogecoin",         ["DOGE"], 8, "#C2A633",
         "https://assets.coingecko.com/coins/images/5/small/dogecoin.png", 5, 10, 28, 80),
        ("LTC",   "Litecoin",     "litecoin",         ["LTC"], 8, "#345D9D",
         "https://assets.coingecko.com/coins/images/2/small/litecoin.png", 0.001, 0.005, 0.05, 90),
        ("XAUT",  "Tether Gold",  "tether-gold",      ["ERC20"], 6, "#D4AF37",
         "https://assets.coingecko.com/coins/images/10481/small/Tether_Gold.png", 0.001, 0.005, 0.0015, 95),
        ("RUB",   "RUB",          None,               [], 2, "#FF3B30",
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


async def _bg_startup():
    logger.info("[bg_startup] begin")
    try:
        await _ensure_schema_and_seed()
    except Exception as e:
        logger.warning("[bg_startup] schema failed: %s", e)
    try:
        await feature_flags.bootstrap_registry()
        logger.info("[bg_startup] feature_flags bootstrap done")
    except Exception as e:
        logger.warning("[bg_startup] feature_flags bootstrap failed: %s", e)
    logger.info("[bg_startup] done")


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("FastAPI starting...")
    bg_tasks: list[asyncio.Task] = []
    bg_tasks.append(asyncio.create_task(_bg_startup()))
    bg_tasks.append(asyncio.create_task(jarvis_sync.rate_sync_loop()))
    bg_tasks.append(asyncio.create_task(tron_monitor.monitor_loop()))
    bg_tasks.append(asyncio.create_task(rates_service.rate_loop()))
    bg_tasks.append(asyncio.create_task(sweep_service.sweep_loop()))
    bg_tasks.append(asyncio.create_task(deal_lifecycle.lifecycle_loop()))
    bg_tasks.append(asyncio.create_task(maker_stats.tier_loop()))
    logger.info("FastAPI ready: %d background tasks scheduled", len(bg_tasks))
    # === P2P workers ===
    try:
        asyncio.create_task(p2p_outbox_worker.run())
        asyncio.create_task(p2p_scheduler_worker.run())
        asyncio.create_task(p2p_recon_worker.run())
        logger.info("[lifespan] p2p workers scheduled")
    except Exception as e:
        logger.warning("[lifespan] p2p workers failed to start: %s", e)
    yield
    logger.info("FastAPI stopping...")
    for t in bg_tasks:
        t.cancel()


app = FastAPI(
    title="PRIDE P2P API", version="0.2.2",
    description="Backend for PRIDE P2P Mini-App + JARVIS integration",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://web.telegram.org", "https://t.me", settings.miniapp_url],
    allow_credentials=True, allow_methods=["*"], allow_headers=["*"],
)


@app.get("/health")
@app.get("/healthz")
@app.get("/ping")
async def health():
    return {"status": "ok", "service": "pride-p2p", "version": "0.2.2"}


@app.get("/_status")
async def status_root():
    return {"ok": True}


@app.get("/myip")
async def my_ip():
    import httpx
    try:
        async with httpx.AsyncClient(timeout=10.0) as cli:
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
app.include_router(owner.router, prefix="/api/v1/owner", tags=["owner"])
app.include_router(cheques.router, prefix="/api/v1/cheques", tags=["cheques"])
app.include_router(audit.router, prefix="/api/v1/admin/audit", tags=["audit"])
app.include_router(payment_methods.router, prefix="/api/v1/users/me/payment_methods", tags=["payment_methods"])

# === P2P v2 (новый ядро) ===
app.include_router(p2p_commands_router.router)
app.include_router(p2p_queries_router.router)
app.include_router(p2p_admin_router.router)


MINIAPP_DIR = Path(__file__).parent.parent / "miniapp"
MINIAPP_DIST = MINIAPP_DIR / "dist"
INDEX_HTML = MINIAPP_DIR / "index.html"
OWNER_HTML = MINIAPP_DIR / "owner.html"

_NO_CACHE_HEADERS = {
    "Cache-Control": "no-cache, no-store, must-revalidate",
    "Pragma": "no-cache",
    "Expires": "0",
}


if MINIAPP_DIST.exists():
    app.mount("/app", StaticFiles(directory=MINIAPP_DIST, html=True), name="miniapp")
    app.mount("/app2", StaticFiles(directory=MINIAPP_DIST, html=True), name="miniapp_v2")
    app.mount("/v7", StaticFiles(directory=MINIAPP_DIST, html=True), name="miniapp_v7")
    # /m8 НЕ mount — явный handler с no-cache headers (mount не ставит их)
    @app.get("/m8", response_class=HTMLResponse)
    async def miniapp_m8_strict():
        idx = MINIAPP_DIST / "index.html"
        if idx.exists():
            return FileResponse(idx, headers={
                "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
                "Pragma": "no-cache",
                "Expires": "0",
            })
        if INDEX_HTML.exists():
            return FileResponse(INDEX_HTML, headers={
                "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
                "Pragma": "no-cache",
                "Expires": "0",
            })
        return HTMLResponse("<h1>PRIDE P2P</h1>")
else:
    @app.get("/app", response_class=HTMLResponse)
    async def miniapp_root():
        if INDEX_HTML.exists():
            return FileResponse(INDEX_HTML, headers=_NO_CACHE_HEADERS)
        return HTMLResponse("<h1>PRIDE P2P</h1><p>Mini-App not built yet.</p>")

    @app.get("/app2", response_class=HTMLResponse)
    async def miniapp_v2():
        if INDEX_HTML.exists():
            return FileResponse(INDEX_HTML, headers=_NO_CACHE_HEADERS)
        return HTMLResponse("<h1>PRIDE P2P</h1><p>Mini-App not built yet.</p>")

    @app.get("/v7", response_class=HTMLResponse)
    async def miniapp_v7():
        if INDEX_HTML.exists():
            return FileResponse(INDEX_HTML, headers=_NO_CACHE_HEADERS)
        return HTMLResponse("<h1>PRIDE P2P</h1><p>Mini-App not built yet.</p>")

    # /m8 — со строгими no-cache headers, чтобы браузер не закешировал
    @app.get("/m8", response_class=HTMLResponse)
    async def miniapp_m8():
        if INDEX_HTML.exists():
            return FileResponse(INDEX_HTML, headers={
                "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
                "Pragma": "no-cache",
                "Expires": "0",
            })
        return HTMLResponse("<h1>PRIDE P2P</h1><p>Mini-App not built yet.</p>")


@app.get("/")
async def root():
    return {"service": "pride-p2p", "status": "ok", "app": "/app",
            "owner": "/owner", "api": "/api/v1", "health": "/health"}


@app.get("/debug/offers_in_db")
async def debug_offers_in_db():
    """Публичный debug: показывает что РЕАЛЬНО в БД среди offers (без авторизации)."""
    from sqlalchemy import select as _select
    from core.db import AsyncSessionLocal
    from core.models import Offer, User
    try:
        async with AsyncSessionLocal() as db:
            r = await db.execute(
                _select(Offer.id, Offer.user_id, Offer.side, Offer.status,
                        Offer.rate_rub_per_usdt, User.username, User.full_name)
                .join(User, User.id == Offer.user_id, isouter=True)
                .where(Offer.status == "active")
                .limit(50)
            )
            rows = r.all()
            return {"count": len(rows), "offers": [
                {"id": x[0], "user_id": x[1], "side": x[2], "status": x[3],
                 "rate": float(x[4]) if x[4] else 0,
                 "username": x[5], "full_name": x[6]}
                for x in rows
            ]}
    except Exception as e:
        return {"error": str(e)}


@app.get("/debug/offers_full")
async def debug_offers_full():
    """Расширенный debug: статус + лимиты + остаток."""
    from sqlalchemy import select as _select
    from core.db import AsyncSessionLocal
    from core.models import Offer, User
    try:
        async with AsyncSessionLocal() as db:
            r = await db.execute(
                _select(
                    Offer.id, Offer.user_id, Offer.side, Offer.status,
                    Offer.rate_rub_per_usdt, Offer.min_amount_rub, Offer.max_amount_rub,
                    Offer.amount_usdt_total, Offer.amount_usdt_remaining,
                    User.username,
                ).join(User, User.id == Offer.user_id, isouter=True).limit(100)
            )
            rows = r.all()
            return {"count": len(rows), "offers": [
                {"id": x[0], "user_id": x[1], "side": x[2], "status": x[3],
                 "rate": float(x[4] or 0),
                 "min_rub": float(x[5] or 0), "max_rub": float(x[6] or 0),
                 "total_usdt": float(x[7] or 0), "remaining_usdt": float(x[8] or 0),
                 "username": x[9]}
                for x in rows
            ]}
    except Exception as e:
        return {"error": str(e)}


@app.get("/debug/cleanup_broken_offers")
async def debug_cleanup_broken_offers():
    """Удаляет офферы с битыми лимитами/суммой. Эскроу возвращает."""
    from sqlalchemy import select as _select
    from decimal import Decimal as _D
    from core.db import AsyncSessionLocal
    from core.models import Offer, User
    from core.services import escrow_service
    try:
        async with AsyncSessionLocal() as db:
            r = await db.execute(_select(Offer).where(Offer.status.in_(("active", "paused"))))
            broken = []
            for o in r.scalars().all():
                if (o.max_amount_rub or _D("0")) <= 0 or (o.amount_usdt_total or _D("0")) <= 0:
                    broken.append({"id": o.id, "user_id": o.user_id, "side": o.side})
                    if o.side == "sell":
                        try:
                            u = await db.get(User, o.user_id)
                            if u:
                                await escrow_service.release_offer_lock(db, u, o)
                        except Exception:
                            pass
                    o.status = "cancelled"
            await db.commit()
            return {"ok": True, "deleted_count": len(broken), "deleted": broken}
    except Exception as e:
        return {"error": str(e)}


@app.get("/debug/me/{tg_id}")
async def debug_me(tg_id: int):
    """Публичный debug: показывает всё что есть у юзера по tg_id."""
    from sqlalchemy import select as _select
    from core.db import AsyncSessionLocal
    from core.models import User, OperationLog
    from core.models.escrow import EscrowLock
    try:
        async with AsyncSessionLocal() as db:
            u = (await db.execute(_select(User).where(User.tg_id == tg_id))).scalar_one_or_none()
            if not u:
                return {"error": f"user with tg_id={tg_id} not found"}
            locks = (await db.execute(
                _select(EscrowLock).where(EscrowLock.user_id == u.id, EscrowLock.status == "locked")
            )).scalars().all()
            ops = (await db.execute(
                _select(OperationLog).where(OperationLog.user_id == u.id)
                .order_by(OperationLog.created_at.desc()).limit(20)
            )).scalars().all()
            return {
                "user": {
                    "id": u.id, "tg_id": u.tg_id, "username": u.username,
                    "full_name": u.full_name,
                    "balance_usdt": float(u.balance_usdt or 0),
                    "kyc_status": u.kyc_status, "kyc_level": u.kyc_level,
                    "total_deals": u.total_deals or 0,
                    "completed_deals": u.completed_deals or 0,
                },
                "escrow_locks": [
                    {"id": l.id, "amount_usdt": float(l.amount_usdt or 0),
                     "reason": l.reason, "offer_id": l.offer_id, "deal_id": l.deal_id,
                     "status": l.status, "created_at": l.created_at.isoformat() if l.created_at else None}
                    for l in locks
                ],
                "locked_total_usdt": float(sum((l.amount_usdt or 0) for l in locks)),
                "recent_operations": [
                    {"id": o.id, "type": o.type, "amount_usdt": float(o.amount_usdt or 0),
                     "balance_after": float(o.balance_after) if o.balance_after else None,
                     "note": o.note,
                     "created_at": o.created_at.isoformat() if o.created_at else None}
                    for o in ops
                ],
                "operations_count": len(ops),
            }
    except Exception as e:
        import traceback
        return {"error": str(e), "traceback": traceback.format_exc()[-500:]}


@app.get("/debug/nuke_demo_offers")
async def debug_nuke_demo_offers():
    """Публичный nuke: удаляет ВСЕ active offers с username Alex_Pro/CryptoPro/NataKZ/P2P_Master.

    Открой эту URL один раз в браузере → возвращает JSON с количеством удалённых.
    """
    from sqlalchemy import text as _t
    from core.db import engine
    deleted = 0
    try:
        async with engine.begin() as conn:
            r = await conn.execute(_t(
                "DELETE FROM offers WHERE user_id IN ("
                " SELECT id FROM users WHERE LOWER(username) IN "
                "  ('alex_pro','cryptopro','natakz','p2p_master','pride official','maxtrader','denisexchange','nikitakursy')"
                " OR LOWER(full_name) IN "
                "  ('alex_pro','cryptopro','natakz','p2p_master','pride official','maxtrader','denisexchange','nikitakursy')"
                ")"
            ))
            try:
                deleted = r.rowcount if hasattr(r, "rowcount") else -1
            except Exception:
                deleted = -1
    except Exception as e:
        return {"error": str(e), "deleted": deleted}
    return {"ok": True, "deleted": deleted}


@app.get("/owner", response_class=HTMLResponse)
async def owner_panel():
    if OWNER_HTML.exists():
        return FileResponse(OWNER_HTML)
    return HTMLResponse("<h1>Owner panel not deployed</h1>", status_code=503)
