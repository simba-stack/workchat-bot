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


# Прайс банков в USDT (fallback если оператор не указал «ЦЕНА ЛК» в строке).
# Согласован с knowledge/pricing.md — обновляется ВРУЧНУЮ при изменении прайса.
PRICING_TABLE_USDT = {
    "альфа": 400, "альфа-банк": 400, "alpha": 400,
    "озон": 300, "ozon": 300,
    "райф": 350, "райффайзен": 350, "raif": 350,
    "точка": 300, "tochka": 300, "tinkoff": 300, "tochka-банк": 300,
    "уралсиб": 250, "uralsib": 250,
    "втб": 300, "vtb": 300,
    "локо": 0, "loko": 0, "бкс": 0, "bks": 0, "дело": 0, "убрир": 0, "ubrir": 0,
}


def lookup_pricing(bank: str) -> float:
    """Fallback цена ЛК для банка из встроенного прайса. 0 если банк неизвестен."""
    if not bank:
        return 0.0
    key = bank.lower().strip().replace("-банк", "").replace("-bank", "").strip()
    return float(PRICING_TABLE_USDT.get(key, 0.0))


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
        # Список заявок дня (формат «СТАРТ»):
        # каждая заявка — отдельный обмен с приёмом + выводом + расчётом маржи.
        "applications": [],
    }


def _f(x) -> float:
    """Безопасный float, None/«»/мусор → 0."""
    try:
        return float(x or 0)
    except (TypeError, ValueError):
        return 0.0


def compute_application(app: dict) -> dict:
    """Per-application расчёт. Возвращает USDT-числа.

    our_usdt = (buy_amount_rub / course) * (1 - our_rate_pct/100)
    client_usdt = (partner_amount_rub * (1 - client_pct/100)) / course
    lk_total_usdt = сумма ЦЕНА ЛК (intake + output)
    margin_usdt = our_usdt - client_usdt - lk_total_usdt
    """
    course = _f(app.get("course")) or 1.0
    our_rate_pct = _f(app.get("our_rate_pct"))
    client_pct = _f(app.get("client_pct"))
    partner_rub = _f(app.get("partner_amount_rub"))
    buy_rub = _f(app.get("buy_amount_rub")) or partner_rub

    our_usdt_gross = buy_rub / course if course else 0.0
    our_usdt = our_usdt_gross * (1 - our_rate_pct / 100.0)
    client_usdt = (partner_rub * (1 - client_pct / 100.0)) / course if course else 0.0

    intake = app.get("intake") or []
    output = app.get("output") or []
    # Приёмный ЛК (АЛЬФА) — рабочий счёт партнёра, не покупаем под каждую
    # заявку. В маржу не включаем, только выводные ЛК (за каждый платим).
    lk_intake = sum(_f(x.get("lk_cost_usdt")) for x in intake)
    lk_output = sum(_f(x.get("lk_cost_usdt")) for x in output)
    lk_total = lk_output

    margin = our_usdt - client_usdt - lk_total
    return {
        "course": course,
        "our_rate_pct": our_rate_pct,
        "client_pct": client_pct,
        "partner_amount_rub": partner_rub,
        "buy_amount_rub": buy_rub,
        "our_usdt_gross": our_usdt_gross,
        "our_usdt": our_usdt,
        "client_usdt": client_usdt,
        "lk_intake_usdt": lk_intake,
        "lk_output_usdt": lk_output,
        "lk_total_usdt": lk_total,
        "lk_count": len(intake) + len(output),
        "margin_usdt": margin,
    }


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

    # Сумма margin USDT по всем заявкам (формат СТАРТ)
    apps = record.get("applications") or []
    apps_margin_usdt = sum(compute_application(a)["margin_usdt"] for a in apps)
    apps_count = len(apps)

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
        "apps_margin_usdt": apps_margin_usdt,
        "apps_count": apps_count,
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

    # Заявки (формат СТАРТ)
    apps = (record or {}).get("applications") or []
    if apps:
        apps_margin = sum(compute_application(a)["margin_usdt"] for a in apps)
        body += "\n\n📋 <b>Заявки</b> (" + str(len(apps)) + "):\n"
        for a in apps:
            sa = compute_application(a)
            body += (
                f"  #{a.get('id', '?')} — заявка {_fmt_money(sa['partner_amount_rub'])}, "
                f"маржа {sa['margin_usdt']:.0f}$\n"
            )
        body += f"<b>Сумма маржи по заявкам: {apps_margin:.0f}$</b>"
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


