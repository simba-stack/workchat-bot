"""JSON-based persistent storage for bot state."""
import json
import os
import asyncio
import time
import secrets
import string
from typing import Optional

import config

_lock = asyncio.Lock()

# Чаты старше этого количества секунд удаляются при очистке
_CHAT_TTL_SEC = 30 * 24 * 3600  # 30 дней


def _gen_secret_command() -> str:
    alphabet = string.ascii_letters + string.digits
    suffix = ''.join(secrets.choice(alphabet) for _ in range(12))
    return f"admin_{suffix}"


def _norm_chat_id(cid) -> str:
    """Normalize chat_id to consistent string key.

    Telegram supergroup IDs arrive in two forms:
      - From Telethon (channel.id):  1234567890       (no prefix)
      - From aiogram events:        -1001234567890    (with -100 prefix)

    We always store the bare ID (without -100).
    Safety check: only strip '100' prefix when the number is ≥ 12 digits
    (a bare 10-digit supergroup ID with '100' prefix = 13 digits total),
    avoiding false matches on short IDs like user_id=100.
    """
    n = abs(int(cid))
    s = str(n)
    if len(s) >= 12 and s.startswith('100'):
        s = s[3:]
    return s


def _norm_deal_id(deal_id) -> str:
    """Нормализует deal_id: убирает решётку и пробелы.

    AI часто шлёт '#95941' (как в чате), храним всегда без решётки.
    """
    return str(deal_id or "").lstrip("#").strip()


def _default_state() -> dict:
    return {
        "admins": [],
        "workers": list(config.DEFAULT_WORKERS),
        "welcome_message": config.DEFAULT_WELCOME,
        "welcome_entities": [],
        "cooldown_minutes": config.DEFAULT_COOLDOWN_MIN,
        "trigger_phrases": list(config.DEFAULT_TRIGGERS),
        "stats": {"total_chats_created": 0, "creations_by_user": {}},
        "user_cooldowns": {},
        "managed_chats": {},
        "admin_secret_command": "",
        "brain_chat_id": 0,
        "client_idle_minutes": 5,
        # Источник трафика: счётчик по источникам и атрибуция per-user
        "source_stats": {},
        "user_sources": {},
        # AI brain (Claude)
        "ai_enabled": False,
        "ai_model": "",  # пусто = берём config.DEFAULT_AI_MODEL
        "ai_stats": {
            "replies_total": 0,
            "input_tokens_total": 0,
            "output_tokens_total": 0,
            "errors_total": 0,
            "skipped_worker_active": 0,
        },
        # Auto-writeback в knowledge/ через GitHub API (memory.py)
        "ai_writeback_enabled": False,
        "writeback_stats": {
            "commits_total": 0,
            "skipped_total": 0,
            "errors_total": 0,
        },
        # Координаторская беседа — куда AI шлёт эскалации команде
        "coordination_chat_id": 0,
        "escalate_stats": {
            "calls_total": 0,
            "by_specialist": {},
            "errors_total": 0,
        },
        # === Учёт сделок ===
        # Чат «Сделки и выплаты» (логирование/обновления статусов)
        "deals_group_id": 0,
        # Чат «Отработка аккаунтов» (источник статусов от операционистов)
        "accounts_group_id": 0,
        # Сами сделки: deal_id (str) -> {client_username, fio, bank, amount,
        # fee, method, status, created_at, history: [{ts, status}]}
        "deals": {},
        "deals_stats": {
            "created_total": 0,
            "by_status": {},
            "errors_total": 0,
        },
    }


