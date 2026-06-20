"""Fraud Engine (ТЗ Том 11).

Post-trade async анализ паттернов мошенничества. Запускается background
worker'ом каждые 5 минут. Не блокирует pre-trade flow — только генерирует
алерты для арбитров/админов.

Каждый детектор возвращает list[FraudAlert] — пустой если ничего не нашли.
Алерты пишутся в audit_log + emit SYSTEM_ALERT через outbox.
"""
from __future__ import annotations
import asyncio
import logging
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from typing import NamedTuple

from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from p2p import audit, outbox, policies
from p2p.enums import EventType, TradeStatus
from p2p.models import P2PTrade

logger = logging.getLogger("p2p.fraud")


# ═══════════════════════════════════════════════════════════════════════
# FraudAlert
# ═══════════════════════════════════════════════════════════════════════

class FraudAlert(NamedTuple):
    severity: str          # LOW / MEDIUM / HIGH / CRITICAL
    pattern: str           # MULTI_ACCOUNT / WASH_TRADE / STRUCTURING / RAPID_CASHOUT / NEW_USER_LARGE
    user_ids: list[int]
    trade_ids: list[str]
    description: str
    score: int

    def dedup_key(self) -> str:
        """Hash для дедупликации в кеше."""
        uids = ",".join(str(x) for x in sorted(self.user_ids))
        tids = ",".join(sorted(self.trade_ids))
        return f"{self.pattern}|{uids}|{tids}"


# ═══════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════

async def _market_price_usdt_rub(db: AsyncSession) -> Decimal | None:
    """Текущая рыночная цена USDT/RUB (среднее buy/sell). None если недоступно."""
    try:
        from core.services import settings_kv
        buy = await settings_kv.get_rate_buy(db)
        sell = await settings_kv.get_rate_sell(db)
        if buy and sell and buy > 0 and sell > 0:
            return (Decimal(str(buy)) + Decimal(str(sell))) / Decimal("2")
    except Exception:
        return None
    return None


async def _get_user_created_at(db: AsyncSession, user_id: int) -> datetime | None:
    try:
        from core.models import User as CoreUser  # type: ignore
        u = await db.get(CoreUser, user_id)
        return getattr(u, "created_at", None) if u else None
    except Exception:
        return None


# ═══════════════════════════════════════════════════════════════════════
# 1) detect_wash_trade
# ═══════════════════════════════════════════════════════════════════════

async def detect_wash_trade(db: AsyncSession, since: datetime) -> list[FraudAlert]:
    """Wash-trading паттерны."""
    alerts: list[FraudAlert] = []

    # 1a. Явный self-trade: buyer_id == seller_id (БД позволяет? скорее нет, но проверяем)
    try:
        r = await db.execute(
            select(P2PTrade).where(
                P2PTrade.created_at >= since,
                P2PTrade.buyer_id == P2PTrade.seller_id,
            ).limit(100)
        )
        for t in r.scalars().all():
            alerts.append(FraudAlert(
                severity="CRITICAL", pattern="WASH_TRADE",
                user_ids=[t.buyer_id],
                trade_ids=[t.id],
                description=f"self-trade: buyer==seller=={t.buyer_id} trade={t.trade_number}",
                score=100,
            ))
    except Exception as e:
        logger.warning("[fraud] wash self-trade scan failed: %s", e)

    # 1b. Пары юзеров, торгующие только между собой > 3 раз за месяц
    try:
        month_ago = datetime.now(timezone.utc) - timedelta(days=30)
        # Получим все trades за месяц одним запросом
        r = await db.execute(
            select(P2PTrade.id, P2PTrade.buyer_id, P2PTrade.seller_id).where(
                P2PTrade.created_at >= month_ago,
                P2PTrade.buyer_id != P2PTrade.seller_id,
            )
        )
        pair_trades: dict[tuple[int, int], list[str]] = defaultdict(list)
        # Также — все партнёры каждого юзера (для проверки "только между собой")
        user_partners: dict[int, set[int]] = defaultdict(set)
        for tid, b, s in r.all():
            key = (min(b, s), max(b, s))
            pair_trades[key].append(tid)
            user_partners[b].add(s)
            user_partners[s].add(b)

        for (u1, u2), tids in pair_trades.items():
            if len(tids) <= 3:
                continue
            # "Только между собой" — у каждого только 1 партнёр (друг друга)
            if len(user_partners[u1]) == 1 and len(user_partners[u2]) == 1:
                # И недавняя активность попадает в окно since
                recent = tids[:50]
                alerts.append(FraudAlert(
                    severity="HIGH", pattern="WASH_TRADE",
                    user_ids=[u1, u2],
                    trade_ids=recent,
                    description=f"closed-pair wash: {u1}<->{u2} trades={len(tids)} (30d, no other partners)",
                    score=80,
                ))
    except Exception as e:
        logger.warning("[fraud] wash pair scan failed: %s", e)

    # 1c. Trades с ценой >20% дешевле рынка (для COMPLETED)
    try:
        market = await _market_price_usdt_rub(db)
        if market and market > 0:
            threshold = market * Decimal("0.8")
            r = await db.execute(
                select(P2PTrade).where(
                    P2PTrade.created_at >= since,
                    P2PTrade.crypto_currency == "USDT",
                    P2PTrade.fiat_currency == "RUB",
                    P2PTrade.price < threshold,
                ).limit(50)
            )
            for t in r.scalars().all():
                alerts.append(FraudAlert(
                    severity="MEDIUM", pattern="WASH_TRADE",
                    user_ids=[t.buyer_id, t.seller_id],
                    trade_ids=[t.id],
                    description=(
                        f"price below market: trade={t.trade_number} "
                        f"price={t.price} market={market:.2f} threshold={threshold:.2f}"
                    ),
                    score=40,
                ))
    except Exception as e:
        logger.warning("[fraud] wash price scan failed: %s", e)

    return alerts


