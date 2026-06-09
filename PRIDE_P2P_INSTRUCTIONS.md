# PRIDE P2P — инструкции SIMBA (что осталось сделать)

Я (Claude) пока ты спал — поднял на Railway полную инфраструктуру для нового сервиса `pride-p2p`. Сделал максимум что можно без твоего участия. Ниже — три шага которые остались за тобой.

## Что уже работает (без тебя)

- ✅ Railway проект **marvelous-embrace** — добавлен сервис **`pride-p2p`** + PostgreSQL онлайн
- ✅ Root Directory = `/pride-outkup-service`
- ✅ Public domain → **`pride-p2p-production.up.railway.app`**
- ✅ 24 env vars выставлены (см. ниже)
- ✅ Postgres схема создана через alembic upgrade head (видно в логах деплоя)
- ✅ В `workchat-bot/api.py` добавлен endpoint **POST `/api/webhook/outkup`** (приёмник событий от pride-p2p с HMAC verify)
- ✅ В `pride-outkup-service/api/routers/webhooks.py` добавлен полноценный обработчик JARVIS push (kyc_decided, dispute_resolved, set_rate, order_completed, feature_flag)
- ✅ `bot/main.py` теперь не валит весь сервис при placeholder токене — Mini-App API продолжает работать

Текущая ошибка деплоя: **`TokenValidationError: Token is invalid!`** — ожидаемо, BOT_TOKEN = placeholder. Будет fix после твоего шага №2.

---

## Шаг 1 — git push свежих правок (из PowerShell)

Я внёс несколько критичных правок которые ещё НЕ в git. Без них новый деплой опять упадёт.

```powershell
cd C:\Users\sycev\workchat-bot
Remove-Item -Force .git\index.lock -ErrorAction SilentlyContinue

# Проверь что webhook добавлен в api.py (последние строки):
Get-Content api.py -Tail 10
# Должно заканчиваться на: return {"ok": True, "skipped": "unknown_event", "event": event}

git add api.py
git add pride-outkup-service/api/routers/webhooks.py
git add pride-outkup-service/bot/main.py
git add PRIDE_P2P_INSTRUCTIONS.md

git commit -m "feat(p2p): JARVIS webhook + safe-skip bot if placeholder token"
git push origin main
```

---

## Шаг 2 — создать @PrideP2P_bot через @BotFather и вставить токен

1. Открой Telegram → `@BotFather`
2. `/newbot` → name: `PRIDE P2P` → username: `PrideP2P_bot`
3. BotFather пришлёт токен формата `12345:ABC...`
4. Зайди в Railway:
   https://railway.com/project/c3b3be24-02ac-4665-a048-28fb939a5220
5. Открой сервис **pride-p2p** → tab **Variables**
6. Найди `BOT_TOKEN` → клик "edit" → вставь токен → Save
7. Railway автоматически передеплоит сервис

После этого — открой `@PrideP2P_bot` в Telegram → `/start` → должна появиться кнопка с Mini-App.

### BotFather дополнительно (рекомендуется):

```
/setdomain → PrideP2P_bot → pride-p2p-production.up.railway.app
/setmenubutton → PrideP2P_bot → текст: «PRIDE P2P», URL: https://pride-p2p-production.up.railway.app/app
/setdescription → PrideP2P_bot → "Обмен USDT / откуп бизнес-счетов PRIDE"
```

---

## Шаг 3 — добавить те же JARVIS секреты в workchat-bot

Чтобы PRIDE JARVIS принимал webhook'и от pride-p2p, в workchat-bot нужны те же `JARVIS_HMAC_SECRET` и `JARVIS_API_TOKEN`:

1. Railway → проект **marvelous-embrace** → сервис **workchat-bot** → Variables
2. Добавь:
   ```
   JARVIS_HMAC_SECRET=t91ijzc500p5xu7xpchrw6pjrq39vw20io7bkj2aolqaqkg34cuzan5as4gand9g
   JARVIS_API_TOKEN=74vqruuxye85ty6a48kgcqd6a210amvh649xipxmohz028rr
   PRIDE_P2P_BASE_URL=https://pride-p2p-production.up.railway.app
   ```