def format_application_report(app: dict) -> str:
    """Форматирует одну заявку в HTML-сообщение для Telegram."""
    s = compute_application(app)
    lines = []
    title = f"✅ <b>Заявка #{app.get('id', '?')}</b>"
    lines.append(title)
    lines.append("")
    lines.append(
        f"📥 Сумма заявки: <b>{_fmt_money(s['partner_amount_rub'])}</b>"
        + (f" → откуп: {_fmt_money(s['buy_amount_rub'])}"
           if s["buy_amount_rub"] != s["partner_amount_rub"] else "")
    )
    lines.append(
        f"💱 Курс: <b>{s['course']:.2f} ₽/USDT</b>, "
        f"наша ставка: <b>{s['our_rate_pct']:.1f}%</b>, "
        f"клиент: <b>{s['client_pct']:.1f}%</b>"
    )
    lines.append("")

    intake = app.get("intake") or []
    if intake:
        lines.append("🏦 <b>ПРИЁМ:</b>")
        for it in intake:
            row = f"  • {it.get('bank', '—')} — {it.get('fio', '—')}"
            rem = _f(it.get("remainder_rub"))
            if rem:
                row += f" (остаток {_fmt_money(rem)})"
            lk = _f(it.get("lk_cost_usdt"))
            if lk:
                row += f" — ЛК {lk:.0f}$"
            lines.append(row)

    output = app.get("output") or []
    if output:
        lines.append("")
        lines.append("🏦 <b>Вывод:</b>")
        for o in output:
            row = f"  • {o.get('bank', '—')} {o.get('fio', '—')}"
            amt = _f(o.get("amount_rub"))
            if amt:
                row += f" — {_fmt_money(amt)}"
            note = o.get("note") or ""
            if note:
                row += f" ({note})"
            lk = _f(o.get("lk_cost_usdt"))
            if lk:
                row += f" — ЛК {lk:.0f}$"
            lines.append(row)

    lines.append("")
    lines.append(f"💸 Клиенту: <b>{s['client_usdt']:.0f}$</b>")
    lines.append(f"✅ Нам: <b>{s['our_usdt']:.0f}$</b>")
    lines.append(f"🛒 ЛК всего: <b>{s['lk_total_usdt']:.0f}$</b> ({s['lk_count']} шт)")
    lines.append("")
    margin_emoji = "📊" if s["margin_usdt"] >= 0 else "⚠️"
    lines.append(f"{margin_emoji} <b>Маржа: {s['margin_usdt']:.0f}$</b>")
    return "\n".join(lines)


# === Парсер формата «СТАРТ» (мульти-строка) ===

_RE_APP_HEADER = re.compile(
    r"^\s*(\d+)[.)]\s*заявка\s+([\d\s.,]+?)(?:\s*[-—]\s*откуп\s+([\d\s.,]+?))?\s*$",
    re.I | re.M,
)
_RE_INTAKE_LINE = re.compile(
    r"^\s*при[её]м\s*[:\-]\s*(.+?)$",
    re.I | re.M,
)
_RE_OUTPUT_HEADER = re.compile(r"^\s*вывод\s*[:\-]\s*$", re.I | re.M)
_RE_PAYOUT_OUR = re.compile(
    r"выплата\s+нам.*?курс\s*([\d.,]+).*?ставка\s*([\d.,]+)\s*%",
    re.I | re.S,
)
_RE_PAYOUT_CLIENT = re.compile(
    r"выплата\s+клиенту.*?\(\s*[\d\s.,]+\s*[-−]\s*([\d.,]+)\s*%",
    re.I | re.S,
)
_RE_LK_PRICE = re.compile(r"цена\s*лк\s*([\d.,]+)\s*\$?", re.I)
_RE_REMAINDER = re.compile(r"остаток\s+([\d][\d\s.,]*?)(?:\s*р|\s*к|\s*$|\)|\s+дебет)", re.I)
_RE_BLOCK = re.compile(r"блок\s+([\d.,]+)\s*к?", re.I)


