"""Risk Engine (ТЗ Том 10).

Pre-trade / pre-ad inline проверки. Возвращают RiskAssessment с decision
(ALLOW / REVIEW / DENY), числовым score (0..100+) и списком reasons.

Каждая проверка увеличивает score. Финальный decision:
  score >= 100 → DENY
  score >=  50 → REVIEW
  иначе       → ALLOW

Дополнительно может выставляться флаг should_freeze, который вызывает
freeze_user() — все available средства уходят в USER_FROZEN. Используется
при критических срабатываниях (например, blacklist tg_id).
"""
from __future__ import annotations
import logging
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from typing import NamedTuple

from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from core.config import settings
from p2p import audit, ledger, outbox, policies, wallet
from p2p.enums import (
    EventType, LedgerAccountType, RiskDecision, TradeStatus,
)
from p2p.models import P2PTrade, P2PWallet, P2PDispute

logger = logging.getLogger("p2p.risk")


# ═══════════════════════════════════════════════════════════════════════
# RiskAssessment
# ═══════════════════════════════════════════════════════════════════════

class RiskAssessment(NamedTuple):
    decision: str          # RiskDecision.ALLOW/REVIEW/DENY value
    score: int             # 0..100+, выше = рискованнее
    reasons: list[str]
    should_freeze: bool    # принудительно заморозить юзера

    def is_deny(self) -> bool:
        return self.decision == RiskDecision.DENY.value

    def is_review(self) -> bool:
        return self.decision == RiskDecision.REVIEW.value


def _new_assessment() -> RiskAssessment:
    """Пустая ALLOW-оценка."""
    return RiskAssessment(
        decision=RiskDecision.ALLOW.value,
        score=0, reasons=[], should_freeze=False,
    )


def _finalize(score: int, reasons: list[str], should_freeze: bool) -> RiskAssessment:
    """Привести score → decision."""
    if score >= 100 or should_freeze:
        decision = RiskDecision.DENY.value
    elif score >= 50:
        decision = RiskDecision.REVIEW.value
    else:
        decision = RiskDecision.ALLOW.value
    return RiskAssessment(
        decision=decision, score=score, reasons=reasons, should_freeze=should_freeze,
    )


# ═══════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════

def _get_blacklist_tg_ids() -> set[int]:
    """Распарсить P2P_BLACKLIST_TG_IDS из settings — comma-separated."""
    raw = getattr(settings, "P2P_BLACKLIST_TG_IDS", "") or ""
    out: set[int] = set()
    for tok in str(raw).split(","):
        tok = tok.strip()
        if tok.isdigit():
            try:
                out.add(int(tok))
            except Exception:
                pass
    return out


async def _user_tg_id(db: AsyncSession, user_id: int) -> int | None:
    """Достать tg_id пользователя (через core.models.User)."""
    try:
        from core.models import User as CoreUser  # type: ignore
    except Exception:
        return None
    try:
        u = await db.get(CoreUser, user_id)
        return int(u.tg_id) if u and u.tg_id is not None else None
    except Exception:
        return None


async def _is_user_in_blacklist(db: AsyncSession, user_id: int) -> bool:
    bl = _get_blacklist_tg_ids()
    if not bl:
        return False
    tg = await _user_tg_id(db, user_id)
    return tg is not None and tg in bl


async def _user_frozen_or_blocked(db: AsyncSession, user_id: int) -> bool:
    """Проверка: либо P2PWallet[user_id].frozen > 0, либо user.is_blocked/kyc_status=banned."""
    # Wallet frozen — берём USDT по умолчанию (мульти-валют не критичен на этом этапе)
    try:
        r = await db.execute(
            select(P2PWallet).where(P2PWallet.user_id == user_id)
        )
        for w in r.scalars().all():
            if (w.frozen or Decimal("0")) > 0:
                return True
    except Exception:
        pass

    # User flags
    try:
        from core.models import User as CoreUser  # type: ignore
        u = await db.get(CoreUser, user_id)
        if u is None:
            return False
        if getattr(u, "is_blocked", False):
            return True
        if (getattr(u, "kyc_status", "") or "").lower() == "banned":
            return True
    except Exception:
        return False
    return False


