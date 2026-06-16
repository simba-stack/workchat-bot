# PRIDE P2P — ROADMAP

Состояние биржи `pride-p2p`. Финальная архитектура и план развития.

---

## Архитектура (текущее состояние)

### Деплой
- **Railway проект:** `marvelous-embrace` / сервис `pride-p2p`
- **Домен:** `pride-p2p-production.up.railway.app`
- **Backend:** FastAPI + SQLAlchemy 2.0 async + Postgres
- **Bot:** aiogram 3.27 (@PrideP2P_bot)
- **Mini-App:** статика `/app` в стиле Crypto Bot

### Ключевые адреса
- **Hot wallet TRON:** `TBXvWrBWSHSKxpZ7NMFSe9gpecm1LZghgx`
- **HD master key** — auto-gen, хранится в `SystemSecret`
- **User deposit addresses** — derived per-user через HMAC-SHA256 от master key

### Внешние сервисы
- **TronGrid** — мониторинг TRON блокчейна (бесплатный план 100k/day)
- **Feee.io** — аренда energy для USDT transfers
  - Header: `key: <api_key>`
  - UA whitelist: `PrideP2P-Bot/1.0`
  - V3 endpoint: `/v3/order/create`
  - Lock period: 8 часов
  - price_in_sun ~120 (динамический)
- **FixedFloat** — cross-chain swap (USDT → BTC/ETH/TON/TRX/SOL и т.д.)
- **CoinGecko** — курсы валют

---

## Готово ✅

### Депозиты (USDT TRC20)
- HD-wallet per-user persistent address
- tron_monitor мониторит все user-addr (батчи через TronGrid)
- При депозите → `sweep_single_address()` → `_sweep_one()` (v13)
- USDT собирается на hot wallet через Feee.io аренду
- TRX тратится **только с Feee** (~5 TRX за sweep)
- Hot wallet TRX **НЕ тратится**

### Withdraws (USDT TRC20)
- `tron_service.send_usdt()` v13:
  - energy_needed = (32k/64k) × 1.3 (с TRON penalty buffer)
  - fee_limit_sun = 30_000_000 (cap, не сжигается)
  - Feee.io аренда → broadcast → receipt SUCCESS check
- При успехе бот пишет в личку с tronscan ссылкой
- При fail → balance rollback + 503

### Withdraws (другие монеты — TRX/ETH/BTC/TON/SOL...)
- Через FixedFloat proxy:
  - `fixedfloat_service.create_order()` создаёт FF order
  - `tron_service.send_usdt()` шлёт USDT на FF deposit address
  - FF делает обмен и отправляет coin на адрес юзера
- ⚠️ **Уведомления пользователя пока только в момент создания**, без polling статуса FF

### Mini-App
- Главная: Crypto Bot стиль с градиент-кругами
- Бот: Пополнить / Вывести / История / Обмен
- BiometricManager (Face ID / Touch ID) перед confirm
- Apple Pay success звук (Web Audio API)
- Auto-refresh каждые 5-15 сек
- Settings, История с группировкой по дням

---

## TODO — приоритеты

### Mini-App v2 — НЕ доделано (для следующей сессии)

**Контекст:** В `miniapp/index.html` (1300 строк) **уже есть legacy P2P JS** (строки 583-1163):
- `loadOffers()`, `openOffer(id)`, `myP2pSide`, `currentOffer`
- Endpoint: `/api/v1/offers/...`
- Но **view'ы P2P удалены** из HTML (комментарий на строке 470: «P2P views убраны»)

Перед добавлением P2P-view'ов **обязательно**:
1. Прочитать legacy JS блок (583-1163)
2. Решить: переиспользовать функции или заменить чистыми (мои черновики в `*_v12.py.tmp` бекапах)
3. Backup: `miniapp/index.html.v1.backup` — рабочая v1 на 16 июня 2026

**Дизайн прототипа уже готов** — Crypto-Bot тёмная тема, 28 экранов: market / filters / offer / 5-step create / deal / chat / dispute / profile / reviews / my-ads / appeal / etc.

**Bottom-nav правильный:** Кошелёк / P2P / История / Ещё (без Обмена — Обмен в action-grid под балансом).

---

### P0 (важно, для прода)

1. **FF withdraw poller + уведомления** ← запросил SIMBA 2026-06-16
   - Фон-задача каждые 30 сек поллит pending FF orders
   - При смене статуса → бот шлёт юзеру в личку с explorer-ссылкой
   - Адаптер explorer URL по сети (tronscan/etherscan/blockstream/tonscan/solscan/bscscan)
   - DB: хранить ff_order_id + user_id + статус
   - ~30-60 мин кода

