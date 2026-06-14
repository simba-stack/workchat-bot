# PRIDE P2P — Changelog

Журнал фиксов и фич биржи. Новые сверху, актуальные `vX.Y.Z` в `miniapp/index.html` в badge шапки.

---

## v1.6.0 — 12 июня 2026 (фикс кнопок Mini-App)

### КОРЕНЬ всех «кнопки не жмутся»
Старый JS: `$('main-swap').addEventListener(...)` — но `main-swap` был удалён из HTML и заменён на `main-history`. `null.addEventListener` → TypeError → весь JS файл прерывается с этой строки, **все обработчики ниже не регистрируются** (settings, toggle-balance, wd-submit, все sw-* в Обмене).

### Фикс
- Введена утилитка `safeBind(id, event, fn)` — не падает если элемент `null`
- Все главные кнопки и опциональные `main-*` теперь привязаны через `safeBind`
- **Debug-overlay** (красная полоса снизу) — ловит `window.error` + `unhandledrejection`, показывает прямо на экране Mini-App

### Правило
**НИКОГДА** в Mini-App не делать прямой `$(id).addEventListener(...)` — только `safeBind(id, ...)`. Иначе одна null-кнопка убивает весь JS.

---

## v1.5.0 — debug overlay

Добавлен `__debug_box` снизу экрана Mini-App. Перехватывает все JS-ошибки и показывает их пользователю (раньше без F12 на телефоне было невозможно дебажить).

---

## v1.4.0 — auto-refresh + биометрия inline

- Удалён сломанный `cloneNode` IIFE для `wd-submit`/`sw-submit` (он ломал sw-handlers косвенно)
- Face ID / Touch ID через `tg.BiometricManager` встроен **прямо** в оригинальные handlers `wd-submit` и `sw-submit`
- Apple Pay success звук — Web Audio API, 3 ноты D5→A5→D6, длительность ~350мс + `HapticFeedback.notificationOccurred('success')`
- **Auto-refresh**:
  - История (view-history) — каждые **5 сек** silent
  - Страница монеты (view-coin) — каждые **5 сек** silent
  - Главная (view-main) — каждые **15 сек**
  - Останавливается когда `document.hidden`
- Backend `/wallet/operations` обёрнут в `try/except` — никогда не отдаёт 500 (вместо этого пустой `items: []`)

---

## v1.3.0 — курсы $0.00 + Не доступно + openCoin

### Баг 1: все курсы $0.00 у монет
`coinPrice('TON')` искал `rates['the-open-network']` (coingecko_id), но backend (`rates_service.tick()`) пишет в kv_settings ключами по coin-code `rates['TON']`. Несовпадение → все курсы 0.

**Fix**: `coinPrice()` теперь смотрит ОБА варианта: `rates[code] || rates[cg_id]`. Поддерживает поля `change_24h` и `usd_24h_change`.

### Баг 2: "Не доступно" в Пополнить
Если справочник `coins[]` ещё не загружен → `nets[]` пустой → текст "Не доступно".

**Fix**: дефолтные сети по умолчанию (`NET_DEFAULTS = {USDT:'TRC20', TON:'TON', BTC:'BTC', ETH:'ERC20', ...}`).

### Баг 3: openCoin при пустом справочнике
`coins.find(...)` возвращал `undefined` → `return` без `go('coin')` → страница не открывалась.

**Fix**: если `!coins.length` — `await loadMain()` сначала, потом fallback на `{code, name: code, networks:[]}`.

---

## v1.2.0 — параллелизация загрузки

- `loadMain()` теперь делает **`Promise.all([/coins, /balances])`** вместо последовательных `await` (вдвое быстрее)
- Справочник монет кешится в `sessionStorage('p2p_coins')` — при повторных открытиях `/coins` не дёргается
- `openCoin()` теперь использует `/wallet/operations` (новая лента) вместо `/wallet/transfers` (только P2P) → история монеты наконец видна

---

## v1.1.0 — UI редизайн в стиле Crypto Bot

