"""Авто-runner для periodic-выплат зарплат работникам.

Запускается из crm_bot._payment_reminder_loop каждые N часов
(N = storage.get_payout_safety().salary_schedule_hours, по умолчанию 24).

Логика run_salary_payouts():
  1. Безопасность: если auto_pay_enabled_global=False → выход
  2. Для каждого работника с compensation rules:
     - если pending_balance >= min_payout_amount
     - если pending_balance <= safety.max_per_tx_usdt
     - если общий daily_outbound + payout <= safety.max_daily_usdt
     - если есть usdt_address
     → отправляем через tron_payouts.send_usdt_to
     → reset pending
     → запись в accounting (salaries)
     → notify owner

  3. Если payout > max_per_tx_usdt — создаём notification "требуется ручной /payout"
  4. Daily limit достигнут → создаём notification "auto-pay приостановлен до завтра"
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Dict, List, Any

logger = logging.getLogger(__name__)


async def run_salary_payouts(reason: str = "scheduled") -> Dict[str, Any]:
    """Один тик — пробежать всех работников и выплатить если выполнены условия.

    Returns: {paid: [...], skipped: [...], errors: [...], total_paid_usdt: float}
    """
    from storage import storage

    result = {
        "paid": [], "skipped": [], "errors": [],
        "total_paid_usdt": 0.0, "reason": reason, "ts": time.time(),
    }

    # === Global safety ===
    safety = storage.get_payout_safety()
    if not safety.get("auto_pay_enabled_global", True):
        result["skipped"].append({"reason": "auto_pay_enabled_global=False (kill-switch)"})
        return result
    if not safety.get("auto_pay_salary_enabled", True):
        result["skipped"].append({"reason": "auto_pay_salary_enabled=False"})
        return result

    # === Проверка tron_payouts available ===
    try:
        from tron_payouts import is_configured, send_usdt_to
        if not is_configured():
            result["errors"].append({"reason": "TRON not configured (no env vars)"})
            try:
                await storage.add_notification(
                    type="warning",
                    text="🔒 Auto-pay не работает: TRON_PRIVATE_KEY/TRON_HOT_WALLET_ADDRESS не заданы в Railway env",
                    dedup_key="tron_not_configured",
                )
            except Exception:
                pass
            return result
    except ImportError:
        result["errors"].append({"reason": "tron_payouts module not available"})
        return result

    max_per_tx = float(safety.get("max_per_tx_usdt") or 500)
    max_daily = float(safety.get("max_daily_usdt") or 2000)
    daily_already = storage.get_daily_outbound_total()
    daily_budget_left = max(0, max_daily - daily_already)

    if daily_budget_left <= 0:
        result["skipped"].append({"reason": f"daily limit reached ({daily_already:.2f}/{max_daily} USDT)"})
        try:
            await storage.add_notification(
                type="warning",
                text=f"⏸ Auto-pay приостановлен: достигнут daily limit {daily_already:.2f}/{max_daily} USDT",
                dedup_key=f"daily_limit:{time.strftime('%Y-%m-%d')}",
            )
        except Exception:
            pass
        return result

    # === Пробегаем работников ===
    comps = storage.list_worker_compensations()
    for username, rules in comps.items():
        try:
            if not rules.get("auto_pay_enabled", True):
                result["skipped"].append({"username": username, "reason": "auto_pay_enabled=False"})
                continue

            pending = storage.get_worker_pending(username)
            min_payout = float(rules.get("min_payout_amount") or 10)
            if pending < min_payout:
                result["skipped"].append({"username": username, "reason": f"pending {pending:.2f}$ < min {min_payout}$"})
                continue

            address = storage.get_worker_usdt_address(username)
            if not address:
                result["skipped"].append({"username": username, "reason": "no usdt_address"})
                try:
                    await storage.add_notification(
                        type="warning",
                        text=f"⚠ @{username}: накоплено {pending:.2f}$, но нет USDT адреса. "
                             f"Установи через /set_usdt @{username} TR7N...",
                        dedup_key=f"no_address:{username}",
                    )
                except Exception:
                    pass
                continue

            # Проверка max_per_tx
            if pending > max_per_tx:
                result["skipped"].append({"username": username, "reason": f"pending {pending:.2f}$ > max_per_tx {max_per_tx}$ (требуется ручной /payout)"})
                try:
                    await storage.add_notification(
                        type="warning",
                        text=f"⚠ @{username}: {pending:.2f}$ превышает max_per_tx ${max_per_tx}. "
                             f"Выплати руками: /payout @{username} {pending:.2f}",
                        dedup_key=f"over_limit:{username}",
                    )
                except Exception:
                    pass
                continue

            # Проверка daily budget
            if pending > daily_budget_left:
                result["skipped"].append({"username": username, "reason": f"pending {pending:.2f}$ > daily budget left {daily_budget_left:.2f}$"})
                continue

            # === Отправляем! ===
            send_result = await send_usdt_to(
                to_address=address,
                amount_usdt=pending,
                reason=f"auto-salary @{username} ({reason})",
                wait_confirmation=False,  # не ждём — scheduler не должен висеть
                timeout_sec=30,
            )

            if not send_result.get("ok"):
                result["errors"].append({"username": username, "amount": pending, "error": send_result.get("error", "send failed")})
                continue

            tx_hash = send_result.get("tx_hash", "")
            # Reset pending
            await storage.reset_worker_pending(username, paid_amount=pending)
            # Запись в accounting
            try:
                await storage.add_accounting_entry(
                    category="salaries",
                    amount_usdt=pending,
                    note=f"auto-salary @{username} · tx:{tx_hash[:16]}...",
                    created_by="auto:scheduler",
                )
            except Exception as _e:
                logger.warning("acc record failed for %s: %s", username, _e)

            result["paid"].append({
                "username": username, "amount": pending,
                "tx_hash": tx_hash, "address": address,
            })
            result["total_paid_usdt"] += pending
            daily_budget_left -= pending

            # Notify owner
            try:
                await storage.add_notification(
                    type="success",
                    text=f"💸 Авто-зарплата отправлена @{username}: {pending:.2f}$\nTX: {tx_hash[:24]}...",
                )
            except Exception:
                pass

            # Pause между transactions чтобы не флудить network
            await asyncio.sleep(2)

        except Exception as e:
            logger.exception("auto-pay worker %s failed: %s", username, e)
            result["errors"].append({"username": username, "error": str(e)[:200]})

    # Summary notification если что-то реально выплатили
    if result["paid"]:
        try:
            await storage.add_notification(
                type="success",
                text=f"📊 Auto-pay tick: выплачено {len(result['paid'])} работников, всего {result['total_paid_usdt']:.2f}$ ({reason})",
                dedup_key=f"autopay_summary:{int(time.time())}",
            )
        except Exception:
            pass

    return result


async def maybe_run_salary_payouts(force: bool = False) -> Dict[str, Any]:
    """Версия для scheduler-а: запускает только если прошло >= salary_schedule_hours
    с последнего тика. Хранит timestamp в storage.state.last_salary_tick.
    """
    from storage import storage
    safety = storage.get_payout_safety()
    schedule_hours = float(safety.get("salary_schedule_hours") or 24)
    last_tick = float(storage.state.get("last_salary_tick") or 0)
    now = time.time()
    if not force:
        if (now - last_tick) < schedule_hours * 3600:
            return {"skipped": True, "reason": f"too soon (next in {schedule_hours*3600 - (now-last_tick):.0f} sec)"}
    result = await run_salary_payouts(reason=f"scheduled ({schedule_hours}h)")
    # Update last tick (даже если ничего не выплатилось — чтобы не долбить каждый цикл)
    try:
        async with __import__('storage').storage._lock if False else __import__('asyncio').Lock():
            pass
    except Exception:
        pass
    storage.state["last_salary_tick"] = now
    try:
        await storage.save()
    except Exception:
        pass
    return result
