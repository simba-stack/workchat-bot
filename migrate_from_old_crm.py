#!/usr/bin/env python3
"""Миграция данных из старой Node.js CRM (MySQL dump) в наш storage.json.

ИСПОЛЬЗОВАНИЕ:
  # Сначала dry-run:
  python migrate_from_old_crm.py /path/to/crm.sql --dry-run

  # Если всё ок:
  python migrate_from_old_crm.py /path/to/crm.sql --apply

  # Бэкап state.json создаётся автоматически перед apply.

ПОВЕДЕНИЕ:
  - НЕ затирает существующих owner'ов и дропов в state.json
  - Дедуп owner: по tg_user_id (если совпадает — пропускаем, не дублируем)
  - Дедуп drop: по (owner_id, fio) — если уже есть, пропускаем
  - Чаты добавляются если ID не зарегистрирован

ЛОГ:
  Каждая операция печатается в stdout. В конце — сводный отчёт.
"""
import re
import json
import os
import sys
import time
import shutil
import argparse
import asyncio
from datetime import datetime
from pathlib import Path


# ════════════════════════════════════════════════════════════════
# Парсер MySQL дампа
# ════════════════════════════════════════════════════════════════

def _parse_value(s: str):
    """Распарсить одно SQL-значение из VALUES (...): NULL, число, строка."""
    s = s.strip()
    if s.upper() == "NULL":
        return None
    if s.startswith("'") and s.endswith("'"):
        # Убираем кавычки и unescape SQL-escapes
        inner = s[1:-1]
        inner = inner.replace("\\'", "'").replace("\\\\", "\\").replace("\\n", "\n").replace("\\r", "\r").replace("\\t", "\t").replace('\\"', '"')
        return inner
    # Число
    try:
        if "." in s:
            return float(s)
        return int(s)
    except ValueError:
        return s


def _split_values(values_str: str):
    """Разбить строку '(...), (...), ...' на список tuple'ов VALUES."""
    rows = []
    cur = []
    i = 0
    depth = 0
    buf = ""
    in_str = False
    n = len(values_str)
    while i < n:
        ch = values_str[i]
        if in_str:
            if ch == "\\" and i + 1 < n:
                buf += ch + values_str[i + 1]
                i += 2
                continue
            if ch == "'":
                in_str = False
            buf += ch
            i += 1
            continue
        if ch == "'":
            in_str = True
            buf += ch
        elif ch == "(":
            depth += 1
            if depth == 1:
                buf = ""
            else:
                buf += ch
        elif ch == ")":
            depth -= 1
            if depth == 0:
                # Конец одного row — парсим
                row = _parse_row(buf)
                rows.append(row)
                buf = ""
            else:
                buf += ch
        else:
            if depth >= 1:
                buf += ch
        i += 1
    return rows


def _parse_row(row_str: str):
    """Разбить '1, NULL, 'abc', 2.5' на список значений (с учётом строк)."""
    parts = []
    cur = ""
    in_str = False
    i = 0
    n = len(row_str)
    while i < n:
        ch = row_str[i]
        if in_str:
            if ch == "\\" and i + 1 < n:
                cur += ch + row_str[i + 1]
                i += 2
                continue
            if ch == "'":
                in_str = False
            cur += ch
        else:
            if ch == "'":
                in_str = True
                cur += ch
            elif ch == ",":
                parts.append(cur)
                cur = ""
            else:
                cur += ch
        i += 1
    if cur.strip():
        parts.append(cur)
    return [_parse_value(p) for p in parts]


def parse_sql_dump(sql_path: str) -> dict:
    """Парсит SQL дамп и возвращает {table_name: [row_dict, ...]}."""
    with open(sql_path, encoding="utf-8") as f:
        sql = f.read()

    result = {}
    # Найти все CREATE TABLE (для колонок) и связанные INSERT INTO
    create_re = re.compile(
        r"CREATE TABLE\s+`(\w+)`\s*\((.+?)\)\s*ENGINE",
        re.DOTALL,
    )
    columns_re = re.compile(r"^\s*`(\w+)`", re.MULTILINE)
    tables_schema = {}
    for m in create_re.finditer(sql):
        name = m.group(1)
        body = m.group(2)
        cols = columns_re.findall(body)
        tables_schema[name] = cols

    # Парсим INSERT INTO `Table` (cols...) VALUES (...), (...);
    insert_re = re.compile(
        r"INSERT INTO\s+`(\w+)`\s*\(([^)]+)\)\s*VALUES\s+(.+?);\s*$",
        re.DOTALL | re.MULTILINE,
    )
    for m in insert_re.finditer(sql):
        table = m.group(1)
        cols_str = m.group(2)
        values_str = m.group(3)
        cols = [c.strip().strip("`") for c in cols_str.split(",")]
        rows = _split_values(values_str)
        out = []
        for r in rows:
            if len(r) != len(cols):
                # Кривая строка — пропускаем
                continue
            d = dict(zip(cols, r))
            out.append(d)
        result.setdefault(table, []).extend(out)
    return result


