"""Конфиг — env → settings (pydantic-settings)."""
from functools import lru_cache
from typing import Optional

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Bot
    bot_token: str
    bot_username: str = "PrideP2P_bot"

    # Mini-App
    miniapp_url: str = "https://pride-outkup-service-production.up.railway.app"
    miniapp_path: str = "/app2"  # was "/app" — bumped to force Telegram WebApp cache invalidation

    # Database
    database_url: str

    # Redis
    redis_url: Optional[str] = None

    # JARVIS sync
    jarvis_base_url: str = "https://workchat-bot-production.up.railway.app"
    jarvis_webhook_path: str = "/api/webhook/outkup"
    jarvis_hmac_secret: str = ""
    jarvis_api_token: str = ""

    # TRON
    tron_private_key: str = ""
    tron_hot_wallet_address: str = ""
    trongrid_api_key: str = ""
    tron_network: str = "mainnet"

    # Admin
    admin_tg_ids: str = "8151738775"

    # Limits
    max_concurrent_deals_per_user: int = 5
    kyc_lvl1_daily_limit_usdt: int = 500
    kyc_lvl2_daily_limit_usdt: int = 5000
    kyc_lvl3_daily_limit_usdt: int = 50000

    # Feature flags
    feature_v2_p2p_enabled: bool = False
    feature_referral_enabled: bool = True

    @property
    def admin_ids(self) -> list[int]:
        return [int(x) for x in self.admin_tg_ids.split(",") if x.strip().isdigit()]

    @property
    def database_url_async(self) -> str:
        """Railway даёт DATABASE_URL=postgresql:// — переводим в asyncpg."""
        url = self.database_url
        if url.startswith("postgres://"):
            url = url.replace("postgres://", "postgresql://", 1)
        if url.startswith("postgresql://") and "+asyncpg" not in url:
            url = url.replace("postgresql://", "postgresql+asyncpg://", 1)
        return url

    @property
    def jarvis_webhook_url(self) -> str:
        return f"{self.jarvis_base_url.rstrip('/')}{self.jarvis_webhook_path}"


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
