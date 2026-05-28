"""Async-safe persistent FSM storage for aiogram.

Design:
- File I/O всегда через asyncio.to_thread / run_in_executor — не блокирует event loop
- Debounced flush: каждые `flush_interval` сек, только если dirty
- Graceful shutdown через .close() — финальный flush + cancel background task
- Atomic write через .tmp + os.replace
- При повреждённом/отсутствующем файле — пустой state, не падает

Использование:
    from fsm_persistent import AsyncPersistentFSMStorage
    storage = AsyncPersistentFSMStorage("/app/data/crm_fsm.json", flush_interval=2.0)
    dp = Dispatcher(storage=storage, fsm_strategy=FSMStrategy.CHAT)
    # ...
    # При shutdown:
    await storage.close()
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any, Dict, Optional

from aiogram.fsm.storage.base import BaseStorage, StorageKey

logger = logging.getLogger(__name__)


class AsyncPersistentFSMStorage(BaseStorage):
    """In-memory FSM storage с async-safe disk persistence.

    Поведение по сравнению с MemoryStorage:
      • Все mutations (set_state/set_data) — синхронные в памяти (быстро, не блокирует).
      • После mutation помечается dirty=True.
      • Фоновая задача каждые flush_interval сек: если dirty — пишет на disk
        через executor (НЕ блокирует event loop).
      • При .close() — финальный flush + отмена фоновой задачи.
      • Read (get_state/get_data) — мгновенный, из памяти.
    """

    def __init__(self, path: str, flush_interval: float = 2.0):
        self.path = path
        self.flush_interval = max(0.5, float(flush_interval))
        self._data: dict = {}
        self._lock = asyncio.Lock()
        self._dirty = False
        self._flush_task: Optional[asyncio.Task] = None
        self._closed = False
        # Одноразовая sync-загрузка при __init__ — до start polling, event loop
        # ещё не критичен для блокировки. Файл обычно <10MB, грузится <100ms.
        try:
            if os.path.exists(path):
                with open(path, "r", encoding="utf-8") as f:
                    raw = f.read()
                if raw.strip():
                    self._data = json.loads(raw)
                    logger.info(
                        "AsyncPersistentFSM loaded %d entries from %s",
                        len(self._data), path,
                    )
        except Exception as e:
            logger.warning(
                "AsyncPersistentFSM init: failed to load %s: %s (starting empty)",
                path, e,
            )
            self._data = {}

    async def _ensure_flush_task(self):
        """Lazy-старт фоновой задачи. Вызывается из первого set_*."""
        if self._closed:
            return
        if self._flush_task is not None and not self._flush_task.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return  # no running loop; будет запущен при следующем set_*
        self._flush_task = loop.create_task(self._flush_loop())

    async def _flush_loop(self):
        """Фоновая задача: каждые flush_interval сек, если dirty → пишем на disk."""
        try:
            while not self._closed:
                await asyncio.sleep(self.flush_interval)
                if self._dirty and not self._closed:
                    try:
                        await self._flush()
                    except Exception as e:
                        logger.warning("AsyncPersistentFSM flush loop error: %s", e)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error("AsyncPersistentFSM flush loop crashed: %s", e)

    async def _flush(self):
        """Снимок данных + offload-write в executor (НЕ блокирует event loop)."""
        async with self._lock:
            if not self._dirty:
                return
            # Глубокая копия чтобы не было гонки при последующих mutations
            snapshot = json.dumps(self._data, ensure_ascii=False)
            self._dirty = False
        try:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, self._do_write_sync, snapshot)
        except Exception as e:
            logger.warning("AsyncPersistentFSM write failed: %s", e)
            async with self._lock:
                self._dirty = True  # retry на следующем tick

    def _do_write_sync(self, snapshot_str: str):
        """Sync atomic write. Выполняется в thread executor."""
        d = os.path.dirname(self.path)
        if d and not os.path.isdir(d):
            os.makedirs(d, exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(snapshot_str)
        os.replace(tmp, self.path)

    def _key(self, key: StorageKey) -> str:
        # Используем bot_id + chat_id + user_id. Игнорируем thread_id и
        # business_connection_id чтобы не плодить ключи (FSMStrategy.CHAT
        # уже шарит state в чате, но user_id всё равно учитываем как fallback).
        return f"{key.bot_id}:{key.chat_id}:{key.user_id or 0}"

    async def set_state(self, key: StorageKey, state=None) -> None:
        await self._ensure_flush_task()
        async with self._lock:
            k = self._key(key)
            if k not in self._data:
                self._data[k] = {"state": None, "data": {}}
            # state может быть None, str, или State-объектом (aiogram)
            self._data[k]["state"] = (
                state.state if hasattr(state, "state") else state
            )
            self._dirty = True

    async def get_state(self, key: StorageKey) -> Optional[str]:
        async with self._lock:
            entry = self._data.get(self._key(key))
            return (entry or {}).get("state")

    async def set_data(self, key: StorageKey, data: Dict[str, Any]) -> None:
        await self._ensure_flush_task()
        async with self._lock:
            k = self._key(key)
            if k not in self._data:
                self._data[k] = {"state": None, "data": {}}
            # Делаем копию данных, чтобы вызыватель не мог изменить наш storage
            self._data[k]["data"] = dict(data) if data else {}
            self._dirty = True

    async def get_data(self, key: StorageKey) -> Dict[str, Any]:
        async with self._lock:
            entry = self._data.get(self._key(key))
            return dict((entry or {}).get("data") or {})

    async def close(self) -> None:
        """Graceful shutdown — финальный flush + отмена фоновой задачи."""
        if self._closed:
            return
        self._closed = True
        # Финальный flush, даже если dirty=False (на всякий)
        try:
            if self._dirty:
                await self._flush()
        except Exception as e:
            logger.warning("AsyncPersistentFSM final flush failed: %s", e)
        # Отменяем фоновую задачу
        if self._flush_task and not self._flush_task.done():
            self._flush_task.cancel()
            try:
                await self._flush_task
            except (asyncio.CancelledError, Exception):
                pass
