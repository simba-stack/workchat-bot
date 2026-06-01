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

            # === НОВАЯ СХЕМА (июнь 2026): начисляем на CRM-баланс работника,
            # а не шлём напрямую. Работник сам запросит вывод когда захочет —
            # это унифицирует все выплаты через один поток (balance withdrawal).
            user_key = storage._balance_key_worker(username)
            tx_id = await storage.accrue_to_balance(
                user_key, pending,
                tx_type="salary", ref="",
                note=f"Зарплата ({reason})",
                status="available",  # зп сразу доступна к выводу
            )
            # Сохраняем адрес в баланс если работник его задавал ранее (для удобства)
            try:
                cur_b = storage.get_balance(user_key)
                if not cur_b.get("usdt_address") and address:
                    await storage.set_balance_address(user_key, address=address)
            except Exception:
                pass
            # Reset pending в worker_pending_balance (мигрировано на crm_balance)
            await storage.reset_worker_pending(username, paid_amount=pending)
            # Запись в accounting
            try:
                await storage.add_accounting_entry(
                    category="salaries",
                    amount_usdt=pending,
                    note=f"@{username} → crm_balance (доступно к выводу, tx_id={tx_id})",
                    created_by="auto:scheduler",
                )
            except Exception as _e:
                logger.warning("acc record failed for %s: %s", username, _e)

            result["paid"].append({
                "username": username, "amount": pending,
                "accrued_to_balance": True, "address": address,
            })
            result["total_paid_usdt"] += pending

            # Notify owner + работника
            try:
                await storage.add_notification(
                    type="success",
                    text=f"💼 Зарплата начислена на баланс @{username}: {pending:.2f}$ "
                         f"(работник может вывести в CRM-боте)",
                )
            except Exception:
                pass

            # Pause между записями
            await asyncio.sleep(0.2)

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


# ============================================================================
# Balance Withdrawals (B-часть: CRM-баланс с выводом)
# ============================================================================

async def process_withdrawal(req_id: str, approved_by: str = "") -> tuple:
    """Выполняет фактическую выплату по заявке withdrawal.

    Используется:
      - автоматически из api.balance_withdraw если amount <= auto_threshold
      - вручную при apruv owner'а из JARVIS

    Returns: (ok: bool, tx_hash: str)
    """
    from storage import storage
    storage.reload_sync()
    reqs = storage.state.get("balance_withdrawal_requests") or {}
    r = reqs.get(req_id)
    if not r:
        logger.warning("[withdraw] req=%s not found", req_id)
        return False, ""
    if r.get("status") not in ("pending", "approved"):
        logger.warning("[withdraw] req=%s status=%s, skipping", req_id, r.get("status"))
        return False, ""

    user_key = r.get("user_key") or ""
    amount = float(r.get("amount_usdt") or 0)
    address = (r.get("address") or "").strip()
    if not address or amount <= 0:
        await storage.cancel_withdrawal(req_id, by=approved_by or "auto")
        return False, ""

    # Safety
    safety = storage.get_payout_safety()
    if not safety.get("auto_pay_enabled_global", True):
        logger.warning("[withdraw] kill-switch on — skipping req=%s", req_id)
        return False, ""
    max_per_tx = float(safety.get("max_per_tx_usdt") or 500)
    if amount > max_per_tx:
        logger.warning("[withdraw] req=%s amount %.2f > max_per_tx %.2f", req_id, amount, max_per_tx)
        try:
            await storage.add_notification(
                type="warning",
                text=f"⚠ Заявка на вывод #{req_id}: {amount:.2f}$ > max_per_tx {max_per_tx}$. "
                     f"Требуется ручной /payout или повышение лимита.",
                dedup_key=f"wd_over_limit:{req_id}",
            )
        except Exception:
            pass
        return False, ""
    max_daily = float(safety.get("max_daily_usdt") or 2000)
    daily_already = storage.get_daily_outbound_total()
    if (daily_already + amount) > max_daily:
        logger.warning("[withdraw] daily limit: %.2f + %.2f > %.2f", daily_already, amount, max_daily)
        try:
            await storage.add_notification(
                type="warning",
                text=f"⏸ Заявка #{req_id} отложена: дневной лимит исчерпан.",
                dedup_key=f"wd_daily:{req_id}",
            )
        except Exception:
            pass
        return False, ""

    # Tron available?
    try:
        from tron_payouts import is_configured, send_usdt_to
    except ImportError:
        return False, ""
    if not is_configured():
        return False, ""

    # Send!
    try:
        send_result = await send_usdt_to(
            to_address=address,
            amount_usdt=amount,
            reason=f"withdraw {req_id} for {user_key}",
            wait_confirmation=False,
            timeout_sec=30,
        )
    except Exception as e:
        logger.exception("[withdraw] send failed: %s", e)
        await storage.update_withdrawal(req_id, status="failed", note=f"send error: {e}")
        return False, ""

    if not send_result.get("ok"):
        err = send_result.get("error") or "send failed"
        await storage.update_withdrawal(req_id, status="failed", note=err)
        try:
            await storage.add_notification(
                type="error",
                text=f"❌ Вывод #{req_id} провалился: {err}",
            )
        except Exception:
            pass
        return False, ""

    tx_hash = send_result.get("tx_hash", "")
    await storage.finalize_withdrawal_paid(req_id, tx_hash=tx_hash, by=approved_by or "auto")

    # accounting
    try:
        await storage.add_accounting_entry(
            category="balance_withdraw",
            amount_usdt=amount,
            note=f"Withdraw {req_id} → {user_key} · tx:{tx_hash[:16]}",
            created_by=approved_by or "auto:withdraw",
            ref_id=req_id,
        )
    except Exception:
        pass

    # notify owner
    try:
        await storage.add_notification(
            type="success",
            text=f"💸 Выплата по заявке #{req_id}: {amount:.2f}$ → {user_key}\nTX: {tx_hash[:24]}...",
        )
    except Exception:
        pass

    return True, tx_hash
