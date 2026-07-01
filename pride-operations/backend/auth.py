"""Auth: Telegram Login Widget verify + JWT."""
import hashlib
import hmac
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import Depends, HTTPException, Request, status
from jose import JWTError, jwt
from pydantic import BaseModel

from config import settings


class TgLoginPayload(BaseModel):
    """Payload от Telegram Login Widget (https://core.telegram.org/widgets/login)."""
    id: int
    first_name: str
    last_name: Optional[str] = None
    username: Optional[str] = None
    photo_url: Optional[str] = None
    auth_date: int
    hash: str


class UserSession(BaseModel):
    """Сессия юзера — что кладём в JWT payload."""
    user_id: int
    username: Optional[str] = None
    first_name: str
    role: str  # 'client' | 'operator' | 'admin' | 'owner'
    issued_at: int


def verify_telegram_login(payload: TgLoginPayload) -> bool:
    """Проверяет HMAC-SHA256 подпись Login Widget.

    Алгоритм из https://core.telegram.org/widgets/login#checking-authorization:
    1. Собираем data_check_string из всех полей КРОМЕ hash, отсортированных по ключу, в формате "key=value\n"
    2. Секретный ключ = SHA-256(bot_token)
    3. hmac_hash = HMAC-SHA256(secret_key, data_check_string)
    4. Сравниваем hmac_hash == payload.hash
    """
    if not settings.tg_bot_token:
        # Dev режим — если токен не задан, авторизацию не проверяем (только для локали!)
        # В проде токен обязан быть.
        return True

    # auth_date не старше 24 часов
    now = int(time.time())
    if now - payload.auth_date > 86400:
        return False

    # Собираем data_check_string
    data = payload.model_dump(exclude={"hash"}, exclude_none=True)
    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(data.items()))

    # Secret key
    secret_key = hashlib.sha256(settings.tg_bot_token.encode()).digest()

    # HMAC
    expected_hash = hmac.new(
        secret_key,
        data_check_string.encode(),
        hashlib.sha256,
    ).hexdigest()

    return hmac.compare_digest(expected_hash, payload.hash)


def determine_role(user_id: int) -> str:
    """Определяет роль юзера по его TG user_id.

    В Sprint 1 — простая логика: SIMBA + перечисленные в admin_tg_ids = owner/admin,
    остальные = client. В Sprint 5+ будет запрос к JARVIS storage для operator role.
    """
    if user_id in settings.admin_ids:
        return "owner"
    # TODO Sprint 5: запрос к JARVIS /api/me для проверки operator/admin роли
    return "client"


def create_access_token(user: UserSession) -> str:
    """Создаёт JWT access token."""
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(user.user_id),
        "username": user.username,
        "first_name": user.first_name,
        "role": user.role,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=settings.jwt_access_ttl_min)).timestamp()),
        "type": "access",
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def create_refresh_token(user_id: int) -> str:
    """Создаёт refresh token (longer TTL)."""
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(user_id),
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(days=settings.jwt_refresh_ttl_days)).timestamp()),
        "type": "refresh",
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def decode_token(token: str) -> dict:
    """Декодирует JWT. Raises JWTError на невалидный/истёкший."""
    return jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])


async def get_current_user(request: Request) -> UserSession:
    """FastAPI dependency: извлекает user из JWT в HttpOnly cookie ИЛИ Authorization header."""
    # 1. Приоритет — cookie (production)
    token = request.cookies.get("access_token")
    # 2. Fallback — Authorization: Bearer <token> (для API clients / mobile)
    if not token:
        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            token = auth_header.split(" ", 1)[1]

    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
        )

    try:
        payload = decode_token(token)
    except JWTError as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Invalid token: {e}",
        )

    if payload.get("type") != "access":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Wrong token type",
        )

    return UserSession(
        user_id=int(payload["sub"]),
        username=payload.get("username"),
        first_name=payload.get("first_name", ""),
        role=payload.get("role", "client"),
        issued_at=payload.get("iat", 0),
    )


async def require_role(*allowed_roles: str):
    """Dependency-factory: `Depends(require_role('owner', 'admin'))`."""
    async def check(user: UserSession = Depends(get_current_user)) -> UserSession:
        if user.role not in allowed_roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Role '{user.role}' not allowed (need one of {allowed_roles})",
            )
        return user
    return check