def _parse_rub(s: str) -> float:
    """'1.420р' → 1420; '227к' → 227000; '785.000' → 785000.

    Дотчёт: «к» / «тыс» — множитель ×1000. Точка/запятая — разделители тысяч
    или десятичной части (точка между группами цифр считается тысячным
    разделителем; одиночная запятая в конце — десятичная).
    """
    if not s:
        return 0.0
    txt = str(s).lower().strip().replace(" ", "").replace("\u00a0", "")
    # Удалить рубли
    txt = txt.rstrip("р").rstrip("₽").rstrip("руб")
    multiplier = 1.0
    if txt.endswith("к") or txt.endswith("k") or txt.endswith("тыс"):
        multiplier = 1000.0
        txt = txt.rstrip("ктkk").rstrip("тыс")
    if txt.endswith("млн"):
        multiplier = 1_000_000.0
        txt = txt[:-3]
    # Заменим запятую на точку (десятичная), а потом убираем точки-разделители
    # тысяч — кроме последней.
    if "," in txt and "." not in txt:
        # 1,5 → 1.5 (десятичная)
        txt = txt.replace(",", ".")
    elif "." in txt and "," not in txt:
        # 785.000 — может быть и 785000 (тысячи) и 785.000 (десятичные)
        # Если после последней точки 3 цифры — это разделитель тысяч.
        parts = txt.split(".")
        if all(len(p) == 3 for p in parts[1:]):
            txt = "".join(parts)
    digits = "".join(ch for ch in txt if ch.isdigit() or ch == ".")
    if digits.count(".") > 1:
        # многоточек → берём как тысячный
        digits = digits.replace(".", "")
    try:
        return float(digits or 0) * multiplier
    except ValueError:
        return 0.0


def _parse_usdt(s: str) -> float:
    """'400$' → 400, '1.878$' → 1878 (точка как тысячный разделитель)."""
    if not s:
        return 0.0
    txt = str(s).lower().strip().replace(" ", "").replace("\u00a0", "")
    txt = txt.rstrip("$").rstrip("usdt").rstrip("usd")
    if "," in txt:
        txt = txt.replace(",", ".")
    if txt.count(".") == 1 and len(txt.split(".")[1]) == 3:
        # тысячный разделитель: 1.878 → 1878
        txt = txt.replace(".", "")
    try:
        return float(txt or 0)
    except ValueError:
        return 0.0


