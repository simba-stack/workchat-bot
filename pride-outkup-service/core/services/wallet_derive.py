"""HD-Wallet deterministic derivation for pride-p2p.

— Master key auto-generates на первом запуске (secrets.token_hex(32) = 256-bit entropy).
— Хранится в system_secrets (Postgres volume Railway = persistent).
— При создании — owner получает копию в Telegram личку (backup на случай DB-loss).
— Private keys НЕ хранятся в БД, только публичные адреса. Privkey derive on-demand.

derivation_path: HMAC-SHA256(master_key, f"user/{user_id}/{coin}/{network}") → 32 bytes → tron.PrivateKey

Из одного master_key + одних user_id всегда получаются одни и те же адреса.
Если БД user_deposit_addresses потеряется — адреса можно восстановить:
просто вызвать derive ещё раз с тем же master_key.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.models import SystemSecret

logger = logging.getLogger(__name__)

MASTER_KEY_NAME = "master_derivation_key_v1"


async def get_or_create_master_key(db: AsyncSession) -> bytes:
    """Возвращает master_key (32 bytes). Создаёт если нет.

    При первом создании — шлёт уведомление owner'у через Telegram.
    """
    res = await db.execute(
        select(SystemSecret).where(SystemSecret.key == MASTER_KEY_NAME)
    )
    row = res.scalar_one_or_none()
    if row:
        return bytes.fromhex(row.value)

    # Генерим новый
    key_hex = secrets.token_hex(32)  # 64 hex chars = 32 bytes = 256 bits
    row = SystemSecret(key=MASTER_KEY_NAME, value=key_hex, is_encrypted=False)
    db.add(row)
    await db.commit()
    logger.warning(
        "[wallet_derive] MASTER KEY GENERATED. Hash: %s. Saving to Postgres volume.",
        hashlib.sha256(key_hex.encode()).hexdigest()[:16],
    )

    # Backup notification — шлём ВСЕМ admin'ам в личку
    try:
        from bot.main import notify_user
        from core.config import settings as cfg

        msg = (
            "🔐 <b>PRIDE P2P · Master Wallet Key</b>\n\n"
            "Сервис только что сгенерировал новый master derivation key. "
            "Этот ключ используется для деривации tron-адресов всех пользователей. "
            "<b>СОХРАНИ его в безопасное место</b> (1Password, бумажная копия в сейфе) — "
            "если БД pride-p2p будет утеряна, без этого ключа доступ ко всем средствам "
            "пользователей будет невозможен.\n\n"
            f"<code>{key_hex}</code>\n\n"
            "После сохранения <b>удали это сообщение</b> и не делись им ни с кем."
        )
        for tg_id in cfg.admin_ids:
            try:
                await notify_user(tg_id, msg)
            except Exception as e:
                logger.warning("[wallet_derive] notify admin %s failed: %s", tg_id, e)
    except Exception as e:
        logger.warning("[wallet_derive] backup notify skipped: %s", e)

    return bytes.fromhex(key_hex)


def derive_tron_keypair(master_key: bytes, user_id: int) -> tuple[str, str]:
    """Возвращает (address, private_key_hex) для юзера.

    Детерминистично: master + user_id всегда дают один результат.
    """
    from tronpy.keys import PrivateKey

    salt = f"user/{user_id}/USDT/TRC20".encode()
    derivation = hmac.new(master_key, salt, hashlib.sha256).digest()
    priv = PrivateKey(derivation)
    addr = priv.public_key.to_base58check_address()
    return addr, priv.hex()


async def get_or_create_user_address(
    db: AsyncSession, user_id: int, coin: str = "USDT", network: str = "TRC20",
) -> tuple[str, int]:
    """Возвращает (address, derivation_index) для (user, coin, network).
    Создаёт в БД при первом запросе.
    """
    from core.models import UserDepositAddress

    coin = coin.upper()
    network = network.upper()

    # Сейчас поддерживаем только TRON (TRC20).
    if network not in ("TRC20", "TRX"):
        raise NotImplementedError(f"Деривация для сети {network} ещё не реализована")

    # Уже есть?
    res = await db.execute(
        select(UserDepositAddress).where(
            UserDepositAddress.user_id == user_id,
            UserDepositAddress.coin_code == coin,
            UserDepositAddress.network == network,
        )
    )
    row = res.scalar_one_or_none()
    if row:
        return row.address, row.derivation_index

    master_key = await get_or_create_master_key(db)
    address, _ = derive_tron_keypair(master_key, user_id)
    row = UserDepositAddress(
        user_id=user_id,
        coin_code=coin,
        network=network,
        address=address,
        derivation_index=user_id,
    )
    db.add(row)
    await db.commit()
    logger.info("[wallet_derive] new address user=%s %s/%s addr=%s",
                user_id, coin, network, address)
    return address, user_id


async def get_user_private_key(
    db: AsyncSession, user_id: int, network: str = "TRC20",
) -> str:
    """Возвращает hex privkey юзера для данной сети. Для sweep / recovery."""
    if network.upper() not in ("TRC20", "TRX"):
        raise NotImplementedError(network)
    master_key = await get_or_create_master_key(db)
    _, priv_hex = derive_tron_keypair(master_key, user_id)
    return priv_hex