# ═══════════════════════════════════════════════════════════════════════
# 2) detect_structuring
# ═══════════════════════════════════════════════════════════════════════

async def detect_structuring(db: AsyncSession, since: datetime) -> list[FraudAlert]:
    """>10 трейдов/час, каждый ≤ MIN_TRADE_AMOUNT_USDT * 1.1 — дробление."""
    alerts: list[FraudAlert] = []
    try:
        min_amt = await policies.get_decimal(db, "MIN_TRADE_AMOUNT_USDT")
    except Exception:
        min_amt = Decimal("10")
    threshold = min_amt * Decimal("1.1")

    try:
        hour_ago = datetime.now(timezone.utc) - timedelta(hours=1)
        scan_since = max(since, hour_ago)
        r = await db.execute(
            select(P2PTrade.id, P2PTrade.buyer_id, P2PTrade.seller_id,
                   P2PTrade.crypto_amount, P2PTrade.crypto_currency).where(
                P2PTrade.created_at >= scan_since,
            )
        )
        # group by user (буду считать со стороны buyer; для seller отдельно)
        per_user: dict[int, list[tuple[str, Decimal]]] = defaultdict(list)
        for tid, b, s, amt, cur in r.all():
            if (cur or "USDT").upper() != "USDT":
                continue
            amt_d = Decimal(str(amt or 0))
            if amt_d <= threshold:
                per_user[b].append((tid, amt_d))
                per_user[s].append((tid, amt_d))

        for uid, items in per_user.items():
            if len(items) > 10:
                alerts.append(FraudAlert(
                    severity="HIGH", pattern="STRUCTURING",
                    user_ids=[uid],
                    trade_ids=[tid for tid, _ in items[:30]],
                    description=(
                        f"structuring: user={uid} {len(items)} trades<={threshold} "
                        f"USDT in last 1h"
                    ),
                    score=70,
                ))
    except Exception as e:
        logger.warning("[fraud] structuring scan failed: %s", e)
    return alerts


# ═══════════════════════════════════════════════════════════════════════
# 3) detect_rapid_cashout
# ═══════════════════════════════════════════════════════════════════════

async def detect_rapid_cashout(db: AsyncSession, since: datetime) -> list[FraudAlert]:
    """>5 cancelled trades за 1 час от одного юзера — подозрение на cashout."""
    alerts: list[FraudAlert] = []
    try:
        hour_ago = datetime.now(timezone.utc) - timedelta(hours=1)
        scan_since = max(since, hour_ago)
        r = await db.execute(
            select(P2PTrade.id, P2PTrade.buyer_id, P2PTrade.seller_id).where(
                P2PTrade.created_at >= scan_since,
                P2PTrade.status == TradeStatus.CANCELLED.value,
            )
        )
        per_user: dict[int, list[str]] = defaultdict(list)
        for tid, b, s in r.all():
            per_user[b].append(tid)
            if s != b:
                per_user[s].append(tid)
        for uid, tids in per_user.items():
            if len(tids) > 5:
                alerts.append(FraudAlert(
                    severity="MEDIUM", pattern="RAPID_CASHOUT",
                    user_ids=[uid],
                    trade_ids=tids[:30],
                    description=(
                        f"rapid cashout: user={uid} cancelled={len(tids)} trades in last 1h"
                    ),
                    score=50,
                ))
    except Exception as e:
        logger.warning("[fraud] rapid_cashout scan failed: %s", e)
    return alerts


