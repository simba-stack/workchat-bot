"""Один helper для URL Mini-App с cache-busting.

Каждый раз когда контейнер стартует (новый deploy), получает свой _APP_BOOT_VER —
этот версионный suffix добавляется к URL Mini-App, чтобы Telegram WebApp
форсированно перезабирал свежую версию HTML (а не отдавал кешированную).
"""
import time
from core.config import settings

# version-suffix = unix-time старта контейнера. Bump на каждом deploy.
_APP_BOOT_VER: str = str(int(time.time()))


def miniapp_link(view: str = "", **extra) -> str:
    """Возвращает URL Mini-App с v={timestamp} (cache buster) + optional view + extra params."""
    base = f"{settings.miniapp_url}{settings.miniapp_path}"
    parts = [f"v={_APP_BOOT_VER}"]
    if view:
        parts.append(f"view={view}")
    for k, v in (extra or {}).items():
        if v is not None:
            parts.append(f"{k}={v}")
    return f"{base}?{'&'.join(parts)}"
