"""JSON-based persistent storage for bot state. Atomic via .tmp+os.replace."""
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

    Always store the bare ID (without -100). Strip '100' only when len >= 12.
    """
    n = abs(int(cid))
    s = str(n)
    if len(s) >= 12 and s.startswith('100'):
        s = s[3:]
    return s


def _norm_deal_id(deal_id) -> str:
    """Нормализует deal_id: убирает решётку и пробелы."""
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
        "source_stats": {},
        "user_sources": {},
        "ai_enabled": False,
        "ai_model": "",
        "ai_stats": {
            "replies_total": 0,
            "input_tokens_total": 0,
            "output_tokens_total": 0,
            "errors_total": 0,
            "skipped_worker_active": 0,
        },
        "ai_writeback_enabled": False,
        "writeback_stats": {
            "commits_total": 0,
            "skipped_total": 0,
            "errors_total": 0,
        },
        "coordination_chat_id": 0,
        "escalate_stats": {
            "calls_total": 0,
            "by_specialist": {},
            "errors_total": 0,
        },
        # deals: deal_id -> {client_username, fio, bank, amount, fee, method,
        # status, created_at, history, work_chat_id}
        # Видимость для команды — через карточку ЛК в Группе 1 (lk_group_id),
        # отдельной публикации в чате сделок больше нет.
        "deals": {},
        "deals_stats": {
            "created_total": 0,
            "by_status": {},
            "errors_total": 0,
        },
        # Чат «Бухгалтерия» (Группа 2 V2 — заявки + расчёт маржи).
        "accounting_group_id": 0,
        # Группа 1 «Личные кабинеты» V2 — анкеты ЛК.
        "lk_group_id": 0,
        # Анкеты ЛК. Ключ — card_id ("lk001"...). Структура:
        # {card_id, supplier (@username), bank, fio, price_usdt,
        #  payment_method (USDT_TRC20/GUARANTOR_BEFORE/_AFTER/_AFTER_WORK),
        #  deal_id, usdt_address, status (В_РАБОТЕ/ОТРАБОТАН/ПОПОЛНИТЬ_И_ОТПУСТИТЬ
        #  /БРАК/БЛОК/ЗАВЕРШЁН), block_amount_rub, block_note, brak_reason,
        #  client_id, client_username, work_chat_id, lk_group_msg_id,
        #  created_at, history}.
        "lk_cards": {},
        # Sequence для генерации card_id
        "lk_cards_seq": 0,
        # Заявки V2: {date_str: [{id, intake, outputs, course_withdrawal,
        # course_payout, partner_pct, computed, ts}, ...]}
        "applications_v2": {},
        # Обратный индекс client_username -> chat_id (последний по created_at).
        # Telegram username case-insensitive, ключи храним в lowercase, без @.
        "client_username_index": {},
        # Роли работников: {uname_lower: {role: str, is_admin: bool}}.
        # При создании новой work_chat юзербот приглашает каждого worker'а
        # и выдаёт ему админ-права (с rank=role в Telegram) если is_admin=True.
        "worker_roles": {},
        # Сводки массовых импортов: {msg_id: {chat_id, html, card_ids[]}}.
        # При удалении карточки из такого импорта — юзербот находит msg по
        # ключу и редактирует html (зачёркивает строку этой карточки).
        "import_summaries": {},
        # Прайс ЛК: {bank_upper: price_usdt}. Единый источник цен для
        # accounting2.lookup_pricing и AI (через knowledge/pricing.md).
        # Менятся через команду «прайс БАНК ЦЕНА» в брейн-чате.
        "pricing": {},
        # Воронка конверсии: ежедневные счётчики операций (для дашборда).
        # Структура: {YYYY-MM-DD: {starts, chats_created, chats_active,
        #   chats_junk, ip_interest, bank_interest, rs_handed, ...}}
        "funnel_stats": {},
        # Статистика по менеджерам/работникам.
        # Структура: {uname_lower: {messages, chats_touched, last_active_ts,
        #   payments_made, lk_completed, junk_handled, ...}}
        "manager_stats": {},
        # Предпочтения клиентов — память на уровне @username (а не чата).
        # Используется чтобы при перевязе НОВОГО ЛК того же клиента AI
        # уже знал прошлый метод оплаты / USDT-адрес — не спрашивал заново.
        # Структура: {uname_lower: {payment_method, usdt_address,
        #   last_updated_ts, lk_count, fio_last, bank_last}}
        "client_preferences": {},
    }


class Storage:
    def __init__(self, path: str):
        self.path = path
        self.state = _default_state()
        d = os.path.dirname(path) or "."
        os.makedirs(d, exist_ok=True)

    def reload_sync(self) -> bool:
        """Синхронный hot-reload state.json — для дашборд-API.
        Bot.py и userbot.py живут разными процессами и каждый держит свой
        in-memory storage.state. Когда userbot пишет в файл, bot.py об этом
        не знает. Дашборд читает через bot.py → видит устаревшее.
        Вызывай эту функцию из API endpoints чтоб подтянуть свежее с диска.
        Возвращает True если что-то перечитал."""
        if not os.path.exists(self.path):
            return False
        try:
            mtime = os.path.getmtime(self.path)
        except OSError:
            return False
        # Не перечитываем если файл не менялся с прошлого раза
        last = getattr(self, "_last_reload_mtime", 0)
        if mtime <= last:
            return False
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                loaded = json.load(f)
        except Exception as e:
            print(f"[storage] reload_sync failed: {e}")
            return False
        # Миграция V2: те же ключи что в load()
        for legacy_key in (
            "accounts_group_id", "pending_accounts_posts",
            "accounting", "deals_group_id",
        ):
            loaded.pop(legacy_key, None)
        for d in (loaded.get("deals") or {}).values():
            if isinstance(d, dict):
                d.pop("accounts_group_msg_id", None)
                d.pop("deals_group_msg_id", None)
        defaults = _default_state()
        for k, v in defaults.items():
            if k not in loaded:
                loaded[k] = v
        self.state = loaded
        self._last_reload_mtime = mtime
        return True

    async def load(self):
        async with _lock:
            if os.path.exists(self.path):
                try:
                    with open(self.path, "r", encoding="utf-8") as f:
                        loaded = json.load(f)
                    # Миграция V2: дропаем ключи старой схемы
                    # (accounts_group_id, pending_accounts_posts, accounting V1,
                    # deals_group_id — устарела, видимость теперь через Группу 1 ЛК)
                    for legacy_key in (
                        "accounts_group_id",
                        "pending_accounts_posts",
                        "accounting",
                        "deals_group_id",
                    ):
                        loaded.pop(legacy_key, None)
                    # Из каждой сделки убираем поля устаревших публикаций.
                    for d in (loaded.get("deals") or {}).values():
                        if isinstance(d, dict):
                            d.pop("accounts_group_msg_id", None)
                            d.pop("deals_group_msg_id", None)
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
        try:
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.state, f, ensure_ascii=False, indent=2)
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
            # Заодно чистим роли — чтобы worker_roles не разрастался
            roles = self.state.setdefault("worker_roles", {})
            roles.pop(username.lower(), None)
            await self._save_unlocked()

    # === Роли работников ===
    # Для каждого worker'а можно задать роль (например "Менеджер", "Оператор")
    # и флаг is_admin. При создании новой work_chat юзербот приглашает worker'а
    # и выдаёт ему админ-права с rank=role если is_admin=True. Если роли нет —
    # worker добавляется обычным участником.

    def get_worker_role(self, username: str) -> dict:
        """Возвращает {role: str, is_admin: bool} для worker'а по username,
        либо пустой dict если роль не задана."""
        if not username:
            return {}
        key = username.lstrip("@").lower().strip()
        if not key:
            return {}
        roles = self.state.get("worker_roles") or {}
        return dict(roles.get(key) or {})

    def list_worker_roles(self) -> dict:
        """Возвращает копию всего worker_roles dict (uname_lower → {role, is_admin})."""
        return dict(self.state.get("worker_roles") or {})

    async def set_worker_role(self, username: str, role: str, is_admin: bool = False):
        """Задаёт роль и админ-флаг для worker'а. Если worker'а не было в
        списке — добавляет (worker_roles работает только с членами workers)."""
        clean = username.lstrip("@").strip()
        if not clean:
            return False
        key = clean.lower()
        async with _lock:
            # Гарантируем что worker в списке
            if clean not in self.state["workers"]:
                self.state["workers"].append(clean)
            roles = self.state.setdefault("worker_roles", {})
            roles[key] = {
                "role": (role or "").strip()[:16] or "Сотрудник",
                "is_admin": bool(is_admin),
            }
            await self._save_unlocked()
            return True

    async def remove_worker_role(self, username: str):
        """Удаляет только роль (worker остаётся в списке)."""
        if not username:
            return
        key = username.lstrip("@").lower().strip()
        if not key:
            return
        async with _lock:
            roles = self.state.setdefault("worker_roles", {})
            roles.pop(key, None)
            await self._save_unlocked()

    def get_welcome(self) -> str:
        return self.state["welcome_message"]

    def get_welcome_entities(self) -> list:
        return list(self.state.get("welcome_entities") or [])

    async def set_welcome(self, text: str, entities: Optional[list] = None):
        async with _lock:
            self.state["welcome_message"] = text
            self.state["welcome_entities"] = entities or []
            await self._save_unlocked()

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

    def get_triggers(self):
        return list(self.state["trigger_phrases"])

    def _username_index_set_unlocked(self, username: str, chat_id) -> bool:
        """Кладёт chat_id в обратный индекс client_username_index по lower-key.
        Если запись уже была — побеждает та, у которой managed_chat свежее
        (по created_at). Возвращает True если индекс обновился."""
        if not username:
            return False
        key_uname = username.lstrip("@").lower().strip()
        if not key_uname:
            return False
        index = self.state.setdefault("client_username_index", {})
        new_key = _norm_chat_id(chat_id)
        if not new_key:
            return False
        existing = index.get(key_uname)
        if existing == new_key:
            return False
        # Сравниваем created_at — оставляем самую свежую беседу.
        managed = self.state.get("managed_chats", {})
        new_at = (managed.get(new_key) or {}).get("created_at", 0)
        old_at = (managed.get(existing) or {}).get("created_at", 0) if existing else -1
        if new_at >= old_at:
            index[key_uname] = new_key
            return True
        return False

    async def update_client_username(self, chat_id, username: str) -> bool:
        """Обновляет client_username в managed_chats[chat_id] и обратный индекс.
        Используется юзерботом при /sync_clients и при первом сообщении
        клиента в managed_chat (если username пустой/устарел)."""
        if not username:
            return False
        clean = username.lstrip("@").strip()
        if not clean:
            return False
        key = _norm_chat_id(chat_id)
        async with _lock:
            info = self.state["managed_chats"].get(key)
            if info is None:
                return False
            changed = False
            if (info.get("client_username") or "") != clean:
                info["client_username"] = clean
                changed = True
            if self._username_index_set_unlocked(clean, key):
                changed = True
            if changed:
                await self._save_unlocked()
            return changed

    def find_chat_by_client_username(self, username: str):
        """Возвращает chat_id (нормализованный ключ managed_chats) клиента
        по @username, либо None. Поиск через обратный индекс."""
        if not username:
            return None
        key_uname = username.lstrip("@").lower().strip()
        if not key_uname:
            return None
        index = self.state.get("client_username_index") or {}
        return index.get(key_uname)

    def get_client_username_index(self) -> dict:
        return dict(self.state.get("client_username_index") or {})

    async def register_chat(self, chat_id, client_id: int, client_name: str, client_username: str = ""):
        key = _norm_chat_id(chat_id)
        clean_uname = (client_username or "").lstrip("@").strip()
        async with _lock:
            self.state["managed_chats"][key] = {
                "client_id": client_id,
                "client_name": client_name,
                "client_username": clean_uname,
                "created_at": time.time(),
                "welcome_sent": False,
                # Метод оплаты + USDT адрес — заполняются AI через
                # tool set_payment_method, когда клиент сделал выбор.
                "payment_method": "",
                "usdt_address": "",
            }
            if clean_uname:
                self._username_index_set_unlocked(clean_uname, key)
            await self._save_unlocked()

    async def set_chat_payment_info(
        self, chat_id, method: str = "", usdt_address: str = "",
        client_username: str = "",
    ) -> bool:
        """Запоминает метод оплаты (USDT_TRC20/GUARANTOR), USDT-адрес и
        username клиента для managed-чата. Используется юзерботом в
        перевяз-форварде когда сделки в storage ещё нет."""
        key = _norm_chat_id(chat_id)
        async with _lock:
            info = self.state["managed_chats"].get(key)
            if info is None:
                return False
            if method:
                info["payment_method"] = (method or "").upper()
            if usdt_address:
                info["usdt_address"] = usdt_address.strip()
            if client_username:
                clean = client_username.lstrip("@").strip()
                info["client_username"] = clean
                if clean:
                    self._username_index_set_unlocked(clean, key)
            await self._save_unlocked()
            return True

    async def set_pending_perevyaz(self, chat_id, bank: str = "", fio: str = ""):
        """Сохраняем bank+fio когда перевязка пришла, а метод оплаты ещё не
        задан. Как только клиент назовёт метод — заберём pending и создадим
        карточку без повторного перевязного события."""
        key = _norm_chat_id(chat_id)
        async with _lock:
            info = self.state["managed_chats"].get(key)
            if info is None:
                return False
            info["pending_perevyaz"] = {
                "bank": (bank or "").strip(),
                "fio": (fio or "").strip(),
                "ts": time.time(),
            }
            await self._save_unlocked()
            return True

    def get_pending_perevyaz(self, chat_id) -> dict:
        info = self.state["managed_chats"].get(_norm_chat_id(chat_id)) or {}
        return dict(info.get("pending_perevyaz") or {})

    async def pop_pending_perevyaz(self, chat_id) -> dict:
        key = _norm_chat_id(chat_id)
        async with _lock:
            info = self.state["managed_chats"].get(key)
            if info is None:
                return {}
            pending = info.pop("pending_perevyaz", None) or {}
            if pending:
                await self._save_unlocked()
            return dict(pending)

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

    async def remove_managed_chat(self, chat_id) -> bool:
        """Удаляет чат из managed_chats — AI перестаёт там отвечать.
        Используется командой 'Ассистент забудь этот чат'."""
        key = _norm_chat_id(chat_id)
        async with _lock:
            managed = self.state.get("managed_chats") or {}
            if key not in managed:
                return False
            managed.pop(key, None)
            # Заодно почистим обратный индекс username, если он указывал сюда.
            idx = self.state.get("client_username_index") or {}
            for uname_key, mapped in list(idx.items()):
                if str(mapped) == str(key):
                    idx.pop(uname_key, None)
            await self._save_unlocked()
            return True

    async def cleanup(self):
        now = time.time()
        changed = False
        async with _lock:
            old_chats = [
                k for k, v in self.state["managed_chats"].items()
                if now - v.get("created_at", now) > _CHAT_TTL_SEC
            ]
            for k in old_chats:
                del self.state["managed_chats"][k]
            if old_chats:
                changed = True
                print(f"[storage] cleanup: removed {len(old_chats)} old managed_chats")

            cd_sec = self.state["cooldown_minutes"] * 60
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

    def get_stats(self) -> dict:
        return dict(self.state["stats"])

    def get_secret_command(self) -> str:
        return self.state["admin_secret_command"]

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

    async def register_source(self, user_id: int, source: str) -> bool:
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

    # === Бухгалтерия V2: Группа 2 «Бухгалтерия» (заявки v2) ===

    def get_accounting_group_id(self) -> int:
        return int(self.state.get("accounting_group_id") or 0)

    async def set_accounting_group_id(self, chat_id: int):
        async with _lock:
            self.state["accounting_group_id"] = int(chat_id)
            await self._save_unlocked()

    # === ЛК-карточки (Группа 1) ===

    def get_lk_group_id(self) -> int:
        return int(self.state.get("lk_group_id") or 0)

    async def set_lk_group_id(self, chat_id: int):
        async with _lock:
            self.state["lk_group_id"] = int(chat_id)
            await self._save_unlocked()

    def get_lk_card(self, card_id: str) -> Optional[dict]:
        return (self.state.get("lk_cards") or {}).get(str(card_id))

    def list_lk_cards(self, status: Optional[str] = None) -> dict:
        all_cards = self.state.get("lk_cards") or {}
        if status is None:
            return dict(all_cards)
        return {k: v for k, v in all_cards.items() if v.get("status") == status}

    def find_lk_card(
        self,
        bank: Optional[str] = None,
        fio: Optional[str] = None,
        supplier: Optional[str] = None,
        work_chat_id=None,
    ) -> list:
        """Поиск карточек по любому набору полей. AND-логика, case-insensitive
        substring для bank/fio/supplier."""
        out = []
        cards = self.state.get("lk_cards") or {}
        wc = _norm_chat_id(work_chat_id) if work_chat_id else None
        for cid, c in cards.items():
            if bank and bank.lower() not in (c.get("bank") or "").lower():
                continue
            if fio and fio.lower() not in (c.get("fio") or "").lower():
                continue
            if supplier:
                s = (c.get("supplier") or "").lstrip("@").lower()
                if supplier.lstrip("@").lower() not in s:
                    continue
            if wc and _norm_chat_id(c.get("work_chat_id") or 0) != wc:
                continue
            out.append({"card_id": cid, **c})
        return out

    # === Import summaries (массовые импорты ЛК) ===

    async def save_import_summary(
        self, msg_id: int, chat_id, html_text: str, card_ids: list,
    ):
        """Сохраняет HTML сводки массового импорта по msg_id — для дальнейшего
        зачёркивания строк удалённых карточек."""
        if not msg_id:
            return False
        key = str(int(msg_id))
        async with _lock:
            summaries = self.state.setdefault("import_summaries", {})
            summaries[key] = {
                "chat_id": _norm_chat_id(chat_id),
                "html": html_text or "",
                "card_ids": list(card_ids or []),
            }
            await self._save_unlocked()
            return True

    def get_import_summary(self, msg_id: int) -> dict:
        if not msg_id:
            return {}
        key = str(int(msg_id))
        return dict(self.state.get("import_summaries", {}).get(key) or {})

    async def update_import_summary_html(self, msg_id: int, new_html: str):
        """Обновляет сохранённый HTML сводки (после edit_message в Telegram)."""
        if not msg_id:
            return False
        key = str(int(msg_id))
        async with _lock:
            summaries = self.state.setdefault("import_summaries", {})
            entry = summaries.get(key)
            if entry is None:
                return False
            entry["html"] = new_html or ""
            await self._save_unlocked()
            return True

    # === Прайс ЛК ===
    # Единый источник цен. Меняется через команду «прайс БАНК ЦЕНА» в
    # брейн-чате. accounting2.lookup_pricing использует это в первую очередь.

    @staticmethod
    def _norm_bank_key(bank: str) -> str:
        return (bank or "").strip().upper()

    def get_pricing(self, bank: str):
        """Возвращает цену из storage или None если не задана."""
        if not bank:
            return None
        prices = self.state.get("pricing") or {}
        return prices.get(self._norm_bank_key(bank))

    def list_pricing(self) -> dict:
        """Копия всего прайса {BANK_UPPER: price_usdt}."""
        return dict(self.state.get("pricing") or {})

    async def set_pricing(self, bank: str, price: float) -> bool:
        if not bank:
            return False
        key = self._norm_bank_key(bank)
        if not key:
            return False
        try:
            price_f = float(price)
        except (TypeError, ValueError):
            return False
        async with _lock:
            prices = self.state.setdefault("pricing", {})
            prices[key] = price_f
            await self._save_unlocked()
            return True

    async def remove_pricing(self, bank: str) -> bool:
        if not bank:
            return False
        key = self._norm_bank_key(bank)
        async with _lock:
            prices = self.state.setdefault("pricing", {})
            if key not in prices:
                return False
            prices.pop(key, None)
            await self._save_unlocked()
            return True

    async def delete_lk_card(self, card_id: str) -> bool:
        """Удаляет одну карточку ЛК по card_id. Возвращает True если
        карточка существовала и удалена."""
        if not card_id:
            return False
        key = str(card_id).strip().lstrip("#")
        async with _lock:
            cards = self.state.get("lk_cards") or {}
            if key not in cards:
                return False
            cards.pop(key, None)
            await self._save_unlocked()
            return True

    async def delete_all_lk_cards(self) -> int:
        """Удаляет ВСЕ карточки ЛК. Возвращает количество удалённых.

        ⚠️ Деструктивная операция — вызывается только после двойного
        подтверждения (Тимон + админ) через команду «Ассистент удалить все ЛК»
        в Группе 1."""
        async with _lock:
            cards = self.state.get("lk_cards") or {}
            n = len(cards)
            self.state["lk_cards"] = {}
            self.state["lk_cards_seq"] = 0
            await self._save_unlocked()
            return n

    async def add_lk_card(self, **fields) -> str:
        """Создаёт новую карточку. Возвращает card_id ("lk001"...).

        Обязательные: bank, fio, price_usdt, payment_method.
        """
        async with _lock:
            seq = int(self.state.get("lk_cards_seq", 0)) + 1
            card_id = f"lk{seq:03d}"
            self.state["lk_cards_seq"] = seq
            cards = self.state.setdefault("lk_cards", {})
            base = {
                "card_id": card_id,
                "supplier": (fields.get("supplier") or "").lstrip("@"),
                "bank": fields.get("bank") or "",
                "fio": fields.get("fio") or "",
                "price_usdt": float(fields.get("price_usdt") or 0),
                "payment_method": fields.get("payment_method") or "",
                "deal_id": fields.get("deal_id") or "",
                "usdt_address": fields.get("usdt_address") or "",
                "status": fields.get("status") or "В_РАБОТЕ",
                "block_amount_rub": 0.0,
                "block_note": "",
                "brak_reason": "",
                "client_id": int(fields.get("client_id") or 0),
                "client_username": (fields.get("client_username") or "").lstrip("@"),
                "work_chat_id": fields.get("work_chat_id") or 0,
                "lk_group_msg_id": 0,
                "created_at": time.time(),
                "history": [{
                    "ts": time.time(),
                    "status": fields.get("status") or "В_РАБОТЕ",
                    "by": fields.get("created_by") or "system",
                }],
            }
            cards[card_id] = base
            await self._save_unlocked()
            return card_id

    async def update_lk_card(self, card_id: str, **fields) -> bool:
        async with _lock:
            cards = self.state.get("lk_cards") or {}
            c = cards.get(str(card_id))
            if c is None:
                return False
            for k, v in fields.items():
                if k == "history":
                    continue
                c[k] = v
            await self._save_unlocked()
            return True

    async def set_lk_card_status(
        self, card_id: str, status: str, **extra
    ) -> bool:
        """Меняет статус + добавляет запись в history. Дополнительные
        поля (block_amount_rub, block_note, brak_reason, deal_id) — в extra."""
        async with _lock:
            cards = self.state.get("lk_cards") or {}
            c = cards.get(str(card_id))
            if c is None:
                return False
            c["status"] = status
            for k, v in extra.items():
                c[k] = v
            c.setdefault("history", []).append({
                "ts": time.time(),
                "status": status,
                "by": extra.get("by") or "system",
                "extra": {k: v for k, v in extra.items() if k != "by"},
            })
            await self._save_unlocked()
            return True

    async def set_lk_card_msg_id(self, card_id: str, msg_id) -> bool:
        async with _lock:
            cards = self.state.get("lk_cards") or {}
            c = cards.get(str(card_id))
            if c is None:
                return False
            c["lk_group_msg_id"] = int(msg_id or 0)
            await self._save_unlocked()
            return True

    # === Заявки V2 (Группа 2 «Бухгалтерия») ===

    def get_applications_v2(self, date_str: str) -> list:
        return list((self.state.get("applications_v2") or {}).get(date_str) or [])

    def list_applications_v2_dates(self) -> list:
        return sorted((self.state.get("applications_v2") or {}).keys())

    async def add_application_v2(self, date_str: str, app_data: dict) -> int:
        """Добавляет заявку. Возвращает её sequence id (1, 2, ... в рамках дня)."""
        async with _lock:
            apps_by_date = self.state.setdefault("applications_v2", {})
            day = apps_by_date.setdefault(date_str, [])
            new_id = len(day) + 1
            entry = {**app_data, "id": new_id, "ts": time.time()}
            day.append(entry)
            await self._save_unlocked()
            return new_id

    async def update_application_v2(
        self, date_str: str, app_id: int, **fields
    ) -> bool:
        """Обновляет произвольные поля заявки на месте. Возвращает True если
        нашли и обновили."""
        async with _lock:
            apps_by_date = self.state.setdefault("applications_v2", {})
            day = apps_by_date.get(date_str) or []
            for app in day:
                if int(app.get("id", 0)) == int(app_id):
                    for k, v in fields.items():
                        app[k] = v
                    await self._save_unlocked()
                    return True
            return False

    async def remove_application_v2(self, date_str: str, app_id: int) -> bool:
        async with _lock:
            apps_by_date = self.state.setdefault("applications_v2", {})
            day = apps_by_date.get(date_str) or []
            for i, app in enumerate(day):
                if int(app.get("id", 0)) == int(app_id):
                    day.pop(i)
                    await self._save_unlocked()
                    return True
            return False

    # === Воронка конверсии (funnel_stats) ===
    # Каждый ключ — отдельный счётчик за день. Ключи:
    #   starts          — нажатий /start в боте
    #   chats_created   — создано work-чатов (бот пригласил клиента)
    #   chats_active    — work-чаты где клиент написал хоть что-то
    #   chats_junk      — work-чаты признанные мусором (ни о чём не спросили)
    #   ip_interest     — клиенты выразившие интерес открыть ИП
    #   bank_interest   — клиенты обсуждавшие конкретные банки
    #   rs_handed       — клиенты сдали РС (карточка ЛК создана)
    #   lk_done         — карточек ЛК переведено в ЗАВЕРШЁН
    #   blocks          — карточек в статусе БЛОК
    #   margin_usdt     — суммарная маржа за день (USDT)

    async def bump_funnel(self, key: str, value: float = 1.0, date_str: str = None):
        """Инкрементирует счётчик воронки на value (по умолчанию +1)."""
        if not key:
            return
        if date_str is None:
            date_str = time.strftime("%Y-%m-%d", time.localtime())
        async with _lock:
            f = self.state.setdefault("funnel_stats", {})
            day = f.setdefault(date_str, {})
            day[key] = float(day.get(key, 0)) + float(value)
            await self._save_unlocked()

    def get_funnel(self, date_str: str = None) -> dict:
        if date_str is None:
            date_str = time.strftime("%Y-%m-%d", time.localtime())
        f = self.state.get("funnel_stats") or {}
        return dict(f.get(date_str) or {})

    def get_funnel_range(self, days: int = 7) -> list:
        """Возвращает список словарей {date, ...counters} за N последних дней."""
        f = self.state.get("funnel_stats") or {}
        out = []
        for i in range(max(1, days)):
            d = time.strftime(
                "%Y-%m-%d", time.localtime(time.time() - i * 86400),
            )
            entry = dict(f.get(d) or {})
            entry["date"] = d
            out.append(entry)
        return out

    # === Стата по менеджерам/работникам ===

    async def bump_manager(self, username: str, key: str, value: float = 1.0):
        """Инкрементирует счётчик активности менеджера/работника."""
        uname = (username or "").lstrip("@").lower().strip()
        if not uname or not key:
            return
        async with _lock:
            ms = self.state.setdefault("manager_stats", {})
            entry = ms.setdefault(uname, {})
            entry[key] = float(entry.get(key, 0)) + float(value)
            entry["last_active_ts"] = time.time()
            await self._save_unlocked()

    def list_manager_stats(self) -> dict:
        """Полная статистика по всем работникам."""
        return dict(self.state.get("manager_stats") or {})

    def get_manager_stat(self, username: str) -> dict:
        """Стата конкретного менеджера."""
        uname = (username or "").lstrip("@").lower().strip()
        return dict((self.state.get("manager_stats") or {}).get(uname) or {})

    # === Предпочтения клиентов (память на уровне @username) ===

    def get_client_preferences(self, username: str) -> dict:
        """Вернуть прошлые предпочтения этого клиента (метод оплаты,
        USDT-адрес и т.д.). Пустой dict если клиент новый."""
        uname = (username or "").lstrip("@").lower().strip()
        if not uname:
            return {}
        prefs = self.state.get("client_preferences") or {}
        return dict(prefs.get(uname) or {})

    async def save_client_preferences(
        self, username: str, payment_method: str = "",
        usdt_address: str = "", fio: str = "", bank: str = "",
    ) -> bool:
        """Сохраняет/обновляет предпочтения клиента. Инкрементит lk_count
        и обновляет timestamp. Пустые поля не затирают существующие."""
        uname = (username or "").lstrip("@").lower().strip()
        if not uname:
            return False
        async with _lock:
            prefs = self.state.setdefault("client_preferences", {})
            entry = prefs.setdefault(uname, {})
            if payment_method:
                entry["payment_method"] = payment_method.upper()
            if usdt_address:
                entry["usdt_address"] = usdt_address.strip()
            if fio:
                entry["fio_last"] = fio.strip()
            if bank:
                entry["bank_last"] = bank.strip()
            entry["last_updated_ts"] = time.time()
            entry["lk_count"] = int(entry.get("lk_count", 0)) + 1
            await self._save_unlocked()
            return True


storage = Storage(config.STORAGE_PATH)
