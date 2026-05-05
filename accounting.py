"""Бухгалтерия: расчёт чистой маржи и форматирование ежедневного отчёта.

Pure-функции, состояние не держит — всё через storage. Структура одной
дневной записи (см. storage._default_state -> accounting):

    {
      "turnovers": [{deal_id, amount_rub, label}, ...],
      "partner_payouts": [{deal_id, amount_usdt, client}, ...],
      "lk_costs": [{bank, amount_usdt, label}, ...],
      "courses": {"usdt_buy_rub": float, "usdt_sell_rub": float},
      "manual": [{label, amount_rub, ts}, ...],
    }

Расчёт:
  pre_salary = turnover_rub
             - partner_payout_in_rub_at_sell_rate (что отдали партнёру)
             - lk_cost_in_rub_at_buy_rate (что потратили на покупку ЛК)
             + manual_total (ручные приходы/расходы)

  ЧМ * (1 + 0.15 + 0.02) = pre_salary
  ЧМ = pre_salary / 1.17

  operator_salary = ЧМ * 0.15
  exchanger_salary = ЧМ * 0.02
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta
from typing import Optional


# Зарплатные ставки (от чистой маржи)
OPERATOR_RATE = 0.15   # операционист
EXCHANGER_RATE = 0.02  # обменник/откупщик


def today_str() -> str:
    """Текущая дата в формате YYYY-MM-DD (UTC+3, московское время)."""
    msk = timezone(timedelta(hours=3))
    return datetime.now(msk).strftime("%Y-%m-%d")


def empty_day_record() -> dict:
    """Пустая структура дневной записи."""
    return {
        "turnovers": [],
        "partner_payouts": [],
        "lk_costs": [],
        "courses": {"usdt_buy_rub": 0.0, "usdt_sell_rub": 0.0},
        "manual": [],
    }


def _f(x) -> float:
    """Безопасный float, None/«»/мусор → 0."""
    try:
        return float(x or 0)
    except (TypeError, ValueError):
        return 0.0


def compute_day_summary(record: dict) -> dict:
    """Считает все агрегаты за день. record может быть None/пустой."""
    if not record:
        record = empty_day_record()

    courses = record.get("courses") or {}
    rate_buy = _f(courses.get("usdt_buy_rub"))
    rate_sell = _f(courses.get("usdt_sell_rub"))

    # 1. Прошло по счетам клиентов (в рублях)
    turnover_rub = sum(_f(t.get("amount_rub")) for t in (record.get("turnovers") or []))

    # 2. Выплаты партнёрам (в USDT и в рублях по sell-курсу — по нему мы платим)
    partner_payout_usdt = sum(
        _f(p.get("amount_usdt")) for p in (record.get("partner_payouts") or [])
    )
    partner_payout_rub = partner_payout_usdt * rate_sell

    # Курсовая дельта (если мы покупаем USDT по rate_buy, а отдаём партнёру
    # по rate_sell, разница — наш доход от обменной операции).
    # На самом деле если rate_sell > rate_buy → разница положительная для нас,
    # но мы её НЕ ПРИБАВЛЯЕМ к pre_salary (turnover уже в рублях, partner —
    # уже в рублях по sell). Дельта показывается отдельно для прозрачности.
    course_delta_rub = (rate_sell - rate_buy) * partner_payout_usdt

    # 3. Стоимость ЛК (мы покупаем по rate_buy)
    lk_cost_usdt = sum(_f(l.get("amount_usdt")) for l in (record.get("lk_costs") or []))
    lk_cost_rub = lk_cost_usdt * rate_buy

    # 4. Ручные правки (плюс — приход, минус — расход)
    manual_total = sum(_f(m.get("amount_rub")) for m in (record.get("manual") or []))

    # Pre-salary: то что осталось до вычета зарплат
    pre_salary = turnover_rub - partner_payout_rub - lk_cost_rub + manual_total

    salary_rate = OPERATOR_RATE + EXCHANGER_RATE
    if pre_salary > 0:
        net_margin = pre_salary / (1.0 + salary_rate)
    else:
        net_margin = pre_salary  # минусуем если в минус — без зарплат
    operator_salary = max(0.0, net_margin * OPERATOR_RATE)
    exchanger_salary = max(0.0, net_margin * EXCHANGER_RATE)

    return {
        "turnover_rub": turnover_rub,
        "partner_payout_usdt": partner_payout_usdt,
        "partner_payout_rub": partner_payout_rub,
        "lk_cost_usdt": lk_cost_usdt,
        "lk_cost_rub": lk_cost_rub,
        "manual_total": manual_total,
        "rate_buy": rate_buy,
        "rate_sell": rate_sell,
        "course_delta_rub": course_delta_rub,
        "pre_salary": pre_salary,
        "operator_salary": operator_salary,
        "exchanger_salary": exchanger_salary,
        "net_margin": net_margin,
        # счётчики
        "deals_count": len(record.get("turnovers") or []),
        "lk_count": len(record.get("lk_costs") or []),
        "manual_count": len(record.get("manual") or []),
    }


def _fmt_money(value: float, currency: str = "₽") -> str:
    """50 000 ₽, 400 USDT."""
    sign = "−" if value < 0 else ""
    return f"{sign}{abs(value):,.0f}".replace(",", " ") + f" {currency}"


def format_day_report(date_str: str, record: Optional[dict]) -> str:
    """Многострочный отчёт за день для отправки в чат «Бухгалтерия»."""
    s = compute_day_summary(record or {})

    rate_part = ""
    if s["rate_buy"] or s["rate_sell"]:
        rate_part = (
            f"\n💱 <b>Курс USDT</b>: закуп {s['rate_buy']:.2f} ₽ / "
            f"партнёру {s['rate_sell']:.2f} ₽"
        )
        if s["rate_sell"] and s["rate_buy"] and s["course_delta_rub"]:
            rate_part += f" (дельта на USDT партнёрам: {_fmt_money(s['course_delta_rub'])})"

    deals_block = ""
    if record and record.get("turnovers"):
        rows = []
        for t in record["turnovers"]:
            did = t.get("deal_id") or ""
            did_part = f"#{did} — " if did else ""
            label = t.get("label") or ""
            rows.append(f"  • {did_part}{_fmt_money(_f(t.get('amount_rub')))} {label}".rstrip())
        deals_block = "\n📥 <b>По счетам клиентов</b>:\n" + "\n".join(rows)

    payouts_block = ""
    if record and record.get("partner_payouts"):
        rows = []
        for p in record["partner_payouts"]:
            did = p.get("deal_id") or ""
            did_part = f"#{did} " if did else ""
            client = p.get("client") or ""
            rows.append(
                f"  • {did_part}{client} — {_fmt_money(_f(p.get('amount_usdt')), 'USDT')}".rstrip()
            )
        payouts_block = "\n💸 <b>Выплачено партнёрам</b>:\n" + "\n".join(rows)

    lk_block = ""
    if record and record.get("lk_costs"):
        rows = []
        for lk in record["lk_costs"]:
            bank = lk.get("bank") or "—"
            label = lk.get("label") or ""
            tail = f" {label}" if label else ""
            rows.append(f"  • {bank} — {_fmt_money(_f(lk.get('amount_usdt')), 'USDT')}{tail}")
        lk_block = "\n🛒 <b>Куплено ЛК</b>:\n" + "\n".join(rows)

    manual_block = ""
    if record and record.get("manual"):
        rows = []
        for i, m in enumerate(record["manual"], 1):
            label = m.get("label") or "—"
            rows.append(f"  [{i}] {label}: {_fmt_money(_f(m.get('amount_rub')))}")
        manual_block = "\n📝 <b>Правки</b>:\n" + "\n".join(rows)

    sections = [
        f"📊 <b>Бухгалтерия за {date_str}</b>",
        "",
        f"💰 Прошло по счетам: <b>{_fmt_money(s['turnover_rub'])}</b>",
        f"💸 Партнёрам: <b>{s['partner_payout_usdt']:.2f} USDT</b> "
        f"(≈ {_fmt_money(s['partner_payout_rub'])})",
        f"🛒 ЛК: <b>{s['lk_cost_usdt']:.2f} USDT</b> "
        f"(≈ {_fmt_money(s['lk_cost_rub'])})",
        f"📝 Ручные правки: {_fmt_money(s['manual_total'])}",
        rate_part.lstrip("\n") if rate_part else "",
        "",
        "━━━━━━━━━━━━━━",
        f"💼 До зарплат: <b>{_fmt_money(s['pre_salary'])}</b>",
        f"🧾 Зарплата операциониста (15%): {_fmt_money(s['operator_salary'])}",
        f"🧾 Зарплата откупщика (2%): {_fmt_money(s['exchanger_salary'])}",
        f"✅ <b>Чистая маржа</b>: <b>{_fmt_money(s['net_margin'])}</b>",
        "",
        f"<i>Сделок: {s['deals_count']}, ЛК: {s['lk_count']}, "
        f"правок: {s['manual_count']}</i>",
    ]
    body = "\n".join(x for x in sections if x is not None)
    body += deals_block + payouts_block + lk_block + manual_block
    return body


# === Парсер команд для accounting_group ===

import re

_RE_REPORT = re.compile(r"^/?(отч[её]т|report)(?:\s+(\S+))?\s*$", re.I)
_RE_RATE = re.compile(
    r"^/?курс(?:\s+usdt)?\s+([\d.,]+)\s*[/\\\s]\s*([\d.,]+)\s*$", re.I
)
_RE_INCOME = re.compile(r"^/?(приход|income)\s+([\d.,]+)\s+(.+)$", re.I)
_RE_EXPENSE = re.compile(r"^/?(расход|expense)\s+([\d.,]+)\s+(.+)$", re.I)
_RE_LK = re.compile(r"^/?лк\s+(\S+)\s+([\d.,]+)\s*(.*)$", re.I)
_RE_TURNOVER = re.compile(
    r"^/?(сумма|turnover)\s+([\d.,]+)(?:\s+(?:#?(\S+)))?\s*(.*)$", re.I
)
_RE_PARTNER = re.compile(
    r"^/?(партнёр|партнер|partner)\s+(\S+)\s+([\d.,]+)(?:\s+#?(\S+))?\s*$",
    re.I,
)
_RE_REMOVE = re.compile(r"^/?(удали|remove|del)\s+правк[ау]?\s+(\d+)\s*$", re.I)


def _to_float(s: str) -> float:
    return float((s or "0").replace(" ", "").replace(",", "."))


def parse_command(text: str) -> Optional[dict]:
    """Пытается распознать команду в свободном тексте. Возвращает dict либо None.

    Команды:
      «отчёт» / «отчёт 2026-05-04» — показать отчёт
      «курс 95 100» — установить курс USDT (закуп 95 / партнёру 100)
      «приход 5000 название» — ручной приход (+5000)
      «расход 500 название» — ручной расход (−500)
      «лк Альфа 400» / «лк Альфа 400 для Иванова» — стоимость купленного ЛК
      «сумма 50000 #95941» / «сумма 50000» — оборот по сделке
      «партнёр @user 400 #95941» — выплата партнёру
      «удали правку 2» — удалить ручную правку с индексом
    """
    if not text:
        return None
    t = text.strip()

    m = _RE_REPORT.match(t)
    if m:
        return {"cmd": "report", "date": m.group(2)}

    m = _RE_RATE.match(t)
    if m:
        return {
            "cmd": "courses",
            "buy": _to_float(m.group(1)),
            "sell": _to_float(m.group(2)),
        }

    m = _RE_INCOME.match(t)
    if m:
        return {
            "cmd": "manual",
            "amount_rub": abs(_to_float(m.group(2))),
            "label": m.group(3).strip(),
        }

    m = _RE_EXPENSE.match(t)
    if m:
        return {
            "cmd": "manual",
            "amount_rub": -abs(_to_float(m.group(2))),
            "label": m.group(3).strip(),
        }

    m = _RE_LK.match(t)
    if m:
        return {
            "cmd": "lk",
            "bank": m.group(1).strip(),
            "amount_usdt": _to_float(m.group(2)),
            "label": (m.group(3) or "").strip(),
        }

    m = _RE_PARTNER.match(t)
    if m:
        client = m.group(2).strip()
        if not client.startswith("@"):
            client = "@" + client.lstrip("@")
        return {
            "cmd": "partner",
            "client": client,
            "amount_usdt": _to_float(m.group(3)),
            "deal_id": (m.group(4) or "").lstrip("#").strip(),
        }

    m = _RE_TURNOVER.match(t)
    if m:
        return {
            "cmd": "turnover",
            "amount_rub": _to_float(m.group(2)),
            "deal_id": (m.group(3) or "").lstrip("#").strip(),
            "label": (m.group(4) or "").strip(),
        }

    m = _RE_REMOVE.match(t)
    if m:
        return {"cmd": "remove_manual", "index": int(m.group(2)) - 1}

    return None


HELP_TEXT = (
    "📊 <b>Бухгалтерия — команды</b>\n\n"
    "<b>Просмотр:</b>\n"
    "• <code>отчёт</code> — за сегодня\n"
    "• <code>отчёт 2026-05-04</code> — за конкретную дату\n\n"
    "<b>Ввод данных:</b>\n"
    "• <code>курс 95 100</code> — закупочный/партнёрский курс USDT\n"
    "• <code>сумма 50000 #95941 описание</code> — оборот по сделке\n"
    "• <code>партнёр @client 400 #95941</code> — выплата партнёру в USDT\n"
    "• <code>лк Альфа 400</code> — стоимость купленного ЛК в USDT\n\n"
    "<b>Ручные правки</b> (приходы/расходы):\n"
    "• <code>приход 5000 название</code>\n"
    "• <code>расход 500 название</code>\n"
    "• <code>удали правку 2</code> — удалить ручную правку #N\n\n"
    "<i>Сделки и партнёрские выплаты записываются автоматически когда сделка</i>\n"
    "<i>переходит в статус ЗАВЕРШЕНА. Курс задаётся раз в день.</i>"
)
