"""Scheduler Worker (ТЗ Том 15).

Periodic tasks:
- TRADE_PAYMENT_TIMEOUT: если pay_deadline_at прошёл — auto-cancel
- TRADE_CONFIRM_TIMEOUT: если confirm_deadline_at прошёл — auto-release (system)
- DISPUTE_SLA: если диспут OPENED > N часов — нотификация админу
- IDEMPOTENCY_CLEANUP: удалить просроченные ключи
- RECONCILIATION: см. reconciliation.py (отдельный таск)
"""
from __future__ import annotations
import asyncio
import logging
from datetime import datetime, timezone, timedelta

from sqlalchemy import select

from core.db import AsyncSessionLocal
from p2p import idempotency, policies, wallet
from p2p.enums import AdvertisementStatus, AdvertisementType, DisputeStatus, TradeStatus
from p2p.models import P2PAdvertisement, P2PDispute, P2PTrade, P2PWallet
from p2p.workflows import cancel_trade, confirm_payment, pause_advertisement, resume_advertisement
from p2p.orchestrator import run_workflow

logger = logging.getLogger("p2p.worker.scheduler")


async def _check_payment_timeouts(db) -> int:
    """Найти WAITING_FOR_PAYMENT trades с истёкшим deadline → cancel."""
    now = datetime.now(timezone.utc)
    r = await db.execute(
        select(P2PTrade).where(
            P2PTrade.status == TradeStatus.WAITING_FOR_PAYMENT.value,
            P2PTrade.pay_deadline_at != None,  # noqa: E711
            P2PTrade.pay_deadline_at < now,
        ).limit(100)
    )
    expired = list(r.scalars().all())
    cnt = 0
    for t in expired:
        try:
            await run_workflow(
                db,
                workflow_type="cancel_trade",
                user_id=t.buyer_id,
                input_payload={"trade_id": t.id, "reason": "auto-timeout: payment deadline expired"},
                handler=cancel_trade.handle,
                actor_role="SYSTEM",
                source="scheduler:payment_timeout",
            )
            await db.commit()
            cnt += 1
        except Exception as e:
            await db.rollback()
            logger.warning("[scheduler] auto-cancel trade=%s failed: %s", t.id, e)
    return cnt


async def _check_confirm_timeouts(db) -> int:
    """Найти PAYMENT_MARKED trades с истёкшим confirm_deadline_at → auto-release.

    Особенности:
    - Если поле confirm_deadline_at NULL — считаем как payment_marked_at + TRADE_CONFIRM_TIMEOUT_MIN.
    - НЕ авто-релизим если по trade есть открытый dispute (OPENED/ARBITRATION).
    """
    now = datetime.now(timezone.utc)
    try:
        confirm_timeout_min = await policies.get_int(db, "TRADE_CONFIRM_TIMEOUT_MIN")
    except Exception:
        confirm_timeout_min = 20
    grace_threshold = now - timedelta(minutes=confirm_timeout_min)

    # Кандидаты — все PAYMENT_MARKED где либо confirm_deadline_at прошёл,
    # либо confirm_deadline_at NULL но payment_marked_at достаточно давно.
    r = await db.execute(
        select(P2PTrade).where(
            P2PTrade.status == TradeStatus.PAYMENT_MARKED.value,
        ).limit(200)
    )
    candidates = list(r.scalars().all())
    if not candidates:
        return 0

    cnt = 0
    for t in candidates:
        # Определяем эффективный deadline
        deadline = t.confirm_deadline_at
        if deadline and deadline.tzinfo is None:
            deadline = deadline.replace(tzinfo=timezone.utc)
        if deadline is None:
            # Computed: payment_marked_at + TRADE_CONFIRM_TIMEOUT_MIN
            pm = t.payment_marked_at
            if pm is None:
                # Нет ни confirm_deadline_at, ни payment_marked_at → пропустим
                continue
            if pm.tzinfo is None:
                pm = pm.replace(tzinfo=timezone.utc)
            deadline = pm + timedelta(minutes=confirm_timeout_min)
        if deadline >= now:
            continue  # ещё рано

        # Проверка на открытый dispute
        dq = await db.execute(
            select(P2PDispute).where(
                P2PDispute.trade_id == t.id,
                P2PDispute.status.in_(
                    (DisputeStatus.OPENED.value, DisputeStatus.ARBITRATION.value)
                ),
            )
        )
        if dq.scalar_one_or_none() is not None:
            logger.debug(
                "[scheduler] skip auto-release trade=%s (has open dispute)", t.id,
            )
            continue

        # Auto-release через confirm_payment workflow. seller_id как user_id
        # (формально), actor_role=SYSTEM.
        try:
            await run_workflow(
                db,
                workflow_type="confirm_payment",
                user_id=t.seller_id,
                input_payload={
                    "trade_id": t.id,
                    "system_auto_release": True,
                    "reason": "auto-release: confirm deadline expired",
                },
                handler=confirm_payment.handle,
                actor_role="SYSTEM",
                source="scheduler:confirm_timeout",
            )
            await db.commit()
            cnt += 1
            logger.info("[scheduler] auto-released trade=%s", t.id)
        except Exception as e:
            await db.rollback()
            logger.warning("[scheduler] auto-release trade=%s failed: %s", t.id, e)
    return cnt


