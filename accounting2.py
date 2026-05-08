"""Бухгалтерия V2: парсеры и расчёт по новой схеме.

Группа 1 «Личные кабинеты» — анкеты ЛК:
    1. Поставщик: @username
    2. Банк: ОЗОН
    3. Цена: 300$
    4. Метод оплаты: USDT TRC20 после отработки
       (или: Сделка в конте, № 12345)
    Статус: В РАБОТЕ
+ команды БРАК / БЛОК.

Группа 2 «Бухгалтерия» — заявки от операциониста:
    ЗАЯВКА 1
    ПРИЕМ: ОЗОН - Иванов - 1000000
    ВЫВЕДЕНО — 800000
    ВЫВОД СУММА:
    ОЗОН - Петров - 300000
    ТОЧКА - Сидоров - 500000
    Курс ВЫВОДА — 90
    Курс ВЫПЛАТЫ — 92
    ПРОЦЕНТ ВЫПЛАТЫ ПАРТНЕРУ: 40

Расчёт (без 2% ставки откупа):
    ВСЕГО ОТКУПИЛИ = сумма всех ВЫВОД-сумм (рубли)
    МЫ ПОЛУЧИЛИ = ВСЕГО ОТКУПИЛИ / Курс ВЫВОДА (USDT)
    ВЫПЛАТА КЛИЕНТУ = (ПРИЕМ × (100 − %) / 100) / Курс ВЫПЛАТЫ (USDT)
    ОПЛАТА ЗА ЛК = сумма Цена всех ВЫВОД-ЛК
        (если ЛК в БЛОК и block_usdt >= цена_ЛК → effective=0;
         если block_usdt < цена_ЛК → effective = цена_ЛК − block_usdt)
    МАРЖА = МЫ ПОЛУЧИЛИ − ВЫПЛАТА КЛИЕНТУ − ОПЛАТА ЗА ЛК
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta
from typing import Optional, Tuple
import re


# === Константы ===

LK_STATUSES = (
    "В_РАБОТЕ", "ОТРАБОТАН", "ПОПОЛНИТЬ_И_ОТПУСТИТЬ",
    "БРАК", "БЛОК", "ЗАВЕРШЁН",
)

PAYMENT_METHODS = (
    "USDT_TRC20",            # выплата на USDT TRC20 после отработки
    "GUARANTOR_BEFORE",      # сделка в конте ДО перевязки
    "GUARANTOR_AFTER",       # сделка в конте ПОСЛЕ перевязки
    "GUARANTOR_AFTER_WORK",  # сделка в конте ПОСЛЕ ОТРАБОТКИ
)

# Дефолтный прайс банков (USDT) — fallback если в анкете не указано.
PRICING_TABLE_USDT = {
    "альфа": 400, "альфа-банк": 400, "alpha": 400,
    "озон": 300, "ozon": 300,
    "райф": 350, "райффайзен": 350, "raif": 350,
    "точка": 300, "tochka": 300, "tinkoff": 300,
    "уралсиб": 250, "uralsib": 250,
    "втб": 300, "vtb": 300,
}


def lookup_pricing(bank: str) -> float:
    if not bank:
        return 0.0
    key = bank.lower().strip().replace("-банк", "").replace("-bank", "").strip()
    return float(PRICING_TABLE_USDT.get(key, 0.0))


def today_str() -> str:
    """Текущая дата YYYY-MM-DD в Москве."""
    msk = timezone(timedelta(hours=3))
    return datetime.now(msk).strftime("%Y-%m-%d")


def _f(x) -> float:
    try:
        return float(x or 0)
    except (TypeError, ValueError):
        return 0.0


def _strip_markdown(text: str) -> str:
    """Снимает **bold**/__italic__/~~strike~~/`code` маркеры Telegram."""
    return re.sub(r"\*\*|__|~~|`+", "", text or "")


# === Утилиты парсинга чисел ===

def parse_rub(s: str) -> float:
    """'1.420р' → 1420; '227к' → 227000; '785.000' → 785000; '1 000 000' → 1000000."""
    if not s:
        return 0.0
    txt = str(s).lower().strip().replace(" ", "").replace(" ", "")
    txt = txt.rstrip("р").rstrip("₽").rstrip("руб")
    multiplier = 1.0
    if txt.endswith("к") or txt.endswith("k") or txt.endswith("тыс"):
        multiplier = 1000.0
        txt = txt.rstrip("ктk").rstrip("тыс")
    if txt.endswith("млн"):
        multiplier = 1_000_000.0
        txt = txt[:-3]
    if "," in txt and "." not in txt:
        txt = txt.replace(",", ".")
    elif "." in txt and "," not in txt:
        parts = txt.split(".")
        if all(len(p) == 3 for p in parts[1:]):
            txt = "".join(parts)
    digits = "".join(ch for ch in txt if ch.isdigit() or ch == ".")
    if digits.count(".") > 1:
        digits = digits.replace(".", "")
    try:
        return float(digits or 0) * multiplier
    except ValueError:
        return 0.0


def parse_usdt(s: str) -> float:
    """'400$' → 400; '1.878$' → 1878 (точка как разделитель тысяч)."""
    if not s:
        return 0.0
    txt = str(s).lower().strip().replace(" ", "").replace(" ", "")
    txt = txt.rstrip("$").rstrip("usdt").rstrip("usd")
    if "," in txt:
        txt = txt.replace(",", ".")
    if txt.count(".") == 1 and len(txt.split(".")[1]) == 3:
        txt = txt.replace(".", "")
    try:
        return float(txt or 0)
    except ValueError:
        return 0.0


def parse_pct(s: str) -> float:
    """'40%' → 40; '40' → 40; '0.4' → 0.4 (тогда warning)."""
    if not s:
        return 0.0
    txt = str(s).strip().rstrip("%")
    if "," in txt:
        txt = txt.replace(",", ".")
    try:
        return float(txt)
    except ValueError:
        return 0.0


# === Парсер анкеты ЛК (Группа 1) ===

# Гибкие шаблоны: «1. Поставщик: @x», «Поставщик @x», «Supplier: @x»
_RE_LK_SUPPLIER = re.compile(
    r"^\s*(?:\d+[.)]\s*)?(?:поставщик|supplier|клиент)\s*[:\-]?\s*(@?\S+.*?)\s*$",
    re.I | re.M,
)
_RE_LK_BANK = re.compile(
    r"^\s*(?:\d+[.)]\s*)?(?:банк|bank)\s*[:\-]?\s*(.+?)\s*$",
    re.I | re.M,
)
_RE_LK_FIO = re.compile(
    r"^\s*(?:\d+[.)]\s*)?(?:ф\.?и\.?о\.?|fio|holder)\s*[:\-]?\s*(.+?)\s*$",
    re.I | re.M,
)
_RE_LK_PRICE = re.compile(
    r"^\s*(?:\d+[.)]\s*)?(?:цена|price)\s*[:\-]?\s*([\d.,]+)\s*\$?\s*$",
    re.I | re.M,
)
_RE_LK_METHOD = re.compile(
    r"^\s*(?:\d+[.)]\s*)?(?:метод(?:\s*оплаты)?|payment|способ\s*оплаты)\s*[:\-]?\s*(.+?)\s*$",
    re.I | re.M,
)
_RE_LK_DEAL = re.compile(
    r"^\s*(?:номер\s*сделки|сделка|deal|deal_id|№)\s*[:#\-]?\s*(\S+)\s*$",
    re.I | re.M,
)
_RE_LK_USDT = re.compile(
    r"^\s*(?:адрес|usdt|trc20|wallet)\s*[:\-]?\s*(\S+)\s*$",
    re.I | re.M,
)
_RE_LK_STATUS = re.compile(
    r"^\s*(?:статус|status)\s*[:\-]?\s*(.+?)\s*$",
    re.I | re.M,
)


def _normalize_method(raw: str) -> str:
    """Распознать метод оплаты из произвольного текста.
    Возвращает один из PAYMENT_METHODS либо «»."""
    if not raw:
        return ""
    t = raw.lower()
    is_guarantor = any(w in t for w in (
        "гарант", "конт", "сделк", "guarantor", "conte", "контик",
    ))
    is_usdt = any(w in t for w in ("usdt", "trc", "трц", "крипт"))
    if is_guarantor:
        if any(w in t for w in ("после отработ", "post_work", "отработк")):
            return "GUARANTOR_AFTER_WORK"
        if any(w in t for w in ("после перевяз", "потом", "после")):
            return "GUARANTOR_AFTER"
        if any(w in t for w in ("сейчас", "до", "вперёд", "вперед", "before")):
            return "GUARANTOR_BEFORE"
        # дефолт для гаранта без уточнения — после перевязки
        return "GUARANTOR_AFTER"
    if is_usdt:
        return "USDT_TRC20"
    return ""


def parse_lk_card(text: str) -> Optional[dict]:
    """Парсит анкету ЛК (мульти-строка). Минимум: банк + цена + метод.
    Возвращает dict с полями карточки либо None."""
    if not text:
        return None
    clean = _strip_markdown(text)

    out: dict = {}

    m = _RE_LK_SUPPLIER.search(clean)
    if m:
        out["supplier"] = m.group(1).strip().lstrip("@").split()[0]
    m = _RE_LK_BANK.search(clean)
    if m:
        out["bank"] = m.group(1).strip()
    m = _RE_LK_FIO.search(clean)
    if m:
        out["fio"] = m.group(1).strip()
    m = _RE_LK_PRICE.search(clean)
    if m:
        out["price_usdt"] = parse_usdt(m.group(1))
    m = _RE_LK_METHOD.search(clean)
    if m:
        out["payment_method"] = _normalize_method(m.group(1))
    m = _RE_LK_DEAL.search(clean)
    if m:
        out["deal_id"] = m.group(1).lstrip("#").strip()
    m = _RE_LK_USDT.search(clean)
    if m:
        out["usdt_address"] = m.group(1).strip()
    m = _RE_LK_STATUS.search(clean)
    if m:
        out["status"] = m.group(1).strip().upper().replace(" ", "_")

    # Минимальная валидация
    if not (out.get("bank") and (out.get("price_usdt") or out.get("payment_method"))):
        return None
    if not out.get("payment_method"):
        # Метод по умолчанию — USDT_TRC20
        out["payment_method"] = "USDT_TRC20"
    if not out.get("price_usdt"):
        out["price_usdt"] = lookup_pricing(out.get("bank", ""))
    return out


# === Парсер команд в Группе 1 ===

# «БРАК <банк> <фио> <причина>» / «брак озон иванов причина»
_RE_BRAK = re.compile(r"^\s*брак\b\s*(.*)$", re.I | re.S)
# «БЛОК <банк> <фио> <сумма> <примечание>»
_RE_BLOK = re.compile(r"^\s*блок\b\s*(.*)$", re.I | re.S)


def parse_brak_command(text: str) -> Optional[dict]:
    """«БРАК ОЗОН Иванов причина» → {bank, fio, reason}."""
    if not text:
        return None
    clean = _strip_markdown(text).strip()
    m = _RE_BRAK.match(clean)
    if not m:
        return None
    rest = m.group(1).strip()
    if not rest:
        return None
    parts = re.split(r"\s+", rest, maxsplit=2)
    bank = parts[0] if parts else ""
    fio = parts[1] if len(parts) >= 2 else ""
    reason = parts[2] if len(parts) >= 3 else ""
    return {"cmd": "БРАК", "bank": bank, "fio": fio, "reason": reason}


def parse_blok_command(text: str) -> Optional[dict]:
    """«БЛОК ОЗОН Иванов 50000 примечание» → {bank, fio, amount_rub, note}."""
    if not text:
        return None
    clean = _strip_markdown(text).strip()
    m = _RE_BLOK.match(clean)
    if not m:
        return None
    rest = m.group(1).strip()
    if not rest:
        return None
    # split: первое слово = банк, второе слово = фио,
    # затем следует сумма (rub) и опционально примечание.
    tokens = rest.split()
    bank = tokens[0] if tokens else ""
    fio = tokens[1] if len(tokens) >= 2 else ""
    amount = 0.0
    note = ""
    if len(tokens) >= 3:
        amount = parse_rub(tokens[2])
        note = " ".join(tokens[3:]) if len(tokens) > 3 else ""
    return {
        "cmd": "БЛОК", "bank": bank, "fio": fio,
        "amount_rub": amount, "note": note,
    }


# === Парсер заявки v2 (Группа 2) ===

_RE_APP_HEADER = re.compile(
    r"^\s*заявка\s+(\d+)(?:\s*\[?\s*([\d.\-]+)\s*\]?)?\s*$",
    re.I | re.M,
)
_RE_INTAKE_LINE = re.compile(
    r"^\s*([^\-—]+?)\s*[\-—]\s*([^\-—]+?)\s*[\-—]\s*([\d\s.,]+(?:к|тыс|млн)?)\s*$",
    re.I | re.M,
)
_RE_WITHDRAWN = re.compile(
    r"^\s*выведено\s*[:—\-]\s*([\d\s.,]+(?:к|тыс|млн)?)\s*$",
    re.I | re.M,
)
_RE_COURSE_WITHDRAW = re.compile(
    r"^\s*курс\s+вывод(?:а)?\s*[:—\-]\s*([\d.,]+)\s*$",
    re.I | re.M,
)
_RE_COURSE_PAYOUT = re.compile(
    r"^\s*курс\s+выплат(?:ы)?\s*[:—\-]\s*([\d.,]+)\s*$",
    re.I | re.M,
)
_RE_PARTNER_PCT = re.compile(
    r"^\s*процент\s+выплаты\s+партн[её]ру\s*[:—\-]\s*([\d.,]+)\s*%?\s*$",
    re.I | re.M,
)


def parse_application_v2(text: str) -> Optional[dict]:
    """Парсит заявку нового формата.

    Структура: ЗАЯВКА N → ПРИЕМ: bank-fio-сумма / ВЫВЕДЕНО: N → ВЫВОД СУММА:
    список bank-fio-сумма → курсы → процент.
    """
    if not text:
        return None
    clean = _strip_markdown(text)

    m = _RE_APP_HEADER.search(clean)
    if not m:
        return None
    app_id_seq = int(m.group(1))
    app_date = (m.group(2) or "").strip()

    # Курсы и проценты
    course_w = parse_pct(_RE_COURSE_WITHDRAW.search(clean).group(1)) \
        if _RE_COURSE_WITHDRAW.search(clean) else 0.0
    course_p = parse_pct(_RE_COURSE_PAYOUT.search(clean).group(1)) \
        if _RE_COURSE_PAYOUT.search(clean) else 0.0
    partner_pct = parse_pct(_RE_PARTNER_PCT.search(clean).group(1)) \
        if _RE_PARTNER_PCT.search(clean) else 0.0
    withdrawn_rub = parse_rub(_RE_WITHDRAWN.search(clean).group(1)) \
        if _RE_WITHDRAWN.search(clean) else 0.0

    # Парсим строки построчно — отделяем ПРИЕМ от ВЫВОДОВ.
    intake = None
    outputs: list = []
    section = None  # "intake" / "output" / None

    for raw in clean.splitlines():
        line = raw.strip()
        if not line:
            continue
        low = line.lower()
        if low.startswith("приём") or low.startswith("прием"):
            section = "intake"
            continue
        if low.startswith("вывод") and ":" in low:
            # «вывод сумма:» / «вывод:»
            section = "output"
            continue
        # Игнорируем итоговые строки
        if any(low.startswith(k) for k in (
            "выведено", "курс ", "процент", "всего откуп",
            "мы получ", "выплата", "марж", "наша", "оплата",
            "заявка", "ст ", "ст:",
        )):
            continue

        if section in ("intake", "output"):
            m_line = _RE_INTAKE_LINE.match(line)
            if m_line:
                row = {
                    "bank": m_line.group(1).strip(),
                    "fio": m_line.group(2).strip(),
                    "amount_rub": parse_rub(m_line.group(3)),
                }
                if section == "intake":
                    intake = row
                    section = None  # после первой строки приёма ждём «ВЫВОД»
                else:
                    outputs.append(row)

    if intake is None or not outputs:
        return None

    intake["withdrawn_rub"] = withdrawn_rub

    return {
        "id_seq": app_id_seq,
        "date": app_date,  # пусто = today
        "intake": intake,
        "outputs": outputs,
        "course_withdrawal": course_w,
        "course_payout": course_p,
        "partner_pct": partner_pct,
        "raw_text": text,
    }


# === Расчёт по заявке (с учётом БЛОК) ===

def compute_application_v2(app: dict, lk_cards: dict) -> dict:
    """Считает заявку. lk_cards — все карточки storage.lk_cards.

    Возвращает dict с числами + lk_breakdown (по каждому output ЛК).
    """
    intake = app.get("intake") or {}
    outputs = app.get("outputs") or []
    course_w = _f(app.get("course_withdrawal")) or 1.0
    course_p = _f(app.get("course_payout")) or 1.0
    partner_pct = _f(app.get("partner_pct"))

    intake_rub = _f(intake.get("amount_rub"))
    withdrawn_rub = _f(intake.get("withdrawn_rub"))

    # Всего откуплено = сумма ВЫВОД-сумм. Если оператор указал ВЫВЕДЕНО —
    # используем его (он точнее знает сколько вышло).
    total_withdrawn = sum(_f(o.get("amount_rub")) for o in outputs)
    if not total_withdrawn:
        total_withdrawn = withdrawn_rub

    # Мы получили (USDT)
    we_got_usdt = (total_withdrawn / course_w) if course_w else 0.0

    # Выплата клиенту
    client_part_rub = intake_rub * (1 - partner_pct / 100.0)
    client_payout_usdt = (client_part_rub / course_p) if course_p else 0.0

    # Оплата за ЛК с учётом БЛОК.
    # Учитываем И приёмный ЛК (intake — наш закупленный счёт куда зашли деньги),
    # И выводные ЛК (outputs — куда откупились). У всех своя цена в анкетах.
    lk_breakdown = []
    lk_costs_total = 0.0
    intake_for_calc = []
    if intake.get("bank") and intake.get("fio"):
        intake_for_calc.append({**intake, "_role": "intake"})
    for o_idx, o in enumerate([*intake_for_calc, *outputs]):
        role = o.get("_role") or "output"
        bank = (o.get("bank") or "").strip()
        fio = (o.get("fio") or "").strip()
        # Найти карточку
        card = None
        for c in (lk_cards or {}).values():
            if (c.get("bank", "").lower() == bank.lower()
                    and fio.lower() in (c.get("fio") or "").lower()):
                card = c
                break
        if card is None:
            # Карточки нет — fallback по прайсу, считаем как обычно
            price = lookup_pricing(bank)
            lk_costs_total += price
            lk_breakdown.append({
                "role": role,
                "bank": bank, "fio": fio,
                "price_usdt": price,
                "effective_usdt": price,
                "card_status": "—",
                "note": "анкета не найдена" if price == 0 else "",
            })
            continue
        price = _f(card.get("price_usdt")) or lookup_pricing(bank)
        status = card.get("status", "")
        if status == "БЛОК":
            block_rub = _f(card.get("block_amount_rub"))
            block_usdt = (block_rub / course_p) if course_p else 0.0
            if block_usdt >= price:
                effective = 0.0
                note = (
                    f"не учтён (блок {block_usdt:.0f}$ ≥ цена {price:.0f}$)"
                )
            else:
                effective = price - block_usdt
                note = (
                    f"учтён частично ({effective:.0f}$, блок {block_usdt:.0f}$)"
                )
            lk_costs_total += effective
            lk_breakdown.append({
                "role": role,
                "bank": bank, "fio": fio,
                "price_usdt": price,
                "effective_usdt": effective,
                "card_status": status,
                "block_amount_rub": block_rub,
                "note": note,
            })
        else:
            lk_costs_total += price
            lk_breakdown.append({
                "role": role,
                "bank": bank, "fio": fio,
                "price_usdt": price,
                "effective_usdt": price,
                "card_status": status,
                "note": "",
            })

    margin_usdt = we_got_usdt - client_payout_usdt - lk_costs_total

    return {
        "intake_rub": intake_rub,
        "withdrawn_rub": withdrawn_rub,
        "total_withdrawn_rub": total_withdrawn,
        "course_withdrawal": course_w,
        "course_payout": course_p,
        "partner_pct": partner_pct,
        "we_got_usdt": we_got_usdt,
        "client_payout_usdt": client_payout_usdt,
        "client_part_rub": client_part_rub,
        "lk_costs_usdt": lk_costs_total,
        "lk_breakdown": lk_breakdown,
        "margin_usdt": margin_usdt,
    }


# === Форматтеры ===

def _fmt_rub(v) -> str:
    sign = "−" if v < 0 else ""
    return f"{sign}{abs(v):,.0f}".replace(",", " ") + " ₽"


def _fmt_usdt(v) -> str:
    sign = "−" if v < 0 else ""
    return f"{sign}{abs(v):,.0f}".replace(",", " ") + " $"


METHOD_LABELS = {
    "USDT_TRC20": "USDT TRC20 после отработки",
    "GUARANTOR_BEFORE": "Сделка в конте (ДО перевязки)",
    "GUARANTOR_AFTER": "Сделка в конте (после перевязки)",
    "GUARANTOR_AFTER_WORK": "Сделка в конте (после отработки)",
}

STATUS_LABELS = {
    "В_РАБОТЕ": "🟢 В РАБОТЕ",
    "ОТРАБОТАН": "✅ ОТРАБОТАН",
    "ПОПОЛНИТЬ_И_ОТПУСТИТЬ": "💎 ПОПОЛНИТЬ И ОТПУСТИТЬ",
    "БРАК": "❌ БРАК",
    "БЛОК": "🚫 БЛОК",
    "ЗАВЕРШЁН": "🏁 ЗАВЕРШЁН",
}


def format_lk_card(card: dict) -> str:
    """Шаблон анкеты ЛК для Группы 1 (Telegram HTML)."""
    cid = card.get("card_id", "?")
    supplier = (card.get("supplier") or "").lstrip("@") or "—"
    bank = card.get("bank") or "—"
    fio = card.get("fio") or "—"
    price = _f(card.get("price_usdt"))
    method = card.get("payment_method") or ""
    method_label = METHOD_LABELS.get(method, method or "—")
    deal_id = card.get("deal_id") or ""
    usdt_addr = card.get("usdt_address") or ""
    status = card.get("status") or "В_РАБОТЕ"
    status_label = STATUS_LABELS.get(status, status)

    lines = [
        f"🆔 <b>ЛК #{cid}</b>",
        f"1. Поставщик: @{supplier}",
        f"2. Банк: <b>{bank}</b>",
        f"3. ФИО: {fio}",
        f"4. Цена: <b>{price:.0f}$</b>",
        f"5. Метод оплаты: {method_label}",
    ]
    if deal_id:
        lines.append(f"   Номер сделки: #{deal_id}")
    if usdt_addr and method == "USDT_TRC20":
        lines.append(f"   USDT TRC20: <code>{usdt_addr}</code>")
    lines.append(f"🔄 Статус: {status_label}")

    if status == "БЛОК":
        bamt = _f(card.get("block_amount_rub"))
        bnote = card.get("block_note") or ""
        lines.append(f"   Сумма блока: {_fmt_rub(bamt)}")
        if bnote:
            lines.append(f"   Что нужно: {bnote}")
    if status == "БРАК":
        lines.append(f"   Причина: {card.get('brak_reason') or '—'}")

    return "\n".join(lines)


def format_application_report_v2(app: dict, computed: dict) -> str:
    """Отчёт по заявке для Группы 2."""
    intake = app.get("intake") or {}
    outputs = app.get("outputs") or []
    app_id = app.get("id") or app.get("id_seq") or "?"
    date = app.get("date") or today_str()

    lines = [
        f"📊 <b>Заявка #{app_id}</b> — {date}",
        "",
        f"📥 <b>ПРИЁМ:</b> {intake.get('bank', '—')} — "
        f"{intake.get('fio', '—')} — <b>{_fmt_rub(intake.get('amount_rub', 0))}</b>",
        f"   ВЫВЕДЕНО: <b>{_fmt_rub(intake.get('withdrawn_rub', 0))}</b>",
        "",
        f"📤 <b>ВЫВОД</b> ({len(outputs)}):",
    ]
    for o in outputs:
        lines.append(
            f"  • {o.get('bank', '—')} — {o.get('fio', '—')} — "
            f"{_fmt_rub(o.get('amount_rub', 0))}"
        )

    lines += [
        "",
        f"💱 Курс ВЫВОДА: <b>{computed['course_withdrawal']:.2f} ₽/USDT</b>",
        f"💱 Курс ВЫПЛАТЫ: <b>{computed['course_payout']:.2f} ₽/USDT</b>",
        f"📊 Процент партнёру: <b>{computed['partner_pct']:.1f}%</b>",
        "",
        "━━━━━━━━━━━━━━",
        f"💰 ВСЕГО ОТКУПИЛИ: <b>{_fmt_rub(computed['total_withdrawn_rub'])}</b>",
        f"✅ МЫ ПОЛУЧИЛИ: <b>{_fmt_usdt(computed['we_got_usdt'])}</b>",
        f"💸 ВЫПЛАТА КЛИЕНТУ: <b>{_fmt_usdt(computed['client_payout_usdt'])}</b>",
        f"🛒 ОПЛАТА ЗА ЛК: <b>{_fmt_usdt(computed['lk_costs_usdt'])}</b>",
        "",
    ]

    margin = computed["margin_usdt"]
    margin_emoji = "📊" if margin >= 0 else "⚠️"
    lines.append(f"{margin_emoji} <b>МАРЖА: {_fmt_usdt(margin)}</b>")

    # Детализация по ЛК (если есть БЛОК или missing)
    notes_lines = []
    for b in computed.get("lk_breakdown", []):
        if b.get("note"):
            notes_lines.append(
                f"  • {b['bank']} {b['fio']}: {b['note']}"
            )
    if notes_lines:
        lines.append("")
        lines.append("⚠️ <b>Примечания:</b>")
        lines.extend(notes_lines)

    return "\n".join(lines)
