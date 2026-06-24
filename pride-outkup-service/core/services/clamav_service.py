"""ClamAV antivirus service.

Реальная интеграция с демоном ClamAV (clamd) для проверки вложений P2P-чата.
Заменяет stub-имитацию в p2p/workers/virus_scanner.py.

Подключение:
  - UNIX-сокет: CLAMAV_SOCKET (например /var/run/clamav/clamd.ctl) — приоритетнее
  - TCP-сокет:  CLAMAV_HOST + CLAMAV_PORT (default 127.0.0.1:3310)

Скан INSTREAM: байты файла передаются в демон (работает даже когда демон в
другом контейнере и не видит файловую систему приложения).

Поведение fail-closed:
  - Библиотека `clamd` или демон недоступны → ClamAVUnavailable.
    Воркер НЕ помечает файл CLEAN, а оставляет PENDING для повторной попытки.
  - Найден вирус → ClamAVResult(infected=True, signature=...).

ENV (см. core/config.py):
  clamav_enabled, clamav_host, clamav_port, clamav_socket, clamav_timeout
"""
from __future__ import annotations

import asyncio
import io
import logging
from dataclasses import dataclass

from core.config import settings

logger = logging.getLogger("p2p.service.clamav")

try:
    import clamd  # type: ignore
    _CLAMD_IMPORT_OK = True
except Exception:  # pragma: no cover - library may be absent in some envs
    clamd = None  # type: ignore
    _CLAMD_IMPORT_OK = False


class ClamAVUnavailable(RuntimeError):
    """Демон ClamAV недоступен / библиотека не установлена / таймаут."""


@dataclass(frozen=True)
class ClamAVResult:
    infected: bool
    signature: str | None = None  # имя сигнатуры если infected

    @property
    def status(self) -> str:
        return "INFECTED" if self.infected else "CLEAN"


def _build_client():
    """Создать синхронный clamd-клиент по настройкам. Бросает ClamAVUnavailable."""
    if not _CLAMD_IMPORT_OK:
        raise ClamAVUnavailable("python `clamd` library not installed")
    socket_path = (settings.clamav_socket or "").strip()
    try:
        if socket_path:
            return clamd.ClamdUnixSocket(path=socket_path, timeout=settings.clamav_timeout)
        return clamd.ClamdNetworkSocket(
            host=settings.clamav_host,
            port=int(settings.clamav_port),
            timeout=settings.clamav_timeout,
        )
    except Exception as e:  # pragma: no cover
        raise ClamAVUnavailable(f"cannot build clamd client: {e}") from e


def _scan_bytes_sync(data: bytes) -> ClamAVResult:
    """Синхронный INSTREAM-скан. Выполняется в thread-pool из async-обёртки."""
    client = _build_client()
    try:
        # ping подтверждает что демон живой до отправки потока
        client.ping()
        resp = client.instream(io.BytesIO(data))
    except ClamAVUnavailable:
        raise
    except Exception as e:
        raise ClamAVUnavailable(f"clamd scan failed: {e}") from e

    # resp == {'stream': ('OK', None)} | {'stream': ('FOUND', 'Eicar-Test-Signature')}
    verdict = (resp or {}).get("stream")
    if not verdict:
        raise ClamAVUnavailable(f"unexpected clamd response: {resp!r}")
    state, signature = verdict[0], (verdict[1] if len(verdict) > 1 else None)
    if state == "FOUND":
        return ClamAVResult(infected=True, signature=signature)
    if state == "OK":
        return ClamAVResult(infected=False)
    # ERROR и прочее — считаем недоступностью, файл останется PENDING
    raise ClamAVUnavailable(f"clamd verdict ERROR: {verdict!r}")


async def scan_bytes(data: bytes) -> ClamAVResult:
    """Проскать байты файла. Бросает ClamAVUnavailable если демон недоступен."""
    return await asyncio.to_thread(_scan_bytes_sync, data)


def _ping_sync() -> bool:
    try:
        return _build_client().ping() == "PONG"
    except Exception:
        return False


async def ping() -> bool:
    """Health-check демона ClamAV. Не бросает — возвращает bool."""
    if not settings.clamav_enabled:
        return False
    return await asyncio.to_thread(_ping_sync)