async def _convert_to_usdt(db: AsyncSession, amount: Decimal, currency: str) -> Decimal:
    """Конвертация в USDT через settings_kv. Если не получилось — возвращает amount как есть."""
    cur = (currency or "USDT").upper()
    if cur == "USDT":
        return amount
    try:
        # Для фиатных — делим на rate (rate = fiat_per_usdt)
        from core.services import settings_kv
        if cur == "RUB":
            rate = await settings_kv.get_rate_sell(db)
            if rate and rate > 0:
                return (amount / rate).quantize(Decimal("0.0001"))
    except Exception:
        pass
    return amount


async def _daily_trade_stats(
    db: AsyncSession, user_id: int, since: datetime,
) -> tuple[int, Decimal]:
    """(count, sum_crypto_in_usdt) — все trades юзера (buyer OR seller) за since...now.

    Статусы: только активные / завершённые (не CANCELLED).
    """
    statuses = [
        TradeStatus.CREATED.value, TradeStatus.ESCROW_LOCKED.value,
        TradeStatus.WAITING_FOR_PAYMENT.value, TradeStatus.PAYMENT_MARKED.value,
        TradeStatus.PAYMENT_CONFIRMATION.value, TradeStatus.DISPUTE_OPENED.value,
        TradeStatus.ARBITRATION.value, TradeStatus.RESOLVED.value,
        TradeStatus.COMPLETED.value,
    ]
    r = await db.execute(
        select(P2PTrade).where(
            ((P2PTrade.buyer_id == user_id) | (P2PTrade.seller_id == user_id)),
            P2PTrade.created_at >= since,
            P2PTrade.status.in_(statuses),
        )
    )
    rows = list(r.scalars().all())
    cnt = len(rows)
    total = Decimal("0")
    for t in rows:
        amt = t.crypto_amount or Decimal("0")
        cur = t.crypto_currency or "USDT"
        try:
            total += await _convert_to_usdt(db, Decimal(str(amt)), cur)
        except Exception:
            total += Decimal(str(amt))
    return cnt, total


async def _trades_count_since(
    db: AsyncSession, user_id: int, since: datetime,
    statuses: list[str] | None = None,
) -> int:
    q = select(func.count(P2PTrade.id)).where(
        ((P2PTrade.buyer_id == user_id) | (P2PTrade.seller_id == user_id)),
        P2PTrade.created_at >= since,
    )
    if statuses is not None:
        q = q.where(P2PTrade.status.in_(statuses))
    r = await db.execute(q)
    return int(r.scalar() or 0)


# ═══════════════════════════════════════════════════════════════════════
# 1) assess_create_trade
# ═══════════════════════════════════════════════════════════════════════

async def assess_create_trade(
    db: AsyncSession,
    *,
    user_id: int,
    amount_crypto: Decimal,
    amount_fiat: Decimal,
    advertisement_id: str,
    currency: str = "USDT",
) -> RiskAssessment:
    """Inline pre-check перед созданием trade.

    Не делает побочных эффектов (не пишет audit/outbox) — только возвращает оценку.
    """
    score = 0
    reasons: list[str] = []
    should_freeze = False
    now = datetime.now(timezone.utc)

    if not isinstance(amount_crypto, Decimal):
        amount_crypto = Decimal(str(amount_crypto or "0"))

    # 1. Blacklist
    if await _is_user_in_blacklist(db, user_id):
        score += 100
        should_freeze = True
        reasons.append("blacklist:tg_id")

    # 2. Frozen / blocked user
    if await _user_frozen_or_blocked(db, user_id):
        score += 100
        reasons.append("user.frozen_or_blocked")

    # 3. Trade amount sanity (defense-in-depth)
    try:
        min_amt = await policies.get_decimal(db, "MIN_TRADE_AMOUNT_USDT")
    except Exception:
        min_amt = Decimal("10")
    try:
        max_amt = await policies.get_decimal(db, "MAX_TRADE_AMOUNT_USDT")
    except Exception:
        max_amt = Decimal("50000")

    amount_usdt = await _convert_to_usdt(db, amount_crypto, currency)
    if amount_usdt < min_amt:
        score += 100
        reasons.append(f"amount.below_min:{amount_usdt}<{min_amt}")
    if amount_usdt > max_amt:
        score += 100
        reasons.append(f"amount.above_max:{amount_usdt}>{max_amt}")

    # 4. Daily volume / count
    since_24h = now - timedelta(hours=24)
    try:
        day_cnt, day_vol = await _daily_trade_stats(db, user_id, since_24h)
    except Exception as e:
        logger.warning("[risk] daily stats failed user=%s: %s", user_id, e)
        day_cnt, day_vol = 0, Decimal("0")

    try:
        max_vol = await policies.get_decimal(db, "RISK_MAX_DAILY_VOLUME_USDT")
    except Exception:
        max_vol = Decimal("100000")
    try:
        max_cnt = await policies.get_int(db, "RISK_MAX_DAILY_TRADES")
    except Exception:
        max_cnt = 50

    projected_vol = day_vol + amount_usdt
    if projected_vol > max_vol:
        score += 100
        reasons.append(f"daily_volume.exceeded:{projected_vol}>{max_vol}")
    elif projected_vol > max_vol * Decimal("0.8"):
        score += 30
        reasons.append(f"daily_volume.near_limit:{projected_vol}/{max_vol}")

    if day_cnt + 1 > max_cnt:
        score += 100
        reasons.append(f"daily_count.exceeded:{day_cnt+1}>{max_cnt}")
    elif day_cnt + 1 > max_cnt * 0.8:
        score += 20
        reasons.append(f"daily_count.near_limit:{day_cnt+1}/{max_cnt}")

    # 5. Velocity — >5 trades за последние 60 секунд (boт?)
    try:
        velocity_cnt = await _trades_count_since(db, user_id, now - timedelta(seconds=60))
    except Exception:
        velocity_cnt = 0
    if velocity_cnt > 5:
        score += 50
        reasons.append(f"velocity:{velocity_cnt}/60s")

    return _finalize(score, reasons, should_freeze)


