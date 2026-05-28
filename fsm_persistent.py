"""Async-safe persistent FSM storage for aiogram. Smoke-tested локально."""
from __future__ import annotations
import asyncio, json, logging, os
from typing import Any, Dict, Optional
from aiogram.fsm.storage.base import BaseStorage, StorageKey

logger = logging.getLogger(__name__)


class AsyncPersistentFSMStorage(BaseStorage):
    def __init__(self, path: str, flush_interval: float = 2.0):
        self.path = path
        self.flush_interval = max(0.5, float(flush_interval))
        self._data: dict = {}
        self._lock = asyncio.Lock()
        self._dirty = False
        self._flush_task: Optional[asyncio.Task] = None
        self._closed = False
        try:
            if os.path.exists(path):
                with open(path, "r", encoding="utf-8") as f:
                    raw = f.read()
                if raw.strip():
                    self._data = json.loads(raw)
                    logger.info("AsyncPersistentFSM loaded %d entries from %s", len(self._data), path)
        except Exception as e:
            logger.warning("AsyncPersistentFSM init load failed: %s (starting empty)", e)
            self._data = {}

    async def _ensure_flush_task(self):
        if self._closed:
            return
        if self._flush_task is not None and not self._flush_task.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._flush_task = loop.create_task(self._flush_loop())

    async def _flush_loop(self):
        try:
            while not self._closed:
                await asyncio.sleep(self.flush_interval)
                if self._dirty and not self._closed:
                    try:
                        await self._flush()
                    except Exception as e:
                        logger.warning("flush loop error: %s", e)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error("flush loop crashed: %s", e)

    async def _flush(self):
        async with self._lock:
            if not self._dirty:
                return
            snapshot = json.dumps(self._data, ensure_ascii=False)
            self._dirty = False
        try:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, self._do_write_sync, snapshot)
        except Exception as e:
            logger.warning("AsyncPersistentFSM write failed: %s", e)
            async with self._lock:
                self._dirty = True

    def _do_write_sync(self, snapshot_str: str):
        d = os.path.dirname(self.path)
        if d and not os.path.isdir(d):
            os.makedirs(d, exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(snapshot_str)
        os.replace(tmp, self.path)

    def _key(self, key: StorageKey) -> str:
        return f"{key.bot_id}:{key.chat_id}:{key.user_id or 0}"

    async def set_state(self, key: StorageKey, state=None) -> None:
        await self._ensure_flush_task()
        async with self._lock:
            k = self._key(key)
            if k not in self._data:
                self._data[k] = {"state": None, "data": {}}
            self._data[k]["state"] = state.state if hasattr(state, "state") else state
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
            self._data[k]["data"] = dict(data) if data else {}
            self._dirty = True

    async def get_data(self, key: StorageKey) -> Dict[str, Any]:
        async with self._lock:
            entry = self._data.get(self._key(key))
            return dict((entry or {}).get("data") or {})

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            if self._dirty:
                await self._flush()
        except Exception as e:
            logger.warning("final flush failed: %s", e)
        if self._flush_task and not self._flush_task.done():
            self._flush_task.cancel()
            try:
                await self._flush_task
            except Exception:
                pass