# ═══════════════════════════════════════════════════════════════════════
# 4) detect_new_user_large_amount
# ═══════════════════════════════════════════════════════════════════════

async def detect_new_user_large_amount(
    db: AsyncSession, since: datetime,
) -> list[FraudAlert]:
    """Юзер <24ч с момента регистрации делает trade > 1000 USDT."""
    alerts: list[FraudAlert] = []
    try:
        r = await db.execute(
            select(P2PTrade).where(
                P2PTrade.created_at >= since,
                P2PTrade.crypto_currency == "USDT",
                P2PTrade.crypto_amount > Decimal("1000"),
            ).limit(200)
        )
        trades = list(r.scalars().all())
    except Exception as e:
        logger.warning("[fraud] new_user_large select failed: %s", e)
        return alerts

    # Кеш user.created_at чтобы не дёргать БД повторно
    user_created: dict[int, datetime | None] = {}

    for t in trades:
        for uid in {t.buyer_id, t.seller_id}:
            if uid not in user_created:
                user_created[uid] = await _get_user_created_at(db, uid)
            created = user_created[uid]
            if created is None:
                continue
            # Нормализация tz
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            age = t.created_at.replace(tzinfo=timezone.utc) - created if t.created_at.tzinfo is None else t.created_at - created
            if age < timedelta(hours=24):
                amt = Decimal(str(t.crypto_amount or 0))
                if amt > Decimal("10000"):
                    sev, score = "HIGH", 80
                elif amt > Decimal("1000"):
                    sev, score = "MEDIUM", 50
                else:
                    continue
                alerts.append(FraudAlert(
                    severity=sev, pattern="NEW_USER_LARGE",
                    user_ids=[uid],
                    trade_ids=[t.id],
                    description=(
                        f"new user large trade: user={uid} amount={amt} USDT "
                        f"age={age.total_seconds()/3600:.1f}h"
                    ),
                    score=score,
                ))
    return alerts


# ═══════════════════════════════════════════════════════════════════════
# 5) run_all_detectors
# ═══════════════════════════════════════════════════════════════════════

async def run_all_detectors(db: AsyncSession, since: datetime) -> list[FraudAlert]:
    """Запускает все 4 детектора параллельно через asyncio.gather.

    Если один из них падает — возвращает результаты остальных.
    """
    results = await asyncio.gather(
        detect_wash_trade(db, since),
        detect_structuring(db, since),
        detect_rapid_cashout(db, since),
        detect_new_user_large_amount(db, since),
        return_exceptions=True,
    )
    alerts: list[FraudAlert] = []
    for r in results:
        if isinstance(r, Exception):
            logger.warning("[fraud] detector raised: %s", r)
            continue
        alerts.extend(r)
    return alerts


# ═══════════════════════════════════════════════════════════════════════
# 6) record_alert
# ═══════════════════════════════════════════════════════════════════════

async def record_alert(db: AsyncSession, alert: FraudAlert) -> None:
    """Записать алерт в audit_log + emit SYSTEM_ALERT через outbox."""
    payload = {
        "severity": alert.severity,
        "pattern": alert.pattern,
        "user_ids": alert.user_ids,
        "trade_ids": alert.trade_ids,
        "description": alert.description,
        "score": alert.score,
    }
    try:
        await audit.log(
            db,
            action="fraud.alert",
            entity_type="fraud_alert",
            entity_id=alert.dedup_key()[:64],
            actor_id=None,
            actor_role="SYSTEM",
            new_state=payload,
        )
    except Exception as e:
        logger.warning("[fraud.record_alert] audit failed: %s", e)
    try:
        await outbox.emit(
            db,
            event_type=EventType.SYSTEM_ALERT.value,
            payload={"kind": "fraud", **payload},
            aggregate_type="fraud_alert",
            aggregate_id=alert.dedup_key()[:64],
        )
    except Exception as e:
        logger.warning("[fraud.record_alert] outbox failed: %s", e)
    logger.warning(
        "[fraud.ALERT] %s/%s users=%s trades=%d score=%d :: %s",
        alert.severity, alert.pattern, alert.user_ids,
        len(alert.trade_ids), alert.score, alert.description,
    )