# ════════════════════════════════════════════════════════════════
# Маппинг и миграция
# ════════════════════════════════════════════════════════════════

def _ts(s: str) -> float:
    """'2026-01-31 12:33:13' → unix ts."""
    if not s:
        return 0.0
    try:
        return datetime.strptime(s, "%Y-%m-%d %H:%M:%S").timestamp()
    except Exception:
        return 0.0


def _norm_chat_id(cid) -> str:
    """Зеркало storage._norm_chat_id — нормализация chat_id для ключа.
    -1003852131311 → '3852131311' (без минуса, без префикса 100)."""
    try:
        n = abs(int(cid))
    except Exception:
        return ""
    s = str(n)
    if len(s) >= 12 and s.startswith("100"):
        s = s[3:]
    return s


def _map_drop_status(drop_row: dict) -> str:
    """send=0 accept=0 ready=0 → draft
       send=1 accept=0           → pending
       accept>0 ready=0          → accepted
       ready=1                   → done"""
    send = int(drop_row.get("send") or 0)
    accept = int(drop_row.get("accept") or 0)
    ready = int(drop_row.get("ready") or 0)
    if ready:
        return "done"
    if accept:
        return "accepted"
    if send:
        return "pending"
    return "draft"


def _map_lk_status(lk_row: dict) -> str:
    """ready=0 → pending; ready=1 → done (упрощённо)."""
    ready = int(lk_row.get("ready") or 0)
    if ready:
        return "done"
    return "new"