- **Главные кнопки**: голубые градиент-круги (#3DA3F0 → #2A86D6), SVG-иконки, scale-анимация на тап + ripple-вспышка + cubic-bezier easing
- 3 кнопки: **Пополнить / Вывести / История** (убрана "Отправить"=чек)
- **Bottom-nav**: Кошелёк / Обмен с SVG-иконками, активная подсвечена синим
- **Шапка**: `⚙` (настройки) вместо `⋯`
- **Баланс**: знак `$` отдельным span'ом, цифра крупная 42px, иконка глаз SVG (eye-open / eye-off перечёркнутый)
- **При скрытом балансе**: `•••` вместо цифр
- Удалена кнопка `4-й чек` из coin-view
- Удалена вкладка `Чеки` из bottom-nav

### Новые экраны
- **view-history**: единая лента операций, группировка по дням (Сегодня / Вчера / 10 июня), цветные иконки типов
- **view-settings**: профиль (юзернейм, tg_id), уведомления toggle, звук toggle, язык, версия Mini-App, кнопка «Связаться с поддержкой»

---

## v1.0.x — backend основные фиксы

### USDT withdraw — критический баг
Старый код:
```python
if tron_service.is_configured() and amount <= 100:  # узкий порог
    ...
# Иначе — баланс СПИСАН, tx НЕ отправлена, status="pending" молча
```

**Fix**: убран порог `amount <= 100`. Если `tron_service.is_configured() == False` → **rollback + 503**. Если `send_usdt` failed → rollback + 503. Лог `[withdraw] USDT/TRC20 SENT/FAILED`.

### USDT withdraw fee
- Было: $4.5 для всех монет
- Стало: **$3** для USDT (т.к. реальный газ ~$0.35 → profit ~$2.65), **$4.5** для прочих coin

### Sweep service
- `tick()` и `sweep_loop()` восстановлены из HEAD (были обрезаны)
- Интервал `SWEEP_INTERVAL_SEC = 60` (раньше 3600) — sweep с user-addr → hot wallet каждую минуту
- Owner Panel → "Sweep ALL" теперь работает (раньше вызывал `tick()` который не существовал)

### FixedFloat proxy withdraw
Для всех coin кроме USDT/TRC20:
1. CoinGecko rate для расчёта USDT эквивалента + 1% запас
2. Создаём FF order: `from=USDT, to=<COIN>, to_address=user_addr`
3. Шлём USDT с hot wallet на FF-адрес
4. FF выдаёт юзеру нужную coin
5. Profit = withdraw_fee - (USDT_paid - amount × rate)
6. Если FF не настроен или ошибка → rollback + 503

### Бот-уведомления
- При успешной USDT/TRC20 отправке → бот пишет юзеру в ЛС с tronscan-ссылкой
- При FF-proxy выводе → "Запущен обмен USDT в {coin} через FixedFloat. Придёт за 10-30 мин"
- При депозите (через `tron_monitor`) → "+X USDT, Депозит зачислен"

### Endpoint /wallet/operations
Единая лента operations_log: `{id, type, coin, amount, amount_abs, rub, note, tronscan_url, balance_after, created_at}`. Источник всех экранов История + страница монеты.

---

## Архитектура (для справки)

```
@PrideP2P_bot
├─ Railway проект: marvelous-embrace / pride-p2p
├─ Домен: pride-p2p-production.up.railway.app
├─ Hot wallet: TBXvWrBWSHSKxpZ7NMFSe9gpecm1LZghgx (TRON)
├─ Variables:
│  ├─ TRON_PRIVATE_KEY (для исходящих USDT/TRC20)
│  ├─ TRON_HOT_WALLET_ADDRESS
│  ├─ FIXEDFLOAT_API_KEY + FIXEDFLOAT_API_SECRET (для cross-chain)
│  ├─ FEEE_API_KEY (energy rental для дешёвого газа)
│  ├─ COINGECKO_* (rates polling)
│  └─ DATABASE_URL (Postgres addon)
├─ Mini-App URL: pride-p2p-production.up.railway.app/app
└─ Sync с JARVIS: HMAC webhook'и
```

## Известные правила для будущих фиксов

1. **Mini-App handlers** — только через `safeBind(id, event, fn)`. Никогда прямой `$(id).addEventListener`.
2. **Windows file truncation** — после правок в Mini-App проверять `wc -l` и количество backticks (должно быть чётное).
3. **Telegram кеширует Mini-App жёстко** — после каждого UI-релиза bump версии в badge и просить пользователя очистить кеш бота.
4. **Backend endpoints** возвращать пустой ответ при ошибках, а не 500 — UI не должен показывать "Ошибка загрузки" из-за временных сбоев.
5. **CoinGecko rate-limit** — все вызовы через кеш в `kv_settings`, не напрямую. `get_rates()` НЕ ходит в CoinGecko, только читает БД.
6. **Backticks/template literals** — после каждой большой правки `python3 -c "s=open('miniapp/index.html').read(); print(s.count(chr(96)) % 2 == 0)"` должно быть `True`.
7. **TRON withdraw** — никогда не списывать баланс без guaranteed rollback path. Если не получилось отправить — `balance_service.credit(...)` обратно + `raise HTTPException(503)`.
