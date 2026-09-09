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
                "wallet_trc20": "",
                "directions": {},
                "days": {},
                "payouts": [],
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

    # ---------- STATS ENTRIES ----------
    async def add_stat_entry(
        self,
        chat_id: int,
        date_str: str,
        amount_rub: float,
        direction: str,
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
                "direction": direction,
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
        self, chat_id: int, amount_usd: float, note: str, admin_id: int
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
    def compute_stats(self, chat_id: int, date_str: str | None = None) -> dict:
        """Считаем общую по чату. Если date_str — только за эту дату.
        Возвращает: total_rub, by_direction={dir: rub}, total_usd_before_pay,
        paid_usd, remaining_usd, rate, dir_pcts={dir: pct}."""
        entry = self.get_client_chat(chat_id)
        if not entry:
            return {}
        rate = float(entry.get("rate") or 0)
        days = entry.get("days") or {}
        by_direction: dict[str, float] = {}
        if date_str:
            day = days.get(date_str) or {"entries": []}
            days_iter = [(date_str, day)]
        else:
            days_iter = list(days.items())
        for _, day in days_iter:
            for e in day.get("entries") or []:
                d = e.get("direction") or "—"
                by_direction[d] = by_direction.get(d, 0.0) + float(e.get("amount_rub") or 0)
        total_rub = sum(by_direction.values())

        directions_cfg = entry.get("directions") or {}
        dir_pcts = {}
        total_usd = 0.0
        by_direction_usd: dict[str, float] = {}
        for d, amt_rub in by_direction.items():
            cfg = directions_cfg.get(d)
            pct = float(cfg.get("commission_pct") or 0) if cfg else 0.0
            dir_pcts[d] = pct
            after_comm = amt_rub * (1 - pct / 100.0)
            usd = (after_comm / rate) if rate > 0 else 0.0
            by_direction_usd[d] = usd
            total_usd += usd

        paid_usd = sum(float(p.get("amount_usd") or 0) for p in (entry.get("payouts") or []))
        return {
            "rate": rate,
            "total_rub": total_rub,
            "by_direction_rub": by_direction,
            "by_direction_usd": by_direction_usd,
            "dir_pcts": dir_pcts,
            "total_usd_before_pay": total_usd,
            "paid_usd": paid_usd,
            "remaining_usd": total_usd - paid_usd,
        }


# singleton
storage = CalcStorage()
