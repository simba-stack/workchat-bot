"""
calc/storage.py — атомарный JSON storage для calc-бота.
Отдельный файл от workchat-bot/storage.py, свой volume путь.

Schema:
{
  "admin_chat_id": int,
  "owner_ids": [int, ...],       # tg_id owner-ов (могут делать админ-команды в DM)
  "client_chats": {
    "<chat_id>": {
      "chat_id": int,
      "chat_title": str,
      "partner_tg_id": int,
      "partner_username": str,
      "rate": float,              # rub → usd (100000 rub / rate = usd)
      "wallet_trc20": str,
      "directions": {             # per-chat, name → cfg
        "<name>": {"name": str, "commission_pct": float, "enabled": bool}
      },
      "days": {                   # per-date
        "<YYYY-MM-DD>": {
          "started_ts": float,
          "entries": [            # каждый +сумма
            {"ts": float, "amount_rub": float, "direction": str,
             "author_id": int, "author_username": str}
          ]
        }
      },
      "payouts": [                # выплаты (админ подтвердил)
        {"ts": float, "amount_usd": float, "note": str, "admin_id": int}
      ],
      "workers": {
        "<tg_id>": {
          "tg_id": int, "username": str, "role": str,
          "perms": {"set_wallet": bool, "add_directions": bool, "request_payout": bool}
        }
      }
    }
  },
  "pending_payouts": [
    {"id": int, "chat_id": int, "amount_usd": float, "wallet": str,
     "requested_by_id": int, "requested_by_name": str,
     "ts": float, "status": "pending"|"paid"|"cancelled",
     "admin_msg_id": int, "client_msg_id": int, "paid_amount_usd": float}
  ],
  "pending_requisites": [
    {"id": int, "chat_id": int, "direction": str, "note": str,
     "requested_by_id": int, "requested_by_name": str,
     "ts": float, "status": "pending"|"done"|"cancelled",
     "admin_msg_id": int, "client_msg_id": int}
  ],
  "next_payout_id": int,
  "next_req_id": int
}
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger("calc.storage")

_DATA_DIR = os.getenv("CALC_DATA_DIR", "/data-calc")
if not os.path.isdir(_DATA_DIR):
    # fallback для локали
    _DATA_DIR = os.path.abspath(
        os.path.join(os.path.dirname(__file__), "..", "_calc_data")
    )
    os.makedirs(_DATA_DIR, exist_ok=True)

_STATE_PATH = Path(_DATA_DIR) / "calc_state.json"
_TMP_PATH = Path(_DATA_DIR) / "calc_state.json.tmp"

_lock = asyncio.Lock()


def _default_state() -> dict:
    return {
        "admin_chat_id": 0,
        "owner_ids": [],
        "client_chats": {},
        "pending_payouts": [],
        "pending_requisites": [],
        "next_payout_id": 1,
        "next_req_id": 1,
        # Глобальные шлюзы — методы приёма платежей. Копируются в client_chat при регистрации.
        # {name: {name, commission_pct, cost_pct, enabled}}
        "gateways": {},
        # Менеджеры: {tg_id: {tg_id, username, name, assignments: [{chat_id, gateway, rule, value}]}}
        # rule: "pct_turnover" (% от rub) | "pct_margin" (% от маржи) | "pct_income" (% от прихода) | "fixed" ($)
        "managers": {},
    }


class CalcStorage:
    def __init__(self):
        self.state: dict = _default_state()

    def load(self) -> None:
        if not _STATE_PATH.exists():
            logger.info("[calc.storage] fresh state at %s", _STATE_PATH)
            return
        try:
            with open(_STATE_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            # merge с дефолтом на случай новых полей
            base = _default_state()
            base.update(data or {})
            self.state = base
            logger.info(
                "[calc.storage] loaded %d client_chats, %d pending_payouts",
                len(self.state.get("client_chats") or {}),
                len(self.state.get("pending_payouts") or []),
            )
        except Exception as e:
            logger.exception("[calc.storage] load failed, using default: %s", e)
            self.state = _default_state()

    async def _save_unlocked(self) -> None:
        """Пишет atomic. Держи _lock перед вызовом."""
        data = json.dumps(self.state, ensure_ascii=False, indent=2)
        with open(_TMP_PATH, "w", encoding="utf-8") as f:
            f.write(data)
        os.replace(_TMP_PATH, _STATE_PATH)

    async def save(self) -> None:
        async with _lock:
            await self._save_unlocked()

    # ---------- OWNERS / ADMIN CHAT ----------
    def is_owner(self, tg_id: int) -> bool:
        return int(tg_id) in [int(x) for x in (self.state.get("owner_ids") or [])]

    def get_admin_chat_id(self) -> int:
        return int(self.state.get("admin_chat_id") or 0)

    async def set_admin_chat(self, chat_id: int) -> None:
        async with _lock:
            self.state["admin_chat_id"] = int(chat_id)
            await self._save_unlocked()

    async def add_owner(self, tg_id: int) -> None:
        async with _lock:
            owners = set(int(x) for x in (self.state.get("owner_ids") or []))
            owners.add(int(tg_id))
            self.state["owner_ids"] = sorted(owners)
            await self._save_unlocked()

    # ---------- CLIENT CHATS ----------
    def get_client_chat(self, chat_id: int) -> dict | None:
        return (self.state.get("client_chats") or {}).get(str(chat_id))

    def list_client_chats(self) -> list[dict]:
        return list((self.state.get("client_chats") or {}).values())

    async def register_client_chat(
        self,
        chat_id: int,
        chat_title: str,
        partner_tg_id: int,
        partner_username: str,
    ) -> dict:
        async with _lock:
            chats = self.state.setdefault("client_chats", {})
            key = str(int(chat_id))
            existing = chats.get(key)
            if existing:
                # обновим partner если поменялся
                existing["chat_title"] = chat_title or existing.get("chat_title", "")
                if partner_tg_id:
                    existing["partner_tg_id"] = int(partner_tg_id)
                if partner_username:
                    existing["partner_username"] = partner_username.lstrip("@")
                await self._save_unlocked()
                return existing
            entry = {
                "chat_id": int(chat_id),
                "chat_title": chat_title or "",
                "partner_tg_id": int(partner_tg_id or 0),
                "partner_username": (partner_username or "").lstrip("@"),
                "rate": 0.0,
                "wallet_trc20": "",       # legacy fallback (общий адрес)
                "directions": {},          # админские способы приёма: {name: {name, commission_pct, enabled}}
                "streams": {},             # партнёрские направления: {name: {name, trc20, enabled}}
                "days": {},
                "payouts": [],             # общий список
                "workers": {},
            }
            chats[key] = entry
            await self._save_unlocked()
            return entry

    async def update_client_chat(self, chat_id: int, **fields) -> dict | None:
        async with _lock:
            entry = (self.state.get("client_chats") or {}).get(str(int(chat_id)))
            if not entry:
                return None
            for k, v in fields.items():
                entry[k] = v
            await self._save_unlocked()
            return entry

    # ---------- DIRECTIONS ----------
    async def set_direction(
        self, chat_id: int, name: str, commission_pct: float, enabled: bool = True
    ) -> None:
        async with _lock:
            entry = (self.state.get("client_chats") or {}).get(str(int(chat_id)))
            if not entry:
                return
            dirs = entry.setdefault("directions", {})
            key = name.strip()
            dirs[key] = {
                "name": key,
                "commission_pct": float(commission_pct),
                "enabled": bool(enabled),
            }
            await self._save_unlocked()

    async def toggle_direction(self, chat_id: int, name: str) -> bool | None:
        async with _lock:
            entry = (self.state.get("client_chats") or {}).get(str(int(chat_id)))
            if not entry:
                return None
            d = (entry.get("directions") or {}).get(name)
            if not d:
                return None
            d["enabled"] = not bool(d.get("enabled"))
            await self._save_unlocked()
            return d["enabled"]

    async def delete_direction(self, chat_id: int, name: str) -> bool:
        async with _lock:
            entry = (self.state.get("client_chats") or {}).get(str(int(chat_id)))
            if not entry:
                return False
            dirs = entry.get("directions") or {}
            if name in dirs:
                del dirs[name]
                await self._save_unlocked()
                return True
            return False

    # ---------- GLOBAL GATEWAYS ----------
    def list_gateways(self) -> dict:
        return self.state.get("gateways") or {}

    async def set_gateway(
        self, name: str,
        merchant_cost_pct: float,
        merchant_rate: float,
        enabled: bool = True,
    ) -> None:
        """Глобальный шлюз: расход % (что мы платим мерчанту) + курс мерчанта.
        Клиентская комиссия и курс — задаются per-client."""
        async with _lock:
            gws = self.state.setdefault("gateways", {})
            existing = gws.get(name) or {}
            gws[name] = {
                "name": name,
                "merchant_cost_pct": float(merchant_cost_pct),
                "merchant_rate": float(merchant_rate),
                "enabled": bool(enabled),
                # Совместимость со старым кодом (не удаляем):
                "commission_pct": float(existing.get("commission_pct") or 0),
                "cost_pct": float(merchant_cost_pct),
            }
            await self._save_unlocked()

    async def toggle_gateway(self, name: str) -> bool | None:
        async with _lock:
            gws = self.state.get("gateways") or {}
            g = gws.get(name)
            if not g:
                return None
            g["enabled"] = not bool(g.get("enabled"))
            await self._save_unlocked()
            return g["enabled"]

    async def delete_gateway(self, name: str) -> bool:
        async with _lock:
            gws = self.state.get("gateways") or {}
            if name in gws:
                del gws[name]
                await self._save_unlocked()
                return True
            return False

    async def apply_gateways_to_chat(self, chat_id: int) -> int:
        """Копирует ВКЛЮЧЁННЫЕ глобальные шлюзы клиенту как directions.
        commission_pct у клиента = 0 (задаёт админ индивидуально).
        Существующие direction'ы не перезаписываются."""
        async with _lock:
            entry = (self.state.get("client_chats") or {}).get(str(int(chat_id)))
            if not entry:
                return 0
            gws = self.state.get("gateways") or {}
            dirs = entry.setdefault("directions", {})
            added = 0
            for name, g in gws.items():
                if not g.get("enabled"):
                    continue
                if name in dirs:
                    continue
                dirs[name] = {
                    "name": name,
                    "commission_pct": 0.0,  # админ задаёт под клиента
                    "enabled": True,
                }
                added += 1
            if added:
                await self._save_unlocked()
            return added

    # ---------- MANAGERS ----------
    def list_managers(self) -> dict:
        return self.state.get("managers") or {}

    def get_manager(self, tg_id: int) -> dict | None:
        return (self.state.get("managers") or {}).get(str(int(tg_id)))

    async def add_manager(self, tg_id: int, username: str, name: str) -> dict:
        async with _lock:
            mgs = self.state.setdefault("managers", {})
            key = str(int(tg_id))
            existing = mgs.get(key)
            if existing:
                if username:
                    existing["username"] = username.lstrip("@")
                if name:
                    existing["name"] = name
                await self._save_unlocked()
                return existing
            m = {
                "tg_id": int(tg_id),
                "username": (username or "").lstrip("@"),
                "name": name or "",
                "assignments": [],
            }
            mgs[key] = m
            await self._save_unlocked()
            return m

    async def delete_manager(self, tg_id: int) -> bool:
        async with _lock:
            mgs = self.state.get("managers") or {}
            key = str(int(tg_id))
            if key not in mgs:
                return False
            del mgs[key]
            await self._save_unlocked()
            return True

    async def add_manager_rule(
        self, tg_id: int, chat_id: int, gateway: str,
        rule_type: str, value: float,
    ) -> bool:
        """rule_type: pct_turnover | pct_margin | pct_income | fixed"""
        async with _lock:
            m = (self.state.get("managers") or {}).get(str(int(tg_id)))
            if not m:
                return False
            assignments = m.setdefault("assignments", [])
            # Replace если такой (chat_id + gateway) уже есть
            for a in assignments:
                if int(a.get("chat_id") or 0) == int(chat_id) and (a.get("gateway") or "") == gateway:
                    a["rule"] = rule_type
                    a["value"] = float(value)
                    await self._save_unlocked()
                    return True
            assignments.append({
                "chat_id": int(chat_id),
                "gateway": gateway or "",
                "rule": rule_type,
                "value": float(value),
            })
            await self._save_unlocked()
            return True

    async def del_manager_rule(self, tg_id: int, chat_id: int, gateway: str) -> bool:
        async with _lock:
            m = (self.state.get("managers") or {}).get(str(int(tg_id)))
            if not m:
                return False
            before = len(m.get("assignments") or [])
            m["assignments"] = [
                a for a in (m.get("assignments") or [])
                if not (int(a.get("chat_id") or 0) == int(chat_id) and (a.get("gateway") or "") == gateway)
            ]
            after = len(m["assignments"])
            if before != after:
                await self._save_unlocked()
                return True
            return False

    # ---------- DANGER: DELETE ----------
    async def delete_client_chat(self, chat_id: int) -> bool:
        """Полное удаление клиентского чата из БД."""
        async with _lock:
            chats = self.state.get("client_chats") or {}
            key = str(int(chat_id))
            if key not in chats:
                return False
            del chats[key]
            # Заодно чистим связанные заявки
            self.state["pending_payouts"] = [
                p for p in (self.state.get("pending_payouts") or [])
                if int(p.get("chat_id") or 0) != int(chat_id)
            ]
            self.state["pending_requisites"] = [
                r for r in (self.state.get("pending_requisites") or [])
                if int(r.get("chat_id") or 0) != int(chat_id)
            ]
            await self._save_unlocked()
            return True

    async def reset_today_all_chats(self, date_str: str) -> dict:
        """Обнуляет за сегодня: entries[date_str], сегодняшние payouts,
        все pending_payouts/pending_requisites (в любом статусе).
        Возвращает {chats_touched, entries_removed, payouts_removed, pending_removed}."""
        import time
        from datetime import datetime as _dt, timezone as _tz, timedelta as _td
        _MSK = _tz(_td(hours=3))
        chats_touched = 0
        entries_removed = 0
        payouts_removed = 0
        async with _lock:
            chats = self.state.get("client_chats") or {}
            for c in chats.values():
                # entries
                days = c.get("days") or {}
                if date_str in days:
                    entries_removed += len(days[date_str].get("entries") or [])
                    del days[date_str]
                    chats_touched += 1
                # payouts за сегодня
                new_payouts = []
                for p in c.get("payouts") or []:
                    ts = p.get("ts") or 0
                    pdate = _dt.fromtimestamp(ts, _MSK).strftime("%Y-%m-%d") if ts else ""
                    if pdate == date_str:
                        payouts_removed += 1
                        continue
                    new_payouts.append(p)
                c["payouts"] = new_payouts
                # сброс day_reset_ts
                if c.get("day_reset_ts"):
                    c["day_reset_ts"] = 0
            pending_removed = len(self.state.get("pending_payouts") or []) + len(self.state.get("pending_requisites") or [])
            self.state["pending_payouts"] = []
            self.state["pending_requisites"] = []
            await self._save_unlocked()
        return {
            "chats_touched": chats_touched,
            "entries_removed": entries_removed,
            "payouts_removed": payouts_removed,
            "pending_removed": pending_removed,
        }

    async def reset_today_for_chat(self, chat_id: int, date_str: str) -> dict:
        """Обнуляет за сегодня для одного клиентского чата."""
        import time
        from datetime import datetime as _dt, timezone as _tz, timedelta as _td
        _MSK = _tz(_td(hours=3))
        entries_removed = 0
        payouts_removed = 0
        pending_removed = 0
        async with _lock:
            c = (self.state.get("client_chats") or {}).get(str(int(chat_id)))
            if not c:
                return {"entries_removed": 0, "payouts_removed": 0, "pending_removed": 0}
            days = c.get("days") or {}
            if date_str in days:
                entries_removed = len(days[date_str].get("entries") or [])
                del days[date_str]
            new_payouts = []
            for p in c.get("payouts") or []:
                ts = p.get("ts") or 0
                pdate = _dt.fromtimestamp(ts, _MSK).strftime("%Y-%m-%d") if ts else ""
                if pdate == date_str:
                    payouts_removed += 1
                    continue
                new_payouts.append(p)
            c["payouts"] = new_payouts
            c["day_reset_ts"] = 0
            # pending заявки этого чата — сбрасываем все
            new_pp = []
            for p in self.state.get("pending_payouts") or []:
                if int(p.get("chat_id") or 0) == int(chat_id):
                    pending_removed += 1
                    continue
                new_pp.append(p)
            self.state["pending_payouts"] = new_pp
            new_pr = []
            for r in self.state.get("pending_requisites") or []:
                if int(r.get("chat_id") or 0) == int(chat_id):
                    pending_removed += 1
                    continue
                new_pr.append(r)
            self.state["pending_requisites"] = new_pr
            await self._save_unlocked()
        return {
            "entries_removed": entries_removed,
            "payouts_removed": payouts_removed,
            "pending_removed": pending_removed,
        }

    async def reset_client_stats(self, chat_id: int) -> bool:
        """Обнуляет статистику (days + payouts) клиентского чата,
        сохраняя направления, курс, работников."""
        async with _lock:
            entry = (self.state.get("client_chats") or {}).get(str(int(chat_id)))
            if not entry:
                return False
            entry["days"] = {}
            entry["payouts"] = []
            # заявки чата тоже
            self.state["pending_payouts"] = [
                p for p in (self.state.get("pending_payouts") or [])
                if int(p.get("chat_id") or 0) != int(chat_id)
            ]
            self.state["pending_requisites"] = [
                r for r in (self.state.get("pending_requisites") or [])
                if int(r.get("chat_id") or 0) != int(chat_id)
            ]
            await self._save_unlocked()
            return True

    # ---------- CHAT MEMBERS TRACKER (username → tg_id) ----------
    async def remember_member(
        self, chat_id: int, tg_id: int, username: str
    ) -> None:
        """Запоминаем кто писал в чате — для последующего добавления в работники."""
        if not tg_id or not username:
            return
        async with _lock:
            entry = (self.state.get("client_chats") or {}).get(str(int(chat_id)))
            if not entry:
                return
            mem = entry.setdefault("members", {})
            mem[username.lstrip("@").lower()] = {
                "tg_id": int(tg_id),
                "username": username.lstrip("@"),
            }
            await self._save_unlocked()

    def find_member(self, chat_id: int, username: str) -> dict | None:
        entry = self.get_client_chat(chat_id)
        if not entry:
            return None
        mem = entry.get("members") or {}
        return mem.get((username or "").lstrip("@").lower())

    # ---------- STREAMS (партнёрские направления с TRC20) ----------
    async def add_stream(
        self, chat_id: int, name: str, trc20: str, enabled: bool = True
    ) -> None:
        async with _lock:
            entry = (self.state.get("client_chats") or {}).get(str(int(chat_id)))
            if not entry:
                return
            streams = entry.setdefault("streams", {})
            streams[name] = {"name": name, "trc20": trc20 or "", "enabled": bool(enabled)}
            await self._save_unlocked()

    async def update_stream_trc20(self, chat_id: int, name: str, trc20: str) -> bool:
        async with _lock:
            entry = (self.state.get("client_chats") or {}).get(str(int(chat_id)))
            if not entry:
                return False
            streams = entry.get("streams") or {}
            if name not in streams:
                return False
            streams[name]["trc20"] = trc20 or ""
            await self._save_unlocked()
            return True

    async def toggle_stream(self, chat_id: int, name: str) -> bool | None:
        async with _lock:
            entry = (self.state.get("client_chats") or {}).get(str(int(chat_id)))
            if not entry:
                return None
            s = (entry.get("streams") or {}).get(name)
            if not s:
                return None
            s["enabled"] = not bool(s.get("enabled"))
            await self._save_unlocked()
            return s["enabled"]

    async def delete_stream(self, chat_id: int, name: str) -> bool:
        async with _lock:
            entry = (self.state.get("client_chats") or {}).get(str(int(chat_id)))
            if not entry:
                return False
            streams = entry.get("streams") or {}
            if name in streams:
                del streams[name]
                await self._save_unlocked()
                return True
            return False

    # ---------- STATS ENTRIES ----------
    async def add_stat_entry(
        self,
        chat_id: int,
        date_str: str,
        amount_rub: float,
        stream: str,               # партнёрское направление (магазин)
        payment_method: str,       # админский способ приёма
        author_id: int,
        author_username: str,
    ) -> dict | None:
        async with _lock:
            entry = (self.state.get("client_chats") or {}).get(str(int(chat_id)))
            if not entry:
                return None
            days = entry.setdefault("days", {})
            day = days.setdefault(date_str, {"started_ts": 0.0, "entries": []})
            import time
            record = {
                "ts": time.time(),
                "amount_rub": float(amount_rub),
                "stream": stream,
                "payment_method": payment_method,
                # legacy alias:
                "direction": payment_method,
                "author_id": int(author_id),
                "author_username": (author_username or "").lstrip("@"),
            }
            day["entries"].append(record)
            await self._save_unlocked()
            return record

    async def start_day(self, chat_id: int, date_str: str) -> None:
        async with _lock:
            entry = (self.state.get("client_chats") or {}).get(str(int(chat_id)))
            if not entry:
                return
            import time
            days = entry.setdefault("days", {})
            day = days.setdefault(date_str, {"started_ts": 0.0, "entries": []})
            day["started_ts"] = time.time()
            await self._save_unlocked()

    # ---------- PAYOUTS ----------
    async def create_payout_request(
        self,
        chat_id: int,
        amount_usd: float,
        wallet: str,
        requested_by_id: int,
        requested_by_name: str,
        stream: str = "",
    ) -> dict:
        async with _lock:
            pid = int(self.state.get("next_payout_id") or 1)
            import time
            req = {
                "id": pid,
                "chat_id": int(chat_id),
                "amount_usd": float(amount_usd),
                "wallet": wallet or "",
                "requested_by_id": int(requested_by_id),
                "requested_by_name": (requested_by_name or "").lstrip("@"),
                "ts": time.time(),
                "status": "pending",
                "admin_msg_id": 0,
                "client_msg_id": 0,
                "paid_amount_usd": 0.0,
                "stream": stream or "",
            }
            self.state.setdefault("pending_payouts", []).append(req)
            self.state["next_payout_id"] = pid + 1
            await self._save_unlocked()
            return req

    def get_payout_request(self, pid: int) -> dict | None:
        for r in self.state.get("pending_payouts") or []:
            if int(r.get("id") or 0) == int(pid):
                return r
        return None

    async def update_payout_request(self, pid: int, **fields) -> dict | None:
        async with _lock:
            for r in self.state.get("pending_payouts") or []:
                if int(r.get("id") or 0) == int(pid):
                    for k, v in fields.items():
                        r[k] = v
                    await self._save_unlocked()
                    return r
            return None

    async def record_payout(
        self, chat_id: int, amount_usd: float, note: str, admin_id: int,
        stream: str = "",
    ) -> None:
        async with _lock:
            entry = (self.state.get("client_chats") or {}).get(str(int(chat_id)))
            if not entry:
                return
            import time
            entry.setdefault("payouts", []).append({
                "ts": time.time(),
                "amount_usd": float(amount_usd),
                "note": note or "",
                "admin_id": int(admin_id),
                "stream": stream or "",
            })
            await self._save_unlocked()

    # ---------- REQUISITE REQUESTS ----------
    async def create_requisite_request(
        self,
        chat_id: int,
        direction: str,
        note: str,
        requested_by_id: int,
        requested_by_name: str,
    ) -> dict:
        async with _lock:
            rid = int(self.state.get("next_req_id") or 1)
            import time
            req = {
                "id": rid,
                "chat_id": int(chat_id),
                "direction": direction,
                "note": note or "",
                "requested_by_id": int(requested_by_id),
                "requested_by_name": (requested_by_name or "").lstrip("@"),
                "ts": time.time(),
                "status": "pending",
                "admin_msg_id": 0,
                "client_msg_id": 0,
            }
            self.state.setdefault("pending_requisites", []).append(req)
            self.state["next_req_id"] = rid + 1
            await self._save_unlocked()
            return req

    def get_requisite_request(self, rid: int) -> dict | None:
        for r in self.state.get("pending_requisites") or []:
            if int(r.get("id") or 0) == int(rid):
                return r
        return None

    async def update_requisite_request(self, rid: int, **fields) -> dict | None:
        async with _lock:
            for r in self.state.get("pending_requisites") or []:
                if int(r.get("id") or 0) == int(rid):
                    for k, v in fields.items():
                        r[k] = v
                    await self._save_unlocked()
                    return r
            return None

    # ---------- WORKERS ----------
    async def add_worker(
        self,
        chat_id: int,
        worker_tg_id: int,
        username: str,
        role: str,
        perms: dict | None = None,
    ) -> dict | None:
        async with _lock:
            entry = (self.state.get("client_chats") or {}).get(str(int(chat_id)))
            if not entry:
                return None
            workers = entry.setdefault("workers", {})
            key = str(int(worker_tg_id))
            w = workers.get(key) or {
                "tg_id": int(worker_tg_id),
                "username": (username or "").lstrip("@"),
                "role": role or "",
                "perms": {
                    "set_wallet": False,
                    "add_directions": False,
                    "request_payout": False,
                },
            }
            if username:
                w["username"] = username.lstrip("@")
            if role:
                w["role"] = role
            if perms:
                w["perms"].update(perms)
            workers[key] = w
            await self._save_unlocked()
            return w

    async def remove_worker(self, chat_id: int, worker_tg_id: int) -> bool:
        async with _lock:
            entry = (self.state.get("client_chats") or {}).get(str(int(chat_id)))
            if not entry:
                return False
            workers = entry.get("workers") or {}
            key = str(int(worker_tg_id))
            if key in workers:
                del workers[key]
                await self._save_unlocked()
                return True
            return False

    async def toggle_worker_perm(
        self, chat_id: int, worker_tg_id: int, perm: str
    ) -> bool | None:
        async with _lock:
            entry = (self.state.get("client_chats") or {}).get(str(int(chat_id)))
            if not entry:
                return None
            workers = entry.get("workers") or {}
            w = workers.get(str(int(worker_tg_id)))
            if not w:
                return None
            perms = w.setdefault("perms", {})
            perms[perm] = not bool(perms.get(perm))
            await self._save_unlocked()
            return perms[perm]

    # ---------- AGGREGATES ----------
    def get_pending_payout_amounts_by_stream(self, chat_id: int) -> dict[str, float]:
        """{stream: sum of pending/taken payout requests}."""
        out: dict[str, float] = {}
        for r in self.state.get("pending_payouts") or []:
            if int(r.get("chat_id") or 0) != int(chat_id):
                continue
            if r.get("status") not in ("pending", "taken"):
                continue
            s = r.get("stream") or "—"
            out[s] = out.get(s, 0.0) + float(r.get("amount_usd") or 0)
        return out

    def compute_stats(self, chat_id: int, date_str: str | None = None,
                       date_from: str | None = None,
                       respect_day_reset: bool = True) -> dict:
        """Двухмерная разбивка:
        - by_stream_rub: {stream: total_rub}
        - by_stream_usd: {stream: total_usd_after_commissions}
        - by_stream_method_rub: {stream: {method: rub}}
        - by_method_rub: {method: total_rub}
        - method_pcts: {method: pct}
        - paid_by_stream: {stream: usd_paid} (по атрибуту stream в payouts)
        - remaining_by_stream: {stream: usd_remaining}
        - total_*: сводные
        """
        entry = self.get_client_chat(chat_id)
        if not entry:
            return {}
        rate = float(entry.get("rate") or 0)
        days = entry.get("days") or {}
        directions_cfg = entry.get("directions") or {}
        method_pcts = {name: float(d.get("commission_pct") or 0) for name, d in directions_cfg.items()}

        by_stream_method_rub: dict[str, dict[str, float]] = {}
        by_method_rub: dict[str, float] = {}
        by_stream_rub: dict[str, float] = {}

        if date_str:
            days_iter = [(date_str, days.get(date_str) or {"entries": []})]
        elif date_from:
            days_iter = [(d, day) for d, day in days.items() if d >= date_from]
        else:
            days_iter = list(days.items())

        # SIMBA 2026-09: если день закрыт вручную, для сегодняшнего запроса
        # (date_str == today) фильтруем entries начиная с day_reset_ts.
        day_reset_ts = float(entry.get("day_reset_ts") or 0) if respect_day_reset else 0.0
        from datetime import datetime as _dt2, timezone as _tz2, timedelta as _td2
        _MSK2 = _tz2(_td2(hours=3))
        today_msk_str = _dt2.now(_MSK2).strftime("%Y-%m-%d")
        apply_reset = day_reset_ts > 0 and (
            date_str == today_msk_str
            or (date_str is None and date_from is None)
        )

        for _, day in days_iter:
            for e in day.get("entries") or []:
                # respect_day_reset — если день закрыт, пропускаем entries до момента сброса
                if apply_reset and float(e.get("ts") or 0) < day_reset_ts:
                    continue
                stream = e.get("stream") or "—"
                method = e.get("payment_method") or e.get("direction") or "—"
                amt = float(e.get("amount_rub") or 0)
                by_stream_method_rub.setdefault(stream, {})
                by_stream_method_rub[stream][method] = by_stream_method_rub[stream].get(method, 0.0) + amt
                by_method_rub[method] = by_method_rub.get(method, 0.0) + amt
                by_stream_rub[stream] = by_stream_rub.get(stream, 0.0) + amt

        # USD расчёт: для каждого stream суммируем по методам с их %
        by_stream_usd: dict[str, float] = {}
        for stream, methods in by_stream_method_rub.items():
            usd = 0.0
            for m, amt_rub in methods.items():
                pct = method_pcts.get(m, 0.0)
                after = amt_rub * (1 - pct / 100.0)
                usd += (after / rate) if rate > 0 else 0.0
            by_stream_usd[stream] = usd

        total_rub = sum(by_stream_rub.values())
        total_usd = sum(by_stream_usd.values())

        # Payouts — раскладываем по stream если указан, иначе в "—".
        # Фильтруем по date_str/date_from.
        from datetime import datetime as _dt, timezone as _tz, timedelta as _td
        _MSK = _tz(_td(hours=3))
        paid_by_stream: dict[str, float] = {}
        for p in (entry.get("payouts") or []):
            ts = p.get("ts") or 0
            if ts:
                pdate = _dt.fromtimestamp(ts, _MSK).strftime("%Y-%m-%d")
                if date_str and pdate != date_str:
                    continue
                if date_from and pdate < date_from:
                    continue
                if apply_reset and float(ts) < day_reset_ts:
                    continue
            s = p.get("stream") or "—"
            paid_by_stream[s] = paid_by_stream.get(s, 0.0) + float(p.get("amount_usd") or 0)
        paid_usd = sum(paid_by_stream.values())

        remaining_by_stream: dict[str, float] = {}
        for s in set(list(by_stream_usd.keys()) + list(paid_by_stream.keys())):
            remaining_by_stream[s] = by_stream_usd.get(s, 0.0) - paid_by_stream.get(s, 0.0)

        # Pending заявки (ещё не оплаченные) — резервируем как "занятое"
        pending_by_stream = self.get_pending_payout_amounts_by_stream(chat_id)
        pending_usd = sum(pending_by_stream.values())
        available_by_stream: dict[str, float] = {}
        for s in set(list(remaining_by_stream.keys()) + list(pending_by_stream.keys())):
            available_by_stream[s] = remaining_by_stream.get(s, 0.0) - pending_by_stream.get(s, 0.0)

        return {
            "rate": rate,
            "total_rub": total_rub,
            "by_stream_rub": by_stream_rub,
            "by_stream_usd": by_stream_usd,
            "by_stream_method_rub": by_stream_method_rub,
            "by_method_rub": by_method_rub,
            "method_pcts": method_pcts,
            "total_usd_before_pay": total_usd,
            "paid_usd": paid_usd,
            "paid_by_stream": paid_by_stream,
            "pending_usd": pending_usd,
            "pending_by_stream": pending_by_stream,
            "remaining_usd": total_usd - paid_usd,
            "remaining_by_stream": remaining_by_stream,
            # available = сколько РЕАЛЬНО можно ещё запросить (после вычета pending)
            "available_usd": total_usd - paid_usd - pending_usd,
            "available_by_stream": available_by_stream,
            # legacy для совместимости со старым UI
            "by_direction_rub": by_method_rub,
            "by_direction_usd": {},
            "dir_pcts": method_pcts,
        }


# singleton
storage = CalcStorage()