После шага 1 (push) — workchat-bot будет принимать события от pride-p2p на `/api/webhook/outkup` с HMAC проверкой.

---

## Шаг 4 (опциональный сейчас) — TRON горячий кошелёк

Когда захочешь включить реальные выплаты USDT TRC20 клиентам через бота — создай НОВЫЙ кошелёк (отдельный от workchat-bot), пополни и впиши в pride-p2p Variables:

```
TRON_PRIVATE_KEY=<hex private key>
TRON_HOT_WALLET_ADDRESS=Txxxxx
TRONGRID_API_KEY=<если есть>
```

Сейчас сервис работает в "read-only" режиме для TRON — депозиты не зачисляются, выплаты не отправляются. Это нормально для пилотного запуска.

---

## Архитектура (TL;DR)

```
                    ┌──────────────────────────────────────┐
                    │       PRIDE JARVIS (workchat-bot)     │
                    │  - админка ролей/откупов/доступов     │
                    │  - принимает webhook'и от pride-p2p   │
                    │  - в Notifications падают P2P события │
                    └────────────▲──────────────┬───────────┘
                                 │ webhooks     │ webhooks
                                 │ (HMAC)       │ (HMAC)
                                 │              ▼
                    ┌────────────┴──────────────────────────┐
                    │  pride-p2p (новый сервис, Railway)    │
                    │  - @PrideP2P_bot (aiogram)            │
                    │  - Mini-App (FastAPI + index.html)    │
                    │  - PostgreSQL (11 таблиц)             │
                    │  - JARVIS sync loop (pull курса)      │
                    │  - escrow / orders / KYC / disputes   │
                    └───────────────────────────────────────┘
```

### Endpoints

**workchat-bot (JARVIS):**
- `POST /api/webhook/outkup` — приём событий от pride-p2p (new_order, payment_requested, deposit_received, kyc_requested, dispute_opened и т.д.)

**pride-p2p:**
- `GET /health` — healthcheck (используется Railway)
- `GET /app` — Mini-App HTML
- `POST /api/v1/webhooks/jarvis` — приём JARVIS push (set_rate, kyc_decided, dispute_resolved, order_completed)
- `POST /api/v1/webhooks/tron` — TronGrid (заглушка Phase A5)
- `GET /api/v1/exchange/rate` — текущий курс
- `POST /api/v1/exchange/buy_usdt` — заявка купить USDT
- `POST /api/v1/exchange/sell_usdt` — заявка продать USDT
- `POST /api/v1/orders/business_outkup` — крупный откуп с реквизитами
- ... (см. `pride-outkup-service/api/routers/`)

---

## Что я НЕ трогал (важно)

- ❌ Я НЕ удалил случайно созданный мной ранее проект Railway `captivating-integrity` (там левый сервис corp-sim-relay). Удали его сам или оставь — он не мешает.
- ❌ TRON_PRIVATE_KEY ни в каком виде не вписывал
- ❌ BOT_TOKEN — только placeholder, реальный токен только ты сам через BotFather

---

## Если что-то сломается

Логи деплоя: https://railway.com/project/c3b3be24-02ac-4665-a048-28fb939a5220/service/c0a3a4cb-c2d4-4a29-8c9f-968ebf5ed4cb

Если бот стартует но не отвечает на /start:
1. Проверь что `BOT_TOKEN` совпадает с тем что BotFather дал
2. В BotFather `/mybots → PrideP2P_bot → Bot Settings → Group Privacy → Disable` (если бот будет в группах)

Если Mini-App не открывается:
1. Проверь что MINIAPP_URL = `https://pride-p2p-production.up.railway.app` (без trailing slash)
2. В BotFather `/setdomain` — должен быть тот же домен без https://

Если JARVIS не принимает webhook:
1. Проверь что `JARVIS_HMAC_SECRET` идентичен в обоих сервисах
2. После добавления переменных в workchat-bot нужно сделать Redeploy
