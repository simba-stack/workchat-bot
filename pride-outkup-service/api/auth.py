"""Telegram WebApp auth — проверка initData через HMAC.

См. https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app
"""
import hashlib
import hmac
import json
import logging
from typing import Optional
from urllib.parse import parse_qsl

from fastapi import Depends, Header, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.config import settings
from core.db import get_db
from core.models import User

logger = logging.getLogger(__name__)


def verify_init_data(init_data: str, bot_token: str, max_age_sec: int = 604800) -> dict:
    """Проверяет подпись Telegram WebApp initData.

    Возвращает распарсенный user dict если валидно. Поднимает HTTPException 401 иначе.
    """
    try:
        data = dict(parse_qsl(init_data, keep_blank_values=True))
    except Exception:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid initData format")

    received_hash = data.pop("hash", None)
    if not received_hash:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "no hash in initData")

    # Build data-check-string
    data_check_string = "\n".join(
        f"{k}={v}" for k, v in sorted(data.items())
    )

    secret_key = hmac.new(
        b"WebAppData", bot_token.encode(), hashlib.sha256,
    ).digest()
    calc_hash = hmac.new(
        secret_key, data_check_string.encode(), hashlib.sha256,
    ).hexdigest()

    if not hmac.compare_digest(calc_hash, received_hash):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid initData hash")

    # Проверка времени (TTL)
    try:
        auth_date = int(data.get("auth_date", "0"))
        import time
        if abs(time.time() - auth_date) > max_age_sec:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "initData expired")
    except (TypeError, ValueError):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid auth_date")

    # Парсим user
    user_raw = data.get("user")
    if not user_raw:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "no user in initData")
    try:
        user = json.loads(user_raw)
    except json.JSONDecodeError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid user json")

    return user


async def get_current_user(
    request: Request,
    x_telegram_init_data: Optional[str] = Header(None),
    db: AsyncSession = Depends(get_db),
) -> User:
    """FastAPI dependency — текущий юзер из initData.

    Принимает initData либо в заголовке X-Telegram-InitData, либо в query ?init_data=...
    (некоторые прокси/Telegram-клиенты режут custom-headers — query надёжнее).
    """
    init_data = x_telegram_init_data or request.query_params.get("init_data") or ""
    if not init_data:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "X-Telegram-InitData header or ?init_data= query param required")

    tg_user = verify_init_data(init_data, settings.bot_token)
    tg_id = int(tg_user.get("id") or 0)
    if not tg_id:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "no user.id in initData")

    # Ищем юзера в БД
    res = await db.execute(select(User).where(User.tg_id == tg_id))
    user = res.scalar_one_or_none()

    if not user:
        user = User(
            tg_id=tg_id,
            username=tg_user.get("username"),
            full_name=" ".join(filter(None, [
                tg_user.get("first_name"),
                tg_user.get("last_name"),
            ])).strip() or None,
            language=tg_user.get("language_code", "ru")[:8],
        )
        db.add(user)
        await db.flush()
        logger.info("[auth] new user registered tg=%s @%s", tg_id, user.username)
    else:
        # Обновляем username если поменялся
        new_username = tg_user.get("username")
        if new_username and user.username != new_username:
            user.username = new_username

    return user


async def require_verified(user: User = Depends(get_current_user)) -> User:
    """Раньше требовал KYC verified — теперь KYC ОПЦИОНАЛЬНЫЙ.

    SIMBA решил: KYC не должен блокировать operations. Юзер может пройти KYC
    добровольно (получит badge ✓ verified в UI), но это не обязательно для
    withdraw/transfer/swap/p2p.

    Блокируем ТОЛЬКО banned юзеров (после явного бана админом).
    """
    if user.kyc_status == "banned":
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "Account banned. Свяжитесь с поддержкой.",
        )
    return user


async def require_admin(user: User = Depends(get_current_user)) -> User:
    """Требует роль админа PRIDE (по tg_id)."""
    if user.tg_id not in settings.admin_ids:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "admin only")
    return user
