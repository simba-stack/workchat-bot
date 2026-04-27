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


def _gen_secret_command() -> str:
    alphabet = string.ascii_letters + string.digits
    suffix = ''.join(secrets.choice(alphabet) for _ in range(12))
    return f"admin_{suffix}"


def _norm_chat_id(cid) -> str:
    """Normalize chat_id to consistent string key (strips -100 prefix)."""
    s = str(cid).lstrip('-')
    if s.startswith('100'):
        s = s[3:]
    return s


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


storage = Storage(config.STORAGE_PATH)
