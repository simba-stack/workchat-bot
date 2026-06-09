# PRIDE P2P Service

Standalone P2P-биржа и обменник USDT↔RUB. Связан с PRIDE JARVIS через webhook + REST API.

**Bot:** [@PrideP2P_bot](https://t.me/PrideP2P_bot)
**Mini-App:** `https://pride-outkup-service-production.up.railway.app/app`
**Status:** 🏗 In development (Phase A1 — skeleton)

## Stack

- Python 3.12, aiogram 3 (bot), FastAPI (Mini-App backend)
- PostgreSQL (SQLAlchemy + Alembic)
- Vue 3 + Vite (Mini-App frontend)
- Telegram WebApp SDK
- tronpy (TRC20 USDT)
- Redis (optional, для rate limiting и pub/sub)

## Структура

```
bot/        — aiogram bot (handlers, keyboards)
api/        — FastAPI backend (routers, auth)
core/       — общая логика: models, schemas, services
miniapp/    — Vue 3 frontend (Mini-App SPA)
migrations/ — alembic
scripts/    — one-off (import_partners, seed_dev)
tests/
```

## Quick start

```bash
# 1. Install
pip install -r requirements.txt

# 2. .env
cp .env.example .env  # заполнить токены

# 3. DB
alembic upgrade head

# 4. Run
python -m bot.main &
uvicorn api.main:app --host 0.0.0.0 --port 8000
```

## Связь с PRIDE JARVIS

Outkup-сервис **отдельный сервис** в отдельном Railway-проекте, но синхронизируется с workchat-bot (PRIDE JARVIS):

- **Webhook**: outkup-service шлёт события в JARVIS (`POST /api/webhook/outkup`)
- **REST API**: JARVIS пуллит данные для UI (`GET /api/v1/sync/*`)
- **Admin actions**: JARVIS может писать в outkup-сервис (`POST /api/v1/admin/*`)

Подробности — в `../OUTKUP_DESIGN_DOC.md` (секция 9).

## TODO

См. `../OUTKUP_SERVICE_PLAN.md` секция 7 (Phases).

- [x] Phase 0: Design-doc
- [ ] Phase A1: Skeleton (← сейчас)
- [ ] Phase A2: KYC + регистрация
- [ ] Phase A3: Mini-App V1
- [ ] Phase A4: Интеграция с JARVIS
- [ ] Phase A5: Tron auto-payouts
- [ ] Phase B1-B3: V2 P2P-стакан