2. **Тест withdraw в реальных условиях**
   - USDT TRC20 — работает после v13, но мало тестов в проде
   - FF (TRX/ETH/BTC) — НЕ ПРОТЕСТИРОВАН с v6+ изменениями
   - Нужно сделать тестовые withdraw'ы каждой популярной монеты

3. **Owner Panel приватный дашборд** (E13)
   - `/owner` route с TG + PIN auth
   - Видеть all users / balances / pending withdraws / FF orders / TRX balance hot/Feee
   - Кнопки: ручной sweep / refund / disable user

### P1 (нужно скоро)

4. **GasFree (JustLend DAO)** — долгосрочное решение энергии
   - Газ платится в USDT через subsidies (~$1/tx)
   - Уберёт зависимость от Feee.io
   - Требует 3-4 часа кода + миграция

5. **Multi-network deposit** — пока только TRC20
   - Депозиты ERC20 USDT (Ethereum) — потребует etherscan мониторинг + ETH HD-wallet
   - Депозиты TON (USDT on TON jetton)
   - Депозиты BTC

6. **Безопасность**
   - HSM/KMS для master_derivation_key (сейчас plain в БД)
   - Rate limiting на withdraw endpoint
   - 2FA для крупных withdraw

### P2 (улучшения)

7. **Notification system** — расширить
   - Email + Push в дополнение к Telegram
   - Подписки на типы событий

8. **Аналитика и отчёты**
   - Daily volume / fees collected / hot wallet balance
   - Алерты при низком hot wallet balance, Feee balance

9. **P2P offers market** — основная фича биржи
   - Шаги 1-9 уже сделаны (миграция 0007, PriceIndex, FeatureFlag, расширения Offer/Deal, etc.)
   - Bot UI расширенный, JARVIS section «Аудит функций»
   - Нужны полевые тесты

---

## Уроки сессии (15-16 июня 2026)

### Sweep v6 → v13 — что выучили

1. **TRON penalty fee** — TRON в 2024+ добавил +25-40% energy на USDT transfers
   - Симуляция `triggerconstantcontract` возвращает БЕЗ penalty
   - Нужен буфер ×1.3-1.5 на симулированную цифру

2. **fee_limit ≠ списание** — это max TRX cap для burn
   - Если energy арендована — не сжигается
   - Низкий fee_limit (< 30 TRX) рестрикует tx даже при наличии энергии → OUT_OF_ENERGY

3. **Feee.io v3:**
   - `lock_period: 28800` = 8 часов (энергия живёт долго)
   - `rent_time_second: 86400` = 24 часа полный срок
   - Activation 3-6 сек после rent
   - 5+ uncompleted orders на одном адресе → code 20014

4. **Bandwidth TRON:** free quota 600 байт/день per address = 2 USDT transfer free per day per address

5. **Архитектурный вывод:**
   - Чистая Feee модель (без fund_trx_from_hot) — TRX только с Feee
   - Hot wallet TRX не тратится при sweep
   - Feee баланс 30-50 TRX = 6-10 sweep запас
   - Стоимость 1 sweep ≈ 5 TRX (~$1.30)

### Финальная цепочка

```
sweep_one(): cooldown → usdt_check → simulate × 1.3 → energy check 
            → if < required → Feee rent → broadcast (fee_limit 30 TRX) 
            → wait 25s → receipt SUCCESS
```

---

## Coverage комиссий

| Операция | Cost | Источник |
|---|---|---|
| Sweep USDT 5-100 | ~5 TRX | Feee.io |
| Withdraw USDT 5-100 | ~5 TRX | Feee.io |
| Withdraw → TRX/ETH/BTC | ~5 TRX (Feee) + 0.5-1% FF | Feee + FF спред |
| Hot wallet TRX | 0 | не тратится при операциях |

---

## Файлы (важные)

- `core/services/sweep_service.py` — v13, чистый Feee
- `core/services/tron_service.py` — send_usdt v13
- `core/services/energy_service.py` — Feee API v3 client
- `core/services/fixedfloat_service.py` — FF API client
- `core/services/wallet_derive.py` — HD-wallet
- `core/services/tron_monitor.py` — депозит мониторинг
- `api/routers/wallet.py` — withdraw/balance/operations endpoints
- `bot.py` — TG-уведомления юзерам
- `miniapp/index.html` — UI в стиле Crypto Bot

---

*Обновлено: 2026-06-16 после стабилизации sweep v13*
