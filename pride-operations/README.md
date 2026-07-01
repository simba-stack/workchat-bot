# PRIDE Operations

Веб-платформа для клиентов и операторов PRIDE. Зеркалит Telegram-инфраструктуру (workchats + канал + общий) + полный JARVIS CRM в едином glass-UI.

## Sprint 1 · MVP Auth (текущий)

- [x] FastAPI backend со структурой
- [x] Telegram Login Widget verify (HMAC-SHA256)
- [x] JWT access + refresh (HttpOnly cookies)
- [x] Glass UI (dark/light theme toggle)
- [x] `/api/me`, `/api/auth/telegram`, `/api/auth/refresh`, `/api/auth/logout`
- [x] Dockerfile + railway.json

## Локальный запуск

```bash
cd pride-operations
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -r backend/requirements.txt

# Минимальные env
export TG_BOT_TOKEN=""  # оставь пустым для dev — авторизация без проверки
export TG_BOT_USERNAME=PrideInviteWork_bot
export JWT_SECRET=$(openssl rand -hex 32)
export ADMIN_TG_IDS=8151738775

cd backend && uvicorn main:app --reload
```

Открой <http://localhost:8000>

## Деплой в Railway (marvelous-embrace)

1. В Railway UI открой проект **marvelous-embrace**
2. Кнопка "+ Add Service" → "GitHub Repo" → выбери `simba-stack/workchat-bot`
3. В service Settings → "Root Directory" укажи: `pride-operations`
4. В "Variables" добавь:
   - `TG_BOT_TOKEN` = токен любого PRIDE-бота (напр. @PrideInviteWork_bot)
   - `TG_BOT_USERNAME` = `PrideInviteWork_bot` (без @)
   - `JWT_SECRET` = случайная строка (64+ символов) — `openssl rand -hex 32`
   - `ADMIN_TG_IDS` = `8151738775` (SIMBA)
   - `JARVIS_BASE_URL` = `https://workchat-bot-production.up.railway.app`
   - `P2P_BASE_URL` = `https://pride-p2p-production.up.railway.app`
5. В "Settings" → "Networking" → Generate Domain → получишь `pride-operations-production.up.railway.app`
6. Deploy запустится автоматически при push в main

## Проверка после деплоя

```
GET /healthz              → {"status":"ok","app":"PRIDE Operations","version":"0.1.0"}
GET /                     → HTML login page (glass)
POST /api/auth/telegram   → JWT в cookie
GET /api/me               → текущий user
```

## Roadmap

- [x] Sprint 1: Auth + shell (готово)
- [ ] Sprint 2: Chat list + history read-only (WebSocket, Telethon adapter)
- [ ] Sprint 3: Send + callback buttons + reactions + files
- [ ] Sprint 4-6: Admin CRM табы
- [ ] Sprint 7-9: Voice, стикеры, Push
- [ ] Sprint 10-12: Performance, тесты, полировка

Полный roadmap → `../outputs/webpanel/MASTER_ROADMAP.md`