async def _cleanup_idempotency(db) -> int:
    n = await idempotency.cleanup_expired(db)
    await db.commit()
    return n


async def _check_auto_pause_resume(db) -> tuple[int, int]:
    """TODO #10: Auto-pause/resume SELL объявлений по advertisement_hold balance.

    Логика:
    - SELL ad ACTIVE + AUTO_PAUSE_EMPTY_BALANCE + wallet.advertisement_hold < ad.available_amount
      → pause (reason='auto:empty_balance')
    - SELL ad PAUSED + AUTO_RESUME_ON_BALANCE + wallet.advertisement_hold >= ad.available_amount
      → resume

    Возвращает (paused_count, resumed_count).
    """
    try:
        auto_pause = await policies.get_bool(db, "AUTO_PAUSE_EMPTY_BALANCE")
        auto_resume = await policies.get_bool(db, "AUTO_RESUME_ON_BALANCE")
    except Exception:
        auto_pause = True
        auto_resume = True

    paused = 0
    resumed = 0

    if not (auto_pause or auto_resume):
        return (0, 0)

    # Берём активные/паузнутые SELL объявления (батч)
    r = await db.execute(
        select(P2PAdvertisement).where(
            P2PAdvertisement.type == AdvertisementType.SELL.value,
            P2PAdvertisement.status.in_((
                AdvertisementStatus.ACTIVE.value,
                AdvertisementStatus.PAUSED.value,
            )),
        ).limit(500)
    )
    ads = list(r.scalars().all())
    if not ads:
        return (0, 0)

    for ad in ads:
        try:
            wb = await wallet.get_breakdown(db, ad.owner_id, ad.crypto_currency)
            avail_needed = ad.available_amount or 0
            hold = wb.advertisement_hold or 0

            if (
                auto_pause
                and ad.status == AdvertisementStatus.ACTIVE.value
                and avail_needed > 0
                and hold < avail_needed
            ):
                # Эскроу меньше чем должно быть — pause
                try:
                    await run_workflow(
                        db,
                        workflow_type="pause_advertisement",
                        user_id=ad.owner_id,
                        input_payload={
                            "advertisement_id": ad.id,
                            "reason": "auto:empty_balance",
                        },
                        handler=pause_advertisement.handle,
                        actor_role="SYSTEM",
                        source="scheduler:auto_pause",
                    )
                    await db.commit()
                    paused += 1
                except Exception as e:
                    await db.rollback()
                    logger.warning(
                        "[scheduler] auto-pause ad=%s failed: %s", ad.id, e,
                    )
                continue

            if (
                auto_resume
                and ad.status == AdvertisementStatus.PAUSED.value
                and (ad.paused_reason or "").startswith("auto:")
                and hold >= avail_needed
                and avail_needed > 0
            ):
                # Эскроу восстановилось — resume (только то что paused-было автоматом)
                try:
                    await run_workflow(
                        db,
                        workflow_type="resume_advertisement",
                        user_id=ad.owner_id,
                        input_payload={"advertisement_id": ad.id},
                        handler=resume_advertisement.handle,
                        actor_role="SYSTEM",
                        source="scheduler:auto_resume",
                    )
                    await db.commit()
                    resumed += 1
                except Exception as e:
                    await db.rollback()
                    logger.warning(
                        "[scheduler] auto-resume ad=%s failed: %s", ad.id, e,
                    )
        except Exception as e:
            logger.warning("[scheduler] auto-pause/resume iter ad=%s failed: %s", ad.id, e)

    return (paused, resumed)


async def run() -> None:
    logger.info("[scheduler] started")
    last_idemp_cleanup = datetime.now(timezone.utc)
    while True:
        try:
            async with AsyncSessionLocal() as db:
                # payment timeouts — каждые 30 сек
                cnt = await _check_payment_timeouts(db)
                if cnt:
                    logger.info("[scheduler] auto-cancelled %d expired trades", cnt)

                # confirm timeouts — auto-release
                cnt2 = await _check_confirm_timeouts(db)
                if cnt2:
                    logger.info("[scheduler] auto-released %d trades", cnt2)

                # TODO #10: auto-pause/resume ad'ы по advertisement_hold
                try:
                    p_cnt, r_cnt = await _check_auto_pause_resume(db)
                    if p_cnt or r_cnt:
                        logger.info(
                            "[scheduler] auto-pause=%d auto-resume=%d",
                            p_cnt, r_cnt,
                        )
                except Exception as _ap_e:
                    logger.warning("[scheduler] auto-pause/resume failed: %s", _ap_e)

                # idempotency cleanup — раз в час
                now = datetime.now(timezone.utc)
                if (now - last_idemp_cleanup).total_seconds() > 3600:
                    n = await _cleanup_idempotency(db)
                    if n:
                        logger.info("[scheduler] cleaned %d expired idempotency keys", n)
                    last_idemp_cleanup = now
        except Exception as e:
            logger.exception("[scheduler] iteration failed: %s", e)
        await asyncio.sleep(30)