class Storage:
    def __init__(self, path: str):
        self.path = path
        self.state = _default_state()
        d = os.path.dirname(path) or "."
        os.makedirs(d, exist_ok=True)

    async def load(self):
        async with _lock:
            if os.path.exists(self.path):
                try:
                    with open(self.path, "r", encoding="utf-8") as f:
                        loaded = json.load(f)
                    # Добавляем отсутствующие ключи из defaults (миграция)
                    defaults = _default_state()
                    for k, v in defaults.items():
                        if k not in loaded:
                            loaded[k] = v
                    self.state = loaded
                except Exception as e:
                    print(f"[storage] load failed, using defaults: {e}")
            if not self.state.get("admin_secret_command"):
                self.state["admin_secret_command"] = _gen_secret_command()
            if config.ADMIN_ID and config.ADMIN_ID not in self.state["admins"]:
                self.state["admins"].append(config.ADMIN_ID)
            await self._save_unlocked()

    async def _save_unlocked(self):
        """Атомарная запись через .tmp + os.replace. Перед заменой сохраняет .bak."""
        try:
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.state, f, ensure_ascii=False, indent=2)
            # Бэкап текущего файла перед заменой
            if os.path.exists(self.path):
                try:
                    os.replace(self.path, self.path + ".bak")
                except Exception:
                    pass
            os.replace(tmp, self.path)
        except Exception as e:
            print(f"[storage] save failed: {e}")

    async def save(self):
        async with _lock:
            await self._save_unlocked()

    # === Admins ===
    def is_admin(self, user_id: int) -> bool:
        return user_id in self.state["admins"]

    async def add_admin(self, user_id: int):
        async with _lock:
            if user_id not in self.state["admins"]:
                self.state["admins"].append(user_id)
            await self._save_unlocked()

    async def remove_admin(self, user_id: int):
        async with _lock:
            if user_id in self.state["admins"]:
                self.state["admins"].remove(user_id)
            await self._save_unlocked()

    def get_admins(self):
        return list(self.state["admins"])

    # === Workers ===
    def get_workers(self):
        return list(self.state["workers"])

    async def add_worker(self, username: str):
        username = username.lstrip("@").strip()
        async with _lock:
            if username and username not in self.state["workers"]:
                self.state["workers"].append(username)
            await self._save_unlocked()

    async def remove_worker(self, username: str):
        username = username.lstrip("@").strip()
        async with _lock:
            if username in self.state["workers"]:
                self.state["workers"].remove(username)
            await self._save_unlocked()

    # === Welcome ===
    def get_welcome(self) -> str:
        return self.state["welcome_message"]

    def get_welcome_entities(self) -> list:
        return list(self.state.get("welcome_entities") or [])

    async def set_welcome(self, text: str, entities: Optional[list] = None):
        async with _lock:
            self.state["welcome_message"] = text
            self.state["welcome_entities"] = entities or []
            await self._save_unlocked()

    # === Cooldown ===
    def get_cooldown_minutes(self) -> int:
        return self.state["cooldown_minutes"]

    async def set_cooldown_minutes(self, minutes: int):
        async with _lock:
            self.state["cooldown_minutes"] = minutes
            await self._save_unlocked()

    def check_cooldown(self, user_id: int) -> Optional[int]:
        last = self.state["user_cooldowns"].get(str(user_id))
        if not last:
            return None
        cd_sec = self.state["cooldown_minutes"] * 60
        elapsed = time.time() - last
        if elapsed < cd_sec:
            return int(cd_sec - elapsed)
        return None

    async def mark_creation(self, user_id: int):
        async with _lock:
            self.state["user_cooldowns"][str(user_id)] = time.time()
            self.state["stats"]["total_chats_created"] += 1
            uid = str(user_id)
            cur = self.state["stats"]["creations_by_user"].get(uid, 0)
            self.state["stats"]["creations_by_user"][uid] = cur + 1
            await self._save_unlocked()

    # === Triggers ===
    def get_triggers(self):
        return list(self.state["trigger_phrases"])

    # === Managed chats ===
    async def register_chat(self, chat_id, client_id: int, client_name: str):
        key = _norm_chat_id(chat_id)
        async with _lock:
            self.state["managed_chats"][key] = {
                "client_id": client_id,
                "client_name": client_name,
                "created_at": time.time(),
                "welcome_sent": False,
            }
            await self._save_unlocked()

    def get_chat_info(self, chat_id) -> Optional[dict]:
        return self.state["managed_chats"].get(_norm_chat_id(chat_id))

    def get_managed_chat_ids(self) -> list:
        return list(self.state.get("managed_chats", {}).keys())

    async def mark_welcome_sent(self, chat_id):
        key = _norm_chat_id(chat_id)
        async with _lock:
            info = self.state["managed_chats"].get(key)
            if info:
                info["welcome_sent"] = True
            await self._save_unlocked()

    # === Cleanup ===
    async def cleanup(self):
        """Удаляет устаревшие записи: старые чаты и истёкшие кулдауны.
        Вызывается периодически из bot.py.
        """
        now = time.time()
        changed = False
        async with _lock:
            # 1. Удаляем managed_chats старше _CHAT_TTL_SEC
            old_chats = [
                k for k, v in self.state["managed_chats"].items()
                if now - v.get("created_at", now) > _CHAT_TTL_SEC
            ]
            for k in old_chats:
                del self.state["managed_chats"][k]
            if old_chats:
                changed = True
                print(f"[storage] cleanup: removed {len(old_chats)} old managed_chats")

            # 2. Удаляем истёкшие записи user_cooldowns
            cd_sec = self.state["cooldown_minutes"] * 60
            # Оставляем двойной запас, чтобы не обрезать действующие кулдауны
            cutoff = now - cd_sec * 2
            old_cooldowns = [
                k for k, v in self.state["user_cooldowns"].items()
                if v < cutoff
            ]
            for k in old_cooldowns:
                del self.state["user_cooldowns"][k]
            if old_cooldowns:
                changed = True
                print(f"[storage] cleanup: removed {len(old_cooldowns)} expired cooldowns")

            if changed:
                await self._save_unlocked()

    # === Stats ===
    def get_stats(self) -> dict:
        return dict(self.state["stats"])

    # === Secret command ===
    def get_secret_command(self) -> str:
        return self.state["admin_secret_command"]

    # === Brain chat ===
    def get_brain_chat_id(self) -> int:
        return int(self.state.get("brain_chat_id") or 0)

    async def set_brain_chat_id(self, chat_id: int):
        async with _lock:
            self.state["brain_chat_id"] = int(chat_id)
            await self._save_unlocked()

    def get_client_idle_minutes(self) -> int:
        return int(self.state.get("client_idle_minutes") or 5)

    async def set_client_idle_minutes(self, minutes: int):
        async with _lock:
            self.state["client_idle_minutes"] = int(minutes)
            await self._save_unlocked()

    # === Traffic source ===
    async def register_source(self, user_id: int, source: str) -> bool:
        """Атрибутирует пользователя к источнику трафика.

        Считается только первый выбор: повторные клики игнорируются,
        чтобы счётчик не накручивался при повторном /start.
        Возвращает True если источник записан впервые, False если уже был.
        """
        source = (source or "").strip()
        if not source:
            return False
        async with _lock:
            uid = str(user_id)
            sources = self.state.setdefault("user_sources", {})
            stats = self.state.setdefault("source_stats", {})
            if uid in sources:
                return False
            sources[uid] = source
            stats[source] = stats.get(source, 0) + 1
            await self._save_unlocked()
            return True

    def get_source_stats(self) -> dict:
        return dict(self.state.get("source_stats", {}))

    def get_user_source(self, user_id: int) -> Optional[str]:
        return self.state.get("user_sources", {}).get(str(user_id))

    # === AI brain ===
    def is_ai_enabled(self) -> bool:
        return bool(self.state.get("ai_enabled", False))

    async def set_ai_enabled(self, enabled: bool):
        async with _lock:
            self.state["ai_enabled"] = bool(enabled)
            await self._save_unlocked()

    def get_ai_model(self) -> str:
        return self.state.get("ai_model") or ""

    async def set_ai_model(self, model: str):
        async with _lock:
            self.state["ai_model"] = (model or "").strip()
            await self._save_unlocked()

    def get_ai_stats(self) -> dict:
        return dict(self.state.get("ai_stats") or {})

    async def bump_ai_stats(
        self,
        *,
        replies: int = 0,
        input_tokens: int = 0,
        output_tokens: int = 0,
        errors: int = 0,
        skipped_worker_active: int = 0,
    ):
        """Атомарно инкрементит выбранные счётчики ai_stats."""
        async with _lock:
            stats = self.state.setdefault(
                "ai_stats",
                {
                    "replies_total": 0,
                    "input_tokens_total": 0,
                    "output_tokens_total": 0,
                    "errors_total": 0,
                    "skipped_worker_active": 0,
                },
            )
            stats["replies_total"] = int(stats.get("replies_total", 0)) + replies
            stats["input_tokens_total"] = (
                int(stats.get("input_tokens_total", 0)) + input_tokens
            )
            stats["output_tokens_total"] = (
                int(stats.get("output_tokens_total", 0)) + output_tokens
            )
            stats["errors_total"] = int(stats.get("errors_total", 0)) + errors
            stats["skipped_worker_active"] = (
                int(stats.get("skipped_worker_active", 0)) + skipped_worker_active
            )
            await self._save_unlocked()

    # === Writeback в граф знаний (memory.py + GitHub API) ===
    def is_writeback_enabled(self) -> bool:
        return bool(self.state.get("ai_writeback_enabled", False))

    async def set_writeback_enabled(self, enabled: bool):
        async with _lock:
            self.state["ai_writeback_enabled"] = bool(enabled)
            await self._save_unlocked()

    def get_writeback_stats(self) -> dict:
        return dict(self.state.get("writeback_stats") or {})

    async def bump_writeback_stats(
        self, *, commits: int = 0, skipped: int = 0, errors: int = 0
    ):
        async with _lock:
            stats = self.state.setdefault(
                "writeback_stats",
                {"commits_total": 0, "skipped_total": 0, "errors_total": 0},
            )
            stats["commits_total"] = int(stats.get("commits_total", 0)) + commits
            stats["skipped_total"] = int(stats.get("skipped_total", 0)) + skipped
            stats["errors_total"] = int(stats.get("errors_total", 0)) + errors
            await self._save_unlocked()

    # === Координаторская беседа (для эскалации) ===
    def get_coordination_chat_id(self) -> int:
        return int(self.state.get("coordination_chat_id") or 0)

    async def set_coordination_chat_id(self, chat_id: int):
        async with _lock:
            self.state["coordination_chat_id"] = int(chat_id)
            await self._save_unlocked()

    def get_escalate_stats(self) -> dict:
        return dict(self.state.get("escalate_stats") or {})

    async def bump_escalate_stats(self, *, specialist: str = "", error: bool = False):
        async with _lock:
            stats = self.state.setdefault(
                "escalate_stats",
                {"calls_total": 0, "by_specialist": {}, "errors_total": 0},
            )
            if error:
                stats["errors_total"] = int(stats.get("errors_total", 0)) + 1
            else:
                stats["calls_total"] = int(stats.get("calls_total", 0)) + 1
                if specialist:
                    by = stats.setdefault("by_specialist", {})
                    by[specialist] = int(by.get(specialist, 0)) + 1
            await self._save_unlocked()

    # === Учёт сделок ===
    def get_deals_group_id(self) -> int:
        return int(self.state.get("deals_group_id") or 0)

    async def set_deals_group_id(self, chat_id: int):
        async with _lock:
            self.state["deals_group_id"] = int(chat_id)
            await self._save_unlocked()

    def get_accounts_group_id(self) -> int:
        return int(self.state.get("accounts_group_id") or 0)

    async def set_accounts_group_id(self, chat_id: int):
        async with _lock:
            self.state["accounts_group_id"] = int(chat_id)
            await self._save_unlocked()

    def get_deal(self, deal_id: str) -> Optional[dict]:
        return (self.state.get("deals") or {}).get(_norm_deal_id(deal_id))

    def list_deals(self, status: Optional[str] = None) -> dict:
        all_deals = self.state.get("deals") or {}
        if status is None:
            return dict(all_deals)
        return {k: v for k, v in all_deals.items() if v.get("status") == status}

    def find_deal_by(
        self,
        deal_id: Optional[str] = None,
        username: Optional[str] = None,
        fio: Optional[str] = None,
        bank: Optional[str] = None,
    ) -> list:
        """Универсальный поиск сделок. Возвращает список совпавших dict (с deal_id внутри).

        Все условия применяются как AND, но fio/bank сравниваются case-insensitive
        и подстрокой. None — параметр игнорируется.
        """
        out = []
        deals = self.state.get("deals") or {}
        for did, d in deals.items():
            if deal_id and _norm_deal_id(deal_id) != did:
                continue
            if username:
                u = (d.get("client_username") or "").lstrip("@").lower()
                if username.lstrip("@").lower() != u:
                    continue
            if fio:
                if fio.lower() not in (d.get("fio") or "").lower():
                    continue
            if bank:
                if bank.lower() not in (d.get("bank") or "").lower():
                    continue
            out.append({"deal_id": did, **d})
        return out

    async def add_deal(
        self,
        deal_id: str,
        client_username: str,
        fio: str,
        bank: str,
        amount,
        fee,
        method: str,
        status: str = "ПОПОЛНИТЬ",
        work_chat_id=None,
    ) -> bool:
        """Создаёт новую запись сделки. Возвращает False если deal_id уже существует.

        work_chat_id — chat_id рабочей беседы с клиентом (где была создана сделка).
        Используется для прямого уведомления клиента при смене статуса.
        """
        deal_id = _norm_deal_id(deal_id)
        if not deal_id:
            return False
        async with _lock:
            deals = self.state.setdefault("deals", {})
            if deal_id in deals:
                return False
            deals[deal_id] = {
                "client_username": (client_username or "").lstrip("@"),
                "fio": fio or "",
                "bank": bank or "",
                "amount": amount,
                "fee": fee,
                "method": method or "",
                "status": status,
                "created_at": time.time(),
                "history": [{"ts": time.time(), "status": status}],
                "work_chat_id": work_chat_id,
                # message_id поста в чате «Сделки и выплаты» — для редактирования
                "deals_group_msg_id": None,
            }
            stats = self.state.setdefault(
                "deals_stats",
                {"created_total": 0, "by_status": {}, "errors_total": 0},
            )
            stats["created_total"] = int(stats.get("created_total", 0)) + 1
            by = stats.setdefault("by_status", {})
            by[status] = int(by.get(status, 0)) + 1
            await self._save_unlocked()
            return True

    async def update_deal_status(self, deal_id: str, new_status: str) -> bool:
        """Меняет статус сделки + добавляет запись в history. Возвращает False если не найдена."""
        deal_id = _norm_deal_id(deal_id)
        new_status = (new_status or "").strip()
        if not deal_id or not new_status:
            return False
        async with _lock:
            deals = self.state.get("deals") or {}
            d = deals.get(deal_id)
            if not d:
                return False
            old_status = d.get("status", "")
            d["status"] = new_status
            d.setdefault("history", []).append(
                {"ts": time.time(), "status": new_status}
            )
            stats = self.state.setdefault(
                "deals_stats",
                {"created_total": 0, "by_status": {}, "errors_total": 0},
            )
            by = stats.setdefault("by_status", {})
            if old_status:
                by[old_status] = max(0, int(by.get(old_status, 0)) - 1)
            by[new_status] = int(by.get(new_status, 0)) + 1
            await self._save_unlocked()
            return True

    def get_deals_stats(self) -> dict:
        return dict(self.state.get("deals_stats") or {})

    async def set_deals_group_msg_id(self, deal_id: str, msg_id):
        """Запоминает message_id поста в чате 'Сделки и выплаты' для редактирования."""
        async with _lock:
            deals = self.state.get("deals") or {}
            d = deals.get(_norm_deal_id(deal_id))
            if d is None:
                return False
            d["deals_group_msg_id"] = msg_id
            await self._save_unlocked()
            return True


storage = Storage(config.STORAGE_PATH)