def migrate(state: dict, dump: dict, dry_run: bool = True) -> dict:
    """Производит миграцию dump → state. Возвращает отчёт."""
    report = {
        "owners_added": 0, "owners_skipped": 0,
        "drops_added": 0, "drops_skipped": 0,
        "lks_added": 0, "lks_skipped": 0,
        "chats_added": 0, "chats_skipped": 0,
        "errors": [],
    }

    # === 1. OWNERS ===
    old_owners = dump.get("Owners", [])
    crm_owners = state.setdefault("crm_owners", {})
    crm_owners_seq = int(state.get("crm_owners_seq") or 0)
    # Индекс по tg_user_id чтобы дедупить
    by_tg = {int(o.get("tg_user_id") or 0): oid for oid, o in crm_owners.items()}
    # Карта old.id → new.owner_id (понадобится для Drops)
    old_owner_to_new = {}

    for o in old_owners:
        tg_id = int(o.get("userId") or 0)
        username = (o.get("username") or "").lstrip("@").strip()
        if not tg_id and not username:
            report["owners_skipped"] += 1
            continue
        # Дедуп
        if tg_id and tg_id in by_tg:
            old_owner_to_new[int(o["id"])] = by_tg[tg_id]
            report["owners_skipped"] += 1
            continue
        crm_owners_seq += 1
        new_oid = f"o{crm_owners_seq:03d}"
        crm_owners[new_oid] = {
            "owner_id": new_oid,
            "tg_user_id": tg_id,
            "username": username,
            "name": username,
            "joined_at": _ts(o.get("createdAt")) or time.time(),
            "last_active_ts": _ts(o.get("updatedAt")) or time.time(),
            "total_drops": 0,
            "total_revenue_usd": float(o.get("dolg") or 0.0),
            "rating": 5.0,
            "banned_until": 0,
            "work_chat_id": None,  # привяжем позже из Chats
            "warnings": 0,
            "debt_usdt": float(o.get("dolg") or 0.0),  # старый dolg
            "limits": int(o.get("limits") or 0),
            "perc": float(o.get("perc") or 0.0),
            "_migrated_from_old_id": int(o["id"]),
        }
        by_tg[tg_id] = new_oid
        old_owner_to_new[int(o["id"])] = new_oid
        report["owners_added"] += 1

    state["crm_owners_seq"] = crm_owners_seq

    # === 2. CHATS ===
    old_chats = dump.get("Chats", [])
    crm_chats = state.setdefault("crm_chats", {})
    for c in old_chats:
        chat_id_raw = c.get("chatId")
        if not chat_id_raw:
            continue
        # ВАЖНО: ключ обязан быть нормализован — иначе bot.find_crm_admin_chat
        # и get_crm_chat не найдут запись после миграции.
        key = _norm_chat_id(chat_id_raw)
        if not key:
            continue
        if key in crm_chats:
            report["chats_skipped"] += 1
            continue
        old_owner_id = c.get("ownerId")
        new_owner_id = old_owner_to_new.get(int(old_owner_id)) if old_owner_id else None
        crm_chats[key] = {
            "chat_id": int(chat_id_raw),
            "owner_id": new_owner_id or "",
            "is_admin": bool(c.get("admin")),
            "is_password": bool(c.get("password")),
            "is_otr": bool(c.get("otr")),
            "registered_at": _ts(c.get("createdAt")) or time.time(),
            "_migrated_from_old_id": int(c["id"]),
        }
        # Если owner есть и чат не admin/password — привяжем как work_chat
        if new_owner_id and not (c.get("admin") or c.get("password") or c.get("otr")):
            owner = crm_owners.get(new_owner_id)
            if owner and not owner.get("work_chat_id"):
                owner["work_chat_id"] = int(chat_id_raw)
        report["chats_added"] += 1

    # === 3. DROPS ===
    old_drops = dump.get("Drops", [])
    crm_drops = state.setdefault("crm_drops", {})
    crm_drops_seq = int(state.get("crm_drops_seq") or 0)
    # Дедуп по (owner_id, fio) lower
    existing_keys = {
        (d.get("owner_id"), (d.get("fio") or "").lower().strip())
        for d in crm_drops.values()
    }
    old_drop_to_new = {}
    for d in old_drops:
        old_owner_id = int(d.get("ownerId") or 0)
        new_owner_id = old_owner_to_new.get(old_owner_id)
        if not new_owner_id:
            report["drops_skipped"] += 1
            continue
        fio = (d.get("fio") or "").strip()
        if not fio:
            report["drops_skipped"] += 1
            continue
        dedup_key = (new_owner_id, fio.lower())
        if dedup_key in existing_keys:
            report["drops_skipped"] += 1
            continue
        crm_drops_seq += 1
        new_did = f"d{crm_drops_seq:04d}"
        # scan field — JSON-массив file_id'ов
        scan_raw = d.get("scan")
        scan_ids = []
        if scan_raw:
            try:
                if isinstance(scan_raw, str):
                    parsed = json.loads(scan_raw)
                    if isinstance(parsed, list):
                        scan_ids = [str(x) for x in parsed]
                elif isinstance(scan_raw, list):
                    scan_ids = [str(x) for x in scan_raw]
            except Exception:
                pass
        crm_drops[new_did] = {
            "drop_id": new_did,
            "owner_id": new_owner_id,
            "work_chat_id": int(d.get("chatId")) if d.get("chatId") else None,
            "fio": fio,
            "about": d.get("about") or "",
            "scan_file_ids": scan_ids,
            "price_usdt": int(d.get("price") or 0),
            "status": _map_drop_status(d),
            "buydate": d.get("buydate") or "",
            "deal": d.get("deal") or "",
            "prolit_count": int(d.get("prolit") or 0),
            "send_ts": _ts(d.get("createdAt")) if d.get("send") else 0,
            "accept_ts": _ts(d.get("updatedAt")) if d.get("accept") else 0,
            "done_ts": _ts(d.get("updatedAt")) if d.get("ready") else 0,
            "link_access": d.get("link_access") or "",
            "admin_msg_id": int(d.get("msgid") or 0),
            "lk_card_ids": [],  # заполним когда будут lk_cards
            "social": "", "residence": "", "other_banks": "",
            "created_at": _ts(d.get("createdAt")) or time.time(),
            "history": [],
            "_migrated_from_old_id": int(d["id"]),
        }
        existing_keys.add(dedup_key)
        old_drop_to_new[int(d["id"])] = new_did
        # Обновим total_drops у owner'а
        if new_owner_id in crm_owners:
            crm_owners[new_owner_id]["total_drops"] = (
                crm_owners[new_owner_id].get("total_drops", 0) + 1
            )
        report["drops_added"] += 1

    state["crm_drops_seq"] = crm_drops_seq

    # === 4. DROP LKs ===
    old_lks = dump.get("DropLKs", [])
    crm_lks = state.setdefault("crm_drop_lks", {})
    crm_lks_seq = int(state.get("crm_drop_lks_seq") or 0)
    for l in old_lks:
        old_drop_id = int(l.get("dropId") or 0)
        new_drop_id = old_drop_to_new.get(old_drop_id)
        if not new_drop_id:
            report["lks_skipped"] += 1
            continue
        new_owner_id = old_owner_to_new.get(int(l.get("ownerId") or 0)) or ""
        crm_lks_seq += 1
        new_lkid = f"lk{crm_lks_seq:04d}"
        # SMS history
        sms_history = []
        sms_raw = l.get("sms")
        if sms_raw:
            try:
                if isinstance(sms_raw, str):
                    sms_history = json.loads(sms_raw)
                elif isinstance(sms_raw, list):
                    sms_history = sms_raw
            except Exception:
                pass
        crm_lks[new_lkid] = {
            "droplk_id": new_lkid,
            "drop_id": new_drop_id,
            "owner_id": new_owner_id,
            "bank": (l.get("bank") or "").strip(),
            "value": l.get("value") or "",
            "deal": l.get("deal") or "",
            "status": _map_lk_status(l),
            "sms_history": sms_history,
            "new_login": "",
            "new_password": l.get("new_password") or "",
            "new_mail": l.get("new_mail") or "",
            "new_number": l.get("new_number") or "",
            "code_word": "",
            "ded_ip": l.get("ded_ip") or "",
            "ded_login": l.get("ded_login") or "Administrator",
            "ded_pass": l.get("ded_pass") or "",
            "ded_location": "",
            "link_pass": l.get("link_pass") or "",
            "msgid_pass": int(l.get("msgid_pass") or 0),
            "created_at": _ts(l.get("createdAt")) or time.time(),
            "_migrated_from_old_id": int(l["id"]),
        }
        # Прицепим к drop
        if new_drop_id in crm_drops:
            crm_drops[new_drop_id].setdefault("lk_card_ids", [])
        report["lks_added"] += 1

    state["crm_drop_lks_seq"] = crm_lks_seq

    return report


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("sql_path", help="Path to SQL dump")
    parser.add_argument("--state", default=None, help="Path to state.json (default: from config)")
    parser.add_argument("--dry-run", action="store_true", help="Не записывать в state.json")
    parser.add_argument("--apply", action="store_true", help="Реально применить (создаст бэкап)")
    args = parser.parse_args()

    if not args.dry_run and not args.apply:
        print("Укажи --dry-run или --apply")
        sys.exit(1)

    # Найти state.json
    state_path = args.state
    if not state_path:
        try:
            sys.path.insert(0, str(Path(__file__).parent))
            import config
            state_path = config.STORAGE_PATH
        except Exception:
            state_path = "state.json"

    print(f"📁 SQL dump: {args.sql_path}")
    print(f"📁 State:    {state_path}")
    print(f"📁 Mode:     {'DRY-RUN' if args.dry_run else 'APPLY'}")
    print()

    print("📖 Парсим SQL дамп...")
    dump = parse_sql_dump(args.sql_path)
    for table, rows in dump.items():
        print(f"   • {table}: {len(rows)} строк")
    print()

    # Загрузить state
    if os.path.exists(state_path):
        with open(state_path, encoding="utf-8") as f:
            state = json.load(f)
        print(f"📖 state.json загружен ({os.path.getsize(state_path)} байт)")
    else:
        print("⚠️  state.json не найден — создам новый")
        state = {}
    print()

    print("🔄 Миграция...")
    report = migrate(state, dump, dry_run=args.dry_run)
    print()

    print("📊 РЕЗУЛЬТАТ:")
    print(f"   • Owners:  +{report['owners_added']} (пропущено {report['owners_skipped']})")
    print(f"   • Chats:   +{report['chats_added']} (пропущено {report['chats_skipped']})")
    print(f"   • Drops:   +{report['drops_added']} (пропущено {report['drops_skipped']})")
    print(f"   • LK:      +{report['lks_added']} (пропущено {report['lks_skipped']})")
    if report.get("errors"):
        print(f"   • Errors:  {len(report['errors'])}")
        for e in report["errors"][:10]:
            print(f"     - {e}")
    print()

    if args.apply:
        # Бэкап
        if os.path.exists(state_path):
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            backup_path = f"{state_path}.pre-migration-{ts}.bak"
            shutil.copy2(state_path, backup_path)
            print(f"💾 Бэкап: {backup_path}")
        # Сохранить
        os.makedirs(os.path.dirname(state_path) or ".", exist_ok=True)
        with open(state_path, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
        print(f"✅ Записано в {state_path}")
    else:
        print("🔍 Dry-run: ничего не записано. Для реального запуска — --apply")


if __name__ == "__main__":
    main()
