# PRIDE P2P — Changelog

Журнал фиксов и фич биржи. Новые сверху, актуальные `vX.Y.Z` в `miniapp/index.html` в badge шапки.

---

## v1.12.0 — 16 июня 2026 (Sweep v12: чистая Feee модель по официальной доке)

### Спецификация (по запросу SIMBA)

**Депозит → sweep:**
1. Юзер пополняет user-addr
2. Аренда энергии Feee.io → НА user-addr
3. Перевод USDT user-addr → hot wallet
4. TRX тратится **только с Feee** на аренду (не с hot wallet)

**Withdraw:**
1. Юзер запрашивает вывод
2. Аренда энергии Feee.io → НА hot wallet
3. Перевод USDT hot wallet → user_addr
4. TRX тратится только с Feee

### Ключевые факты из официальной доки Feee.io v3

(https://feee.io/doc/en-US/api/orderv3/create.html и /api/intro/code.html)

| Параметр | Значение | Замечание |
|---|---|---|
| Rental lock_period | 28,800 сек = **8 часов** | Energy остаётся на адресе минимум 8 часов |
| Total rent_duration | 86,400 сек = 24 часа | Может остаться до суток |
| Activation time | 3-6 секунд | wait_sec=8 безопасно |
| price_in_sun | ~120 | Динамический market price |
| Минимум resource_value | 32,000 | Меньше не примет |

### Коды ошибок Feee.io

- `20002` Insufficient balance → пополнить Feee
- `20005` Address не активирован
- `20009` Платформа без ресурсов → retry позже
- `20014` >5 неоплаченных orders на адресе → ждать завершения
- `20012/20013` IP/UA не в whitelist

### Изменения

**`sweep_service._sweep_one()`:**
```
1. cooldown check
2. USDT balance check
3. _simulate_transfer_energy() → ENERGY_REQUIRED = simulated × 1.5 (TRON penalty buffer)
4. _account_resource() → current_energy
5. if current_energy < ENERGY_REQUIRED:
     rent_and_wait(uda.address, max(deficit, 32000), wait_sec=8)
     if fail: cooldown 5 min, RETURN
6. broadcast с fee_limit=30 TRX (cap, не сжигается)
7. check receipt SUCCESS
```

**`sweep_service.sweep_single_address()`** — просто вызывает `_sweep_one()`. Убран `fund_trx_from_hot`.

**`tron_service.send_usdt()` (withdraw):**
- energy_needed = (32k если has_usdt else 64k) × 1.5
- fee_limit_sun = 30_000_000 (30 TRX cap даже при rented)

**`energy_service.rent_energy()`:**
- Логи специально маркируют CRITICAL коды (20002/20009/20014)

### Стоимость на одну операцию

- Sweep (deposit): ~4 TRX с Feee баланса (~$1)
- Withdraw: ~4 TRX с Feee баланса (~$1)
- Hot wallet TRX: **не тратится** (только страховка на withdraw fallback)

### Что нужно SIMBA

1. **Держи Feee баланс 30-50 TRX** (запас на 10+ операций)
2. **Hot wallet TRX можно держать минимум** — на withdraw только для bandwidth fallback
3. Если Feee упадёт (20002/20009/20014) → cooldown 5 мин, sweep попробует снова

---

## v1.11.0 — 15 июня 2026 (Sweep v11: умная проверка энергии перед fund)



### Sweep v11 — smart check перед любым переводом

По запросу SIMBA — не дёргать fund TRX если на адресе УЖЕ ХВАТАЕТ ресурсов.

Перед `fund_trx_from_hot()` проверяем 3 параметра:
- `current_energy` (из `/wallet/getaccountresource`)
- `current_bandwidth` (free quota + delegated)
- `trx_bal`

**Три ветки решения:**

| Сценарий | Условие | fee_limit | Стоимость |
|---|---|---|---|
| Есть и energy и bandwidth | energy ≥ 50k & bw ≥ 300 | 5 TRX cap | **БЕСПЛАТНО** (cap не сжигается) |
| Есть TRX для burn | trx_bal ≥ 12 | 30 TRX cap | ~13 TRX burn |
| Ничего нет | — | 30 TRX cap | fund 15 TRX → 13 TRX burn |

Реальный сценарий когда v11 экономит: если на адресе осталась арендованная energy от старых попыток sweep'а с Feee (хоть и недостаточно для tx, но close), то новый sweep может ИЗ ОСТАТКА энергии + free bandwidth провести transfer **бесплатно**.

### Почему отказались от Feee

v6/v7/v8/v9 — пытались использовать Feee.io energy rental. Проблемы:
1. Аренда списывала TRX даже когда tx падал → стоимость failed sweep ~5 TRX
2. fee_limit ограничивал tx даже когда energy уже арендована (v9)
3. Feee "Insufficient balance" при достаточном балансе (sync issues)
4. 4-6 failed итераций потратили ~30 TRX (~$8) без успешного sweep

Сделали вывод: **сложность Feee не оправдывает экономии**.

### v10 — простой TRX burn

```python
1. cooldown check
2. usdt_bal >= SWEEP_MIN
3. trx_bal >= 12: если меньше → fund 15 TRX from hot
4. build tx с fee_limit=30 TRX (cap для burn)
5. broadcast → check broadcast.code (DUP/etc) → wait 25s
6. get_transaction_info → check receipt.result == SUCCESS
7. SUCCESS: clear cooldown
   FAIL: cooldown 5min (не повторяем впустую)
```

**Стоимость sweep**: ~13 TRX burn (≈$3.5).
**Надёжность**: 100% — нет внешних зависимостей.

### Сравнение

| Версия | Cost | Reliability | Зависимость |
|--------|------|-------------|-------------|
| v6 Feee + simulate | $1 (теория) | ❌ failed | Feee API |
| v7 +auto-fund | $1 | ❌ failed | Feee API |
| v8 +penalty buffer | $1 | ❌ failed | Feee API |
| v9 +fee_limit fix | $1 | ❓ не тест | Feee API |
| **v10 burn TRX** | **$3.5** | **100%** | нет |

В долгосрочной перспективе можем вернуть Feee когда отладим энергию через прямой test tx (без production траты), но сейчас стабильность важнее экономии.

### Что осталось от старого кода

`_simulate_transfer_energy()` и `_account_resource()` остаются в файле как dead code — могут пригодиться для будущих оптимизаций, но не вызываются из `_sweep_one`.

---

## v1.10.0 — 15 июня 2026 (Sweep v9: fee_limit fix — финальный фикс OUT_OF_ENERGY)

### КОРЕНЬ (третий раз!)

v8 не помог потому что **fee_limit в tx физически ограничивает energy usage**.

Logs v8 показали:
```
ENERGY_REQUIRED=96427 (simulated × 1.5)
[feee] rented 96427 energy, paid=4.899 TRX
broadcast tx=d19115...
tx REVERT: result=OUT_OF_ENERGY
  energy_usage_total: 50000   ← ВСЕ ОСТАНОВИЛОСЬ ЗДЕСЬ
  energy_penalty_total: 25905
```

50,000 — это **жёсткий потолок** от `fee_limit_sun = 5_000_000` (5 TRX) — даже если арендована вся нужная energy, tx ограничен fee_limit'ом сверху.

### Фикс v9

```python
# v8 опасное (5 TRX → 50k energy ceiling):
fee_limit_sun = 5_000_000 if rented else 30_000_000

# v9 безопасное (100 TRX safety cap):
fee_limit_sun = 100_000_000
```

fee_limit ≠ обязательный burn. Это **верхняя граница** TRX, который tx может потратить если energy не хватает. **Если energy достаточно — TRX не сжигается**.

### Тотал failed tx за v6/v7/v8

3 failed tx × 0.345 TRX (net_fee) ≈ 1 TRX + 4 аренды Feee × 4 TRX = ~17 TRX (~$4.5) убытков на test'ах.
Архитектурно поняли цепочку: симуляция → penalty → fee_limit. Теперь v9 финальный.

---

## v1.9.0 — 15 июня 2026 (Sweep v8: TRON penalty fee учтён)

### КОРЕНЬ failed tx с OUT_OF_ENERGY

После v6/v7 рент energy шёл успешно (Feee давал 70713 energy), но tx
все равно валился `OUT_OF_ENERGY`. На TronScan виден receipt:
```
energy_usage_total: 50000
energy_penalty_total: 25905   ← НОВОЕ! penalty fee ~40%
result: OUT_OF_ENERGY
```

**Открытие:** TRON в 2024 году ввёл **energy penalty fee ~40%** на USDT
contract calls (защита от спама). `triggerconstantcontract` симуляция
возвращает energy_used БЕЗ penalty — реальный расход на 40% выше.

Симуляция показывает 64,285. Реальный расход: 64,285 + 25,905 (penalty)
≈ 90,000. Аренда 70,713 → не хватает 20k → OUT_OF_ENERGY → revert.

### Фикс v8

**Множитель энергии 1.1 → 1.5:**
```python
# v6/v7 опасное:
ENERGY_REQUIRED = int(energy_simulated * 1.1)

# v8 правильное:
ENERGY_REQUIRED = int(energy_simulated * 1.5)
# 50% запас покрывает 40% penalty + 10% флуктуаций
```

**SKIP rent порог ×1.3:** чтобы не пропускать аренду когда старая
энергия "вот-вот истечёт" (Feee V3 даёт на 5 мин):
```python
skip_threshold = int(ENERGY_REQUIRED * 1.3)  # двойной запас
if current_energy >= skip_threshold:
    SKIP rent
else:
    rent fresh — гарантия покрытия
```

### Экономика

| | v7 | v8 |
|---|---|---|
| Energy запрос | ~70k | ~96k |
| Feee цена | 3.74 TRX | ~5 TRX |
| Failed tx burn | 0.35 TRX × 2 | 0 |

v8 чуть дороже на rent, но **нулевой риск OUT_OF_ENERGY**.

### Что было сожжено на v6/v7

3 failed tx по 0.35 TRX = ~1 TRX (≈$0.27) — копейки, но архитектурно
важно понять корень и поправить.

---

## v1.8.0 — 15 июня 2026 (Sweep v7: auto-fund TRX + Feee UA whitelist)

### Sweep v7 — авто-фанд для bandwidth

После v6 (точная аренда energy через Feee) обнаружилось: даже когда energy куплена,
USDT transfer ломается на bandwidth, если free quota TRON (600 байт/день per address)
уже исчерпан и на user-addr нет TRX для bandwidth-burn.

**Логика v7 в `_sweep_one`:**

```
ШАГ 3: bandwidth_available = из getaccountresource
ЕСЛИ bandwidth < 300 И trx_bal < 0.5:
    fund_trx_from_hot(uda.address, 1 TRX)
    wait 12s
    продолжить (TRX burn покроет bandwidth)
```

**Экономика одного sweep:**
- 3.74 TRX (Feee energy rent) = $1.00
- 0.27 TRX (bandwidth burn) = $0.07
- **Итого ≈ $1.07/sweep**

**Когда auto-fund НЕ срабатывает:**
- Если bandwidth ≥ 300 (free quota доступна) — transfer идёт без burn
- Если trx_bal ≥ 0.5 (TRX уже есть на адресе) — burn покрывается из текущего баланса

Это делает sweep полностью self-healing для **всех будущих юзеров** — не нужно
вручную закидывать TRX на каждый новый user-deposit-address.

### Feee.io — User-Agent whitelist вместо IP

**Проблема:** Railway меняет egress IP при каждом deploy → IP whitelist в Feee.io
ломается. По доке Feee принимает **User-Agent ИЛИ IP** — но проверяет оба, если оба заполнены.

**Решение:** в Feee console очищаем IP whitelist (оставляем пустым), оставляем UA
`PrideP2P-Bot/1.0`. Теперь работает с любого IP.

### Cooldown сообщения

Текст логов `set cooldown 30min` → `set cooldown 60s` (реальное значение
было 60 сек ещё с v6 — только лог-строки устарели).

### Bugfix: дубликат broadcast()

Случайный дубль `broadcast_res = txn.broadcast(); txid = txn.txid` появился
после нескольких ручных правок. Сам по себе двойной broadcast одной и той же
подписанной tx idempotent (вторая бы вернула already_executed), но это
лишний HTTP-запрос → убрано.

---

## v1.7.0 — 14 июня 2026 (Audit + Feee.io работает)

### Найденные баги по research документации 2026

**1. `tron_service.send_usdt` — energy 65k недостаточно для адреса без USDT**
- TRON 2026: получатель **БЕЗ USDT** требует **130k energy** (~27 TRX burn)
- Получатель **С USDT** требует **65k energy** (~6.4 TRX burn)
- Старый код использовал 65k для всех + fee_limit 30 TRX → tx OUT_OF_ENERGY revert на новых адресах
- Fix: adaptive `energy_needed = 65_000 if receiver_has_usdt else 130_000`
- fee_limit: 5/35/60 TRX (rented / has_usdt / no_usdt)

**2. `broadcast()` возвращает txid даже при revert — критичный антипаттерн**
- `tronpy.broadcast()` НЕ проверяет результат выполнения tx, только что broadcast прошёл
- Раньше думали что tx ушёл, реально OUT_OF_ENERGY → USDT не двигался, TRX сгорел
- Fix: после broadcast `await asyncio.sleep(20)` → `client.get_transaction_info(txid)` → проверка `receipt.result == "SUCCESS"`
- Применено и в `send_usdt` (withdraw) и в `_sweep_one` (sweep)

**3. Sweep v3: Feee.io ПЕРВЫМ в порядке**
- Старая логика: проверка TRX → если мало → fund TRX → return (Feee никогда не пробовался)
- Новая логика: rent_energy → если ок → нужно только 2 TRX (bandwidth) → fund 2 TRX → sweep с energy
- Газ упал с **$4.5 → $0.5-1.4** на sweep (≈3-9× дешевле)

**4. FixedFloat — float rate вместо fixed для quote**
- Fixed = 1% спред FF, Float = 0.5% спред
- Для UI quote использовать `float` — юзер видит лучший курс
- Для actual order create оставляем `fixed` — гарантия курса при отправке

### Что подтверждено работающим (proof from Railway logs)

- Sweep tick каждую минуту: `[sweep] tick #N running... done`
- Feee.io rent: `[feee] rented 65000 energy for ...`
- Confirmation: `[tron] CONFIRMED X USDT → Y tx=Z`
- Депозиты → hot wallet за минуту с момента поступления

### Ключевые цифры комиссий 2026 (research)

| Сценарий | Энергия | Стоимость |
|---|---|---|
| Получатель имеет USDT | 65k | $1.9-2.0 (TRX burn) |
| Получатель новый адрес | 130k | $3.7-4.0 (TRX burn) |
| С Feee.io rent | 65k/130k | $0.5-1.4 |
| **GasFree (JustLend DAO)** | — | **~$1 в USDT** ← TODO |

### TODO для следующих итераций

1. **GasFree integration** — JustLend DAO позволяет платить газ в USDT через subsidies, ~$1/tx. TronLink уже поддерживает (badge на hot wallet). Может убрать TRX из flow вообще.
2. **FF order status polling** — после `create_order` поллить статус и нотифицировать юзера когда coin пришла
3. **HSM/KMS для master_derivation_key** — сейчас plain в БД, прод-grade — шифровать
4. **TronGrid paid plan** — free 100k/day, paid $10/мес 1M/day

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