# ═══════════════════════════════════════════════════════════════════════
# 2) assess_create_advertisement
# ═══════════════════════════════════════════════════════════════════════

async def assess_create_advertisement(
    db: AsyncSession,
    *,
    user_id: int,
    amount_total: Decimal,
    price: Decimal,
    type: str,
    currency_pair: tuple[str, str] = ("USDT", "RUB"),
) -> RiskAssessment:
    """Pre-check перед публикацией advertisement."""
    score = 0
    reasons: list[str] = []
    should_freeze = False

    if not isinstance(amount_total, Decimal):
        amount_total = Decimal(str(amount_total or "0"))
    if not isinstance(price, Decimal):
        price = Decimal(str(price or "0"))

    # 1. Blacklist
    if await _is_user_in_blacklist(db, user_id):
        score += 100
        should_freeze = True
        reasons.append("blacklist:tg_id")

    # 2. Frozen / blocked user
    if await _user_frozen_or_blocked(db, user_id):
        score += 100
        reasons.append("user.frozen_or_blocked")

    # 3. Price deviation
    crypto, fiat = currency_pair
    try:
        max_dev_raw = await policies.get_policy(db, "MAX_PRICE_DEVIATION_PCT")
        max_dev = Decimal(str(max_dev_raw))
    except Exception:
        max_dev = Decimal("15")

    market_price: Decimal | None = None
    try:
        from core.services import settings_kv
        if (crypto or "").upper() == "USDT" and (fiat or "").upper() == "RUB":
            ad_type = (type or "").upper()
            if ad_type == "SELL":
                market_price = await settings_kv.get_rate_buy(db)
            else:
                market_price = await settings_kv.get_rate_sell(db)
    except Exception:
        market_price = None

    if market_price and market_price > 0 and price > 0:
        deviation_pct = ((price - market_price) / market_price * Decimal("100")).copy_abs()
        hard_limit = max_dev * Decimal("2")
        if deviation_pct > hard_limit:
            score += 100
            reasons.append(f"price.deviation.hard:{deviation_pct:.2f}%>{hard_limit}%")
        elif deviation_pct > max_dev:
            score += 30
            reasons.append(f"price.deviation:{deviation_pct:.2f}%>{max_dev}%")

    # 4. Очень крупный объём
    amount_usdt = await _convert_to_usdt(db, amount_total, crypto or "USDT")
    if amount_usdt > Decimal("1000000"):
        score += 50
        reasons.append(f"total.huge:{amount_usdt}>1000000")

    return _finalize(score, reasons, should_freeze)


# ═══════════════════════════════════════════════════════════════════════
# 3) assess_dispute_open
# ═══════════════════════════════════════════════════════════════════════

