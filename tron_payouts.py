"""USDT TRC20 авто-выплаты через Tron (mainnet).

Архитектура (поддерживает 2 способа авторизации):

  ВАРИАНТ A — Прямой приватный ключ:
  • TRON_PRIVATE_KEY (env, 64 hex) — приватник hot-wallet'а
  • TRON_HOT_WALLET_ADDRESS (env) — публичный адрес этого кошелька

  ВАРИАНТ B — BIP39 мнемоника (SafePal-style):
  • TRON_MNEMONIC (env) — 12 или 24 слова через пробел
  • TRON_DERIVATION_PATH (env, опц.) — default "m/44'/195'/0'/0/0" (стандарт TRX)
  • TRON_HOT_WALLET_ADDRESS (env, опц.) — для cross-check;
        если не задан — derive из мнемоники

  Если заданы ОБА — приоритет у TRON_PRIVATE_KEY.

  • TRON_OWNER_TG_ID (env) — кому шлём уведомления о каждой выплате
  • TRONGRID_API_KEY (env, опционально) — для повышенного rate-limit

Безопасность:
  • Hot wallet НЕ держит большие суммы — только operational balance
  • При каждой выплате — TG уведомление owner-у с tx_hash
  • Логирование ВСЕХ исходящих в state.tron_outbound_log
  • Выплаты ≥ tfa_threshold_usdt требуют 2FA-код от @PrideGuard_bot

Использование:
    from tron_payouts import send_usdt_to
    result = await send_usdt_to(
        to_address="TR7NHqj...",
        amount_usdt=100.5,
        reason="salary @vasya",
    )
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Optional, Dict, Any

logger = logging.getLogger(__name__)

# USDT TRC20 contract на mainnet
USDT_TRC20_CONTRACT = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"
USDT_DECIMALS = 6


def _get_env_int(name: str, default: int = 0) -> int:
    try:
        return int(os.environ.get(name, str(default)) or default)
    except Exception:
        return default


def _derive_from_mnemonic(mnemonic: str, path: str = "") -> tuple:
    """BIP39 → seed → BIP32 (SLIP-44 TRX coin) → (privkey_hex, tron_address).

    Default path: m/44'/195'/0'/0/0 (стандарт для TRX).
    SafePal использует именно его.

    Возвращает (priv_hex, address) или ("", "") если что-то пошло не так.
    Зависимости: mnemonic, bip_utils.
    """
    mnemonic = (mnemonic or "").strip()
    if not mnemonic:
        return "", ""
    try:
        from mnemonic import Mnemonic
        from bip_utils import (
            Bip39SeedGenerator, Bip44, Bip44Coins, Bip44Changes,
        )
    except ImportError as e:
        logger.error(
            "BIP39 libraries не установлены: %s. Добавь "
            "'mnemonic>=0.20' и 'bip_utils>=2.7' в requirements.txt", e,
        )
        return "", ""
    try:
        # 1) Валидация мнемоники — проверяем во всех wordlists (BIP39 поддерживает
        # 9 языков). SafePal обычно даёт английскую.
        valid_lang = None
        for lang in ("english", "japanese", "korean", "spanish", "chinese_simplified",
                     "chinese_traditional", "french", "italian", "czech", "portuguese"):
            try:
                if Mnemonic(lang).check(mnemonic):
                    valid_lang = lang
                    break
            except Exception:
                continue
        if not valid_lang:
            # Найдём слово которого нет в английском wordlist и предложим
            # ближайшее по Levenshtein — для подсказки SIMBA без раскрытия мнемоники.
            words = mnemonic.split()
            mn_en = Mnemonic("english")
            wordlist = set(mn_en.wordlist)
            bad_positions = []
            suggestions = []
            for i, w in enumerate(words):
                if w.lower() not in wordlist:
                    bad_positions.append(i + 1)
                    # Levenshtein distance — найдём 3 ближайших
                    def _lev(a, b):
                        if a == b: return 0
                        if not a: return len(b)
                        if not b: return len(a)
                        m = [[0]*(len(b)+1) for _ in range(len(a)+1)]
                        for x in range(len(a)+1): m[x][0] = x
                        for y in range(len(b)+1): m[0][y] = y
                        for x in range(1, len(a)+1):
                            for y in range(1, len(b)+1):
                                c = 0 if a[x-1] == b[y-1] else 1
                                m[x][y] = min(m[x-1][y]+1, m[x][y-1]+1, m[x-1][y-1]+c)
                        return m[len(a)][len(b)]
                    near = sorted(wordlist, key=lambda x: _lev(w.lower(), x))[:3]
                    suggestions.append({"pos": i+1, "len": len(w), "first_char": w[:1], "hint": near})
            logger.error(
                "[tron] mnemonic checksum failed. bad_positions=%s",
                bad_positions,
            )
            if bad_positions:
                _DERIVED_CACHE["error"] = (
                    f"Слов(а) не из BIP39 wordlist: позиции {bad_positions}. "
                    f"См. derive_suggestions для подсказок."
                )
                _DERIVED_CACHE["suggestions"] = suggestions
            else:
                # Все слова валидны но checksum не сходится — значит порядок неверный
                # или одно слово заменено на другое из wordlist (опечатка типа brand→broom)
                _DERIVED_CACHE["error"] = (
                    "Все 12 слов есть в wordlist, но checksum не сходится. "
                    "Скорее всего одно слово заменено на ДРУГОЕ из BIP39 (типа 'broom'→'brand' — оба валидны). "
                    "Перепроверь порядок и каждое слово по списку https://github.com/bitcoin/bips/blob/master/bip-0039/english.txt"
                )
            return "", ""
        if valid_lang != "english":
            logger.info("[tron] mnemonic language detected: %s", valid_lang)
        # 2) Mnemonic → seed (Bip39SeedGenerator language-agnostic)
        seed = Bip39SeedGenerator(mnemonic).Generate()
        # 3) BIP44 derivation для TRX
        bip44_mst = Bip44.FromSeed(seed, Bip44Coins.TRON)
        # Стандартный путь m/44'/195'/0'/0/0
        derived = (
            bip44_mst
            .Purpose()
            .Coin()
            .Account(0)
            .Change(Bip44Changes.CHAIN_EXT)
            .AddressIndex(0)
        )
        priv_hex = derived.PrivateKey().Raw().ToHex()
        # tron-адрес из публичного ключа
        address = derived.PublicKey().ToAddress()
        return priv_hex, address
    except Exception as e:
        logger.exception("[tron] derive from mnemonic failed: %s", e)
        return "", ""


# Кэш — derive один раз при первом запросе чтобы не считать каждый раз.
_DERIVED_CACHE = {"priv": "", "address": "", "ts": 0.0, "error": "", "info": ""}


def _ensure_derived():
    """Если задан TRON_MNEMONIC — derive один раз и закэшируй."""
    global _DERIVED_CACHE
    if _DERIVED_CACHE["priv"]:
        return
    mn = (os.environ.get("TRON_MNEMONIC") or "").strip()
    if not mn:
        _DERIVED_CACHE["error"] = "TRON_MNEMONIC env not set"
        return
    # Нормализуем: схлопываем множественные пробелы в один
    words = mn.split()
    word_count = len(words)
    _DERIVED_CACHE["info"] = f"word_count={word_count}, lengths={[len(w) for w in words[:5]]}…"
    mn_normalized = " ".join(words)
    path = (os.environ.get("TRON_DERIVATION_PATH") or "").strip()
    try:
        priv, addr = _derive_from_mnemonic(mn_normalized, path)
    except Exception as e:
        _DERIVED_CACHE["error"] = f"derive raised: {type(e).__name__}: {e}"
        return
    if priv:
        _DERIVED_CACHE["priv"] = priv
        _DERIVED_CACHE["address"] = addr
        _DERIVED_CACHE["ts"] = time.time()
        _DERIVED_CACHE["error"] = ""
        logger.info("[tron] hot wallet derived from mnemonic, address=%s", addr)
    else:
        _DERIVED_CACHE["error"] = "derive returned empty — см. логи Railway (likely BIP39 checksum failed)"


def get_private_key() -> str:
    """Возвращает приватный ключ (64-hex). НИКОГДА не логируем!

    Приоритет: TRON_PRIVATE_KEY (если задан) → TRON_MNEMONIC (derive)."""
    direct = (os.environ.get("TRON_PRIVATE_KEY") or "").strip()
    if direct:
        return direct
    _ensure_derived()
    return _DERIVED_CACHE.get("priv") or ""


def get_hot_wallet_address() -> str:
    """Приоритет: TRON_HOT_WALLET_ADDRESS env → derive из мнемоники."""
    direct = (os.environ.get("TRON_HOT_WALLET_ADDRESS") or "").strip()
    if direct:
        return direct
    _ensure_derived()
    return _DERIVED_CACHE.get("address") or ""


def get_trongrid_key() -> str:
    return (os.environ.get("TRONGRID_API_KEY") or "").strip()


def get_owner_tg_id() -> int:
    return _get_env_int("TRON_OWNER_TG_ID", 0)


def is_configured() -> bool:
    """True если все необходимые env vars установлены (любым способом)."""
    return bool(get_private_key() and get_hot_wallet_address())


def validate_tron_address(address: str) -> bool:
    """Валидация Tron-адреса (base58 'T...' длина 34)."""
    if not address or not isinstance(address, str):
        return False
    address = address.strip()
    if not address.startswith("T"):
        return False
    if len(address) != 34:
        return False
    # Дополнительно — base58 char check
    import string
    valid = set(string.digits + string.ascii_letters) - {"0", "O", "I", "l"}
    return all(ch in valid for ch in address)


# === Lazy import tronpy чтобы не падать при отсутствии библиотеки ===
def _import_tronpy():
    """Возвращает (Tron, PrivateKey) или (None, None) если tronpy не установлен."""
    try:
        from tronpy import Tron
        from tronpy.keys import PrivateKey
        from tronpy.providers import HTTPProvider
        return Tron, PrivateKey, HTTPProvider
    except ImportError:
        logger.error(
            "tronpy не установлен — добавь 'tronpy>=0.4' в requirements.txt"
        )
        return None, None, None


def _get_client():
    """Создаёт Tron client с API-key если есть."""
    Tron, _, HTTPProvider = _import_tronpy()
    if Tron is None:
        return None
    api_key = get_trongrid_key()
    if api_key:
        provider = HTTPProvider(api_key=api_key)
        return Tron(provider=provider)
    return Tron()  # без ключа — публичный rate-limit


async def get_hot_wallet_balance() -> Dict[str, float]:
    """Возвращает {trx: float, usdt: float} hot-wallet'а.
    TRX нужен для оплаты network fee (~1-2 TRX за USDT transfer)."""
    if not is_configured():
        return {"trx": 0.0, "usdt": 0.0, "error": "not configured"}
    address = get_hot_wallet_address()
    try:
        client = _get_client()
        if client is None:
            return {"trx": 0.0, "usdt": 0.0, "error": "tronpy not installed"}
        # TRX
        trx_sun = await asyncio.to_thread(client.get_account_balance, address)
        # USDT TRC20
        contract = await asyncio.to_thread(client.get_contract, USDT_TRC20_CONTRACT)
        usdt_raw = await asyncio.to_thread(
            lambda: contract.functions.balanceOf(address)
        )
        usdt = usdt_raw / (10 ** USDT_DECIMALS)
        return {"trx": float(trx_sun), "usdt": float(usdt)}
    except Exception as e:
        logger.warning("get_hot_wallet_balance failed: %s", e)
        return {"trx": 0.0, "usdt": 0.0, "error": str(e)}


async def send_usdt_to(
    to_address: str,
    amount_usdt: float,
    reason: str = "",
    wait_confirmation: bool = True,
    timeout_sec: int = 90,
) -> Dict[str, Any]:
    """Отправляет USDT TRC20 на адрес.

    Args:
      to_address: целевой Tron-адрес (T...)
      amount_usdt: сумма USDT (decimal, например 100.5)
      reason: метка для лога (например "salary @vasya")
      wait_confirmation: ждать ли подтверждения в сети
      timeout_sec: макс время ожидания подтверждения

    Returns:
      {ok: bool, tx_hash: str, confirmed: bool, error: str?, balance_after: dict?}
    """
    # === Валидация ===
    if not is_configured():
        return {"ok": False, "error": "TRON_PRIVATE_KEY / TRON_HOT_WALLET_ADDRESS не заданы"}
    if not validate_tron_address(to_address):
        return {"ok": False, "error": f"Невалидный Tron-адрес: {to_address}"}
    if amount_usdt <= 0:
        return {"ok": False, "error": "Сумма должна быть > 0"}
    if amount_usdt > 10000:
        # Safety — не более 10k за раз. Можно увеличить если надо.
        return {"ok": False, "error": "Сумма >10000 USDT — safety guard"}

    Tron, PrivateKey, HTTPProvider = _import_tronpy()
    if Tron is None:
        return {"ok": False, "error": "tronpy не установлен (pip install tronpy)"}

    # === Проверка баланса ===
    balances = await get_hot_wallet_balance()
    if balances.get("usdt", 0) < amount_usdt:
        return {
            "ok": False,
            "error": f"Недостаточно USDT на hot-wallet'е: {balances.get('usdt', 0):.2f} < {amount_usdt}",
        }
    if balances.get("trx", 0) < 5:
        # Хотя бы 5 TRX для network fee
        return {
            "ok": False,
            "error": f"Недостаточно TRX для network fee: {balances.get('trx', 0):.2f} < 5",
        }

    # === Отправка ===
    try:
        priv = get_private_key()
        from_addr = get_hot_wallet_address()
        client = _get_client()
        priv_key = PrivateKey(bytes.fromhex(priv))

        # Контракт + raw amount
        contract = await asyncio.to_thread(client.get_contract, USDT_TRC20_CONTRACT)
        raw_amount = int(round(amount_usdt * (10 ** USDT_DECIMALS)))

        # Билдим транзакцию
        def _build_and_sign():
            txn = (
                contract.functions.transfer(to_address, raw_amount)
                .with_owner(from_addr)
                .fee_limit(20_000_000)  # 20 TRX fee limit (с запасом)
                .build()
                .sign(priv_key)
            )
            return txn

        txn = await asyncio.to_thread(_build_and_sign)
        result = await asyncio.to_thread(txn.broadcast)
        tx_hash = result.get("txid") or result.get("transaction", {}).get("txID") or ""

        if not tx_hash:
            logger.error("tron broadcast no txid: %s", result)
            return {"ok": False, "error": "broadcast не вернул txid", "raw": result}

        logger.info(
            "[tron] sent %.2f USDT to %s (reason=%s) tx=%s",
            amount_usdt, to_address, reason, tx_hash,
        )

        # Лог в storage
        try:
            from storage import storage as _storage
            await _storage.add_tron_outbound(
                tx_hash=tx_hash,
                to_address=to_address,
                amount_usdt=amount_usdt,
                reason=reason,
                status="broadcasted",
            )
        except Exception as e:
            logger.warning("[tron] storage log failed: %s", e)

        # Ожидание confirmation
        confirmed = False
        if wait_confirmation:
            confirmed = await wait_for_confirmation(tx_hash, timeout_sec=timeout_sec)
            try:
                from storage import storage as _storage
                await _storage.update_tron_outbound(
                    tx_hash, status="confirmed" if confirmed else "pending",
                )
            except Exception:
                pass

        # Баланс после
        balance_after = await get_hot_wallet_balance()

        return {
            "ok": True,
            "tx_hash": tx_hash,
            "confirmed": confirmed,
            "balance_after": balance_after,
            "reason": reason,
            "to": to_address,
            "amount_usdt": amount_usdt,
        }

    except Exception as e:
        logger.exception("[tron] send failed: %s", e)
        return {"ok": False, "error": str(e)[:300]}


async def wait_for_confirmation(tx_hash: str, timeout_sec: int = 90) -> bool:
    """Polling network — ждёт пока tx появится в подтверждённых блоках.
    Tron block time ~3 sec, обычно 1-2 блока достаточно."""
    Tron, _, _ = _import_tronpy()
    if Tron is None:
        return False
    client = _get_client()
    if client is None:
        return False
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        try:
            tx_info = await asyncio.to_thread(client.get_transaction, tx_hash)
            if tx_info and tx_info.get("ret"):
                ret = tx_info["ret"][0] if isinstance(tx_info["ret"], list) else tx_info["ret"]
                if ret.get("contractRet") == "SUCCESS":
                    return True
        except Exception:
            pass
        await asyncio.sleep(3)
    return False