def parse_application(text: str) -> Optional[dict]:
    """Парсит мультистрочный формат «СТАРТ»-заявки.

    Минимум: «N. Заявка X» + «ПРИЕМ:» + «Выплата нам» с курсом и ставкой
    + «Выплата клиенту» с %. Откуп опционально. Вывод секция опционально.

    Возвращает dict приложения либо None если не похоже на заявку.
    """
    if not text:
        return None

    m_head = _RE_APP_HEADER.search(text)
    if not m_head:
        return None

    app_id = m_head.group(1)
    partner_rub = _parse_rub(m_head.group(2))
    buy_rub = _parse_rub(m_head.group(3) or "") or partner_rub

    # Курс и ставка (наша)
    course = 0.0
    our_rate_pct = 0.0
    m_pay_our = _RE_PAYOUT_OUR.search(text)
    if m_pay_our:
        course = _f(m_pay_our.group(1).replace(",", "."))
        our_rate_pct = _f(m_pay_our.group(2).replace(",", "."))

    # Клиент % (например "(785000-40%)/82" → 40)
    client_pct = 0.0
    m_pay_cl = _RE_PAYOUT_CLIENT.search(text)
    if m_pay_cl:
        client_pct = _f(m_pay_cl.group(1).replace(",", "."))

    # Парсим строки построчно
    intake: list = []
    output: list = []
    in_output_section = False

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        # Секция «Вывод:»
        if _RE_OUTPUT_HEADER.match(line):
            in_output_section = True
            continue

        # Заголовок «Заявка» — пропускаем
        if _RE_APP_HEADER.match(line):
            in_output_section = False
            continue

        # Расчётные итоги — пропускаем
        if any(k in line.lower() for k in (
            "выплата клиенту", "выплата нам", "рассчёт мар", "расчёт мар", "маржа",
            "курс ", "старт",
        )):
            in_output_section = False
            continue

        # ПРИЁМ
        m = _RE_INTAKE_LINE.match(line)
        if m:
            in_output_section = False
            body = m.group(1)
            # Парсим: БАНК - ФИО (остаток X) - ЦЕНА ЛК Y$
            parts = [p.strip() for p in re.split(r"\s*[-—]\s*", body)]
            if len(parts) >= 2:
                bank = parts[0]
                fio_part = parts[1]
                # Извлекаем (остаток X)
                rem_m = _RE_REMAINDER.search(fio_part)
                remainder = _parse_rub(rem_m.group(1)) if rem_m else 0.0
                if rem_m:
                    fio_part = fio_part[: rem_m.start()].strip("() ")
                fio = fio_part.strip()
                # ЦЕНА ЛК — может быть в любой из остальных частей
                lk_cost = 0.0
                for extra in parts[2:]:
                    lk_m = _RE_LK_PRICE.search(extra)
                    if lk_m:
                        lk_cost = _parse_usdt(lk_m.group(1))
                        break
                intake.append({
                    "bank": bank,
                    "fio": fio,
                    "remainder_rub": remainder,
                    "lk_cost_usdt": lk_cost,
                })
            continue

        # Строка вывода (только если внутри секции Вывод:)
        if in_output_section:
            # «ОЗОН Столярова - 227к - блок 1.5к - ЦЕНА ЛК 400$»
            parts = [p.strip() for p in re.split(r"\s*[-—]\s*", line)]
            if len(parts) < 2:
                continue
            bank_fio = parts[0]
            # Разделить bank fio: первое слово = банк, остальное = фио
            words = bank_fio.split(maxsplit=1)
            if len(words) >= 2:
                bank = words[0]
                fio = words[1]
            else:
                bank = bank_fio
                fio = ""
            amount_rub = _parse_rub(parts[1]) if len(parts) >= 2 else 0.0
            # Note и lk_cost из остальных частей
            notes = []
            lk_cost = 0.0
            for extra in parts[2:]:
                lk_m = _RE_LK_PRICE.search(extra)
                if lk_m:
                    lk_cost = _parse_usdt(lk_m.group(1))
                else:
                    notes.append(extra)
            output.append({
                "bank": bank,
                "fio": fio,
                "amount_rub": amount_rub,
                "note": ", ".join(notes),
                "lk_cost_usdt": lk_cost,
            })

    return {
        "id": app_id,
        "partner_amount_rub": partner_rub,
        "buy_amount_rub": buy_rub,
        "course": course,
        "our_rate_pct": our_rate_pct,
        "client_pct": client_pct,
        "intake": intake,
        "output": output,
        "raw_text": text,
    }


HELP_TEXT = (
    "📊 <b>Бухгалтерия — команды</b>\n\n"
    "<b>Заявка от операциониста</b> (формат СТАРТ — пиши в чат целиком):\n"
    "<code>1. Заявка 785000 - откуп 771610\n"
    "ПРИЕМ: АЛЬФА - Иванов Иван (остаток 1420р) - ЦЕНА ЛК 400$\n"
    "Вывод:\n"
    "ОЗОН Петров - 227к - блок 1.5к - ЦЕНА ЛК 400$\n"
    "ТОЧКА Сидоров - 229к - ЦЕНА ЛК 400$\n"
    "🟢 Выплата клиенту: (785000-40%)/82 = 5743$\n"
    "✅ Выплата нам (курс 82-ставка 2%) = 9221$</code>\n\n"
    "Бот сам вычислит маржу и сохранит заявку.\n\n"
    "<b>Просмотр:</b>\n"
    "• <code>отчёт</code> — за сегодня (все заявки + дневная сумма)\n"
    "• <code>отчёт 2026-05-04</code> — за конкретную дату\n\n"
    "<b>Ручные правки</b>:\n"
    "• <code>приход 5000 название</code>\n"
    "• <code>расход 500 название</code>\n"
    "• <code>удали правку 2</code>\n"
    "• <code>лк Альфа 400</code> — отдельный расход на ЛК\n"
    "• <code>курс 95 100</code> — общий курс USDT (для не-СТАРТ-сделок)\n"
)