async def assess_dispute_open(
    db: AsyncSession,
    *,
    user_id: int,
    trade_id: str,
) -> RiskAssessment:
    """Pre-check перед открытием диспута: ловим abuse-паттерн."""
    score = 0
    reasons: list[str] = []
    should_freeze = False
    now = datetime.now(timezone.utc)

    # 1. > 3 диспутов за 24ч → DENY
    try:
        since_24h = now - timedelta(hours=24)
        r = await db.execute(
            select(func.count(P2PDispute.id)).where(
                P2PDispute.opened_by_id == user_id,
                P2PDispute.created_at >= since_24h,
            )
        )
        disputes_24h = int(r.scalar() or 0)
    except Exception:
        disputes_24h = 0

    if disputes_24h > 3:
        score += 100
        reasons.append(f"disputes.24h:{disputes_24h}>3")

    # 2. > 30% disputed из последних 20 трейдов → REVIEW
    try:
        r = await db.execute(
            select(P2PTrade.id).where(
                ((P2PTrade.buyer_id == user_id) | (P2PTrade.seller_id == user_id))
            ).order_by(P2PTrade.created_at.desc()).limit(20)
        )
        recent_ids = [row[0] for row in r.all()]
    except Exception:
        recent_ids = []

    if recent_ids:
        try:
            r = await db.execute(
                select(func.count(P2PDispute.id)).where(
                    P2PDispute.trade_id.in_(recent_ids),
                    P2PDispute.opened_by_id == user_id,
                )
            )
            disputed = int(r.scalar() or 0)
        except Exception:
            disputed = 0
        ratio = disputed / max(1, len(recent_ids))
        if ratio > 0.3:
            score += 50
            reasons.append(f"dispute_ratio:{ratio:.2f}>0.30")

    return _finalize(score, reasons, should_freeze)


# ═══════════════════════════════════════════════════════════════════════
# 4) freeze_user
# ═══════════════════════════════════════════════════════════════════════

async def freeze_user(
    db: AsyncSession,
    *,
    user_id: int,
    reason: str,
    by_user_id: int | None = None,
) -> None:
    """Принудительно заморозить все available средства юзера во всех валютах.

    Использует ledger.freeze_balance для каждой валюты, после — обновляет
    wallet projection. Пишет audit + emit WALLET_FROZEN per currency.
    """
    # Соберём все валюты пользователя — берём из p2p_wallets
    currencies: set[str] = set()
    try:
        r = await db.execute(
            select(P2PWallet).where(P2PWallet.user_id == user_id)
        )
        for w in r.scalars().all():
            currencies.add(w.currency or "USDT")
    except Exception:
        pass
    if not currencies:
        currencies.add("USDT")

    frozen_summary: dict[str, str] = {}
    for cur in sorted(currencies):
        try:
            br = await wallet.get_breakdown(db, user_id, cur)
            avail = br.available or Decimal("0")
        except Exception as e:
            logger.warning("[risk.freeze_user] breakdown failed user=%s cur=%s: %s",
                           user_id, cur, e)
            continue
        if avail > 0:
            try:
                await ledger.freeze_balance(
                    db, user_id=user_id, currency=cur, amount=avail,
                    reason=reason,
                )
                await wallet.update_wallet_from_ledger(db, user_id, cur)
                frozen_summary[cur] = str(avail)
            except Exception as e:
                logger.exception("[risk.freeze_user] ledger freeze failed user=%s cur=%s: %s",
                                 user_id, cur, e)
                continue

        # Audit + outbox даже если ничего не было заморожено (важен сам факт freeze)
        try:
            await audit.log(
                db,
                action="user.frozen",
                entity_type="user",
                entity_id=str(user_id),
                actor_id=by_user_id,
                actor_role="SYSTEM",
                new_state={
                    "user_id": user_id,
                    "currency": cur,
                    "amount_frozen": str(avail),
                    "reason": reason,
                },
            )
        except Exception as e:
            logger.warning("[risk.freeze_user] audit failed: %s", e)

        try:
            await outbox.emit(
                db,
                event_type=EventType.WALLET_FROZEN.value,
                payload={
                    "user_id": user_id, "currency": cur,
                    "amount": str(avail), "reason": reason,
                    "by": by_user_id,
                },
                aggregate_type="wallet",
                aggregate_id=f"{user_id}:{cur}",
            )
        except Exception as e:
            logger.warning("[risk.freeze_user] outbox failed: %s", e)

    logger.warning(
        "[risk.freeze_user] user=%s reason=%s frozen=%s by=%s",
        user_id, reason, frozen_summary, by_user_id,
    )
