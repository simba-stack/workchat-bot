"""Config для pride-operations backend."""
from functools import lru_cache
from typing import Optional

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # === Basic ===
    app_name: str = "PRIDE Operations"
    port: int = 8000

    # === Telegram Bot (для валидации Login Widget) ===
    # Используем токен любого бота PRIDE — например @PrideInviteWork_bot или @PRIDE_CRM_BOT
    # Login Widget подписывается bot_token'ом, мы валидируем HMAC-SHA256
    tg_bot_token: str = ""
    tg_bot_username: str = "PrideInviteWork_bot"

    # === JARVIS integration ===
    # Синхронизируем данные с JARVIS через HTTP
    jarvis_base_url: str = "https://workchat-bot-production.up.railway.app"
    jarvis_api_token: str = ""  # для internal API calls

    # === P2P integration ===
    p2p_base_url: str = "https://pride-p2p-production.up.railway.app"

    # === Auth (JWT) ===
    jwt_secret: str = "change-me-in-production"  # HS256 пока; RS256 в Sprint 2
    jwt_algorithm: str = "HS256"
    jwt_access_ttl_min: int = 15
    jwt_refresh_ttl_days: int = 30

    # === Database (Postgres) ===
    database_url: str = ""  # Railway DATABASE_URL

    # === Redis (для WebSocket pub/sub + session storage) ===
    redis_url: Optional[str] = None  # Railway REDIS_URL

    # === Admin ===
    admin_tg_ids: str = "8151738775"  # SIMBA user_id (комма-separated)

    # === Feature flags ===
    feature_cabinet_enabled: bool = True
    feature_admin_enabled: bool = False  # включим в Sprint 5+

    @property
    def admin_ids(self) -> list[int]:
        return [int(x) for x in self.admin_tg_ids.split(",") if x.strip().isdigit()]

    @property
    def database_url_async(self) -> str:
        url = self.database_url
        if url.startswith("postgres://"):
            url = url.replace("postgres://", "postgresql://", 1)
        if url.startswith("postgresql://") and "+asyncpg" not in url:
            url = url.replace("postgresql://", "postgresql+asyncpg://", 1)
        return url


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
