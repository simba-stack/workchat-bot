# Workchat Bot

Telegram-бот для автоматического создания рабочих беседок между клиентом и менеджерами.
Когда клиент пишет триггерную фразу, бот:

1. Проверяет капчу (антибот),
2. Создаёт супергруппу `[PRIDE] Поставки РС | {имя клиента}`,
3. Приглашает в неё менеджеров из списка,
4. Отправляет клиенту invite-ссылку,
5. После входа клиента отправляет приветственное сообщение.

## Архитектура

- **bot** (`aiogram`) — общается с клиентом, держит админ-панель, капчу, кулдаун.
- **userbot** (`Telethon`, авторизуется по номеру телефона) — создаёт супергруппы и инвайтит менеджеров (Bot API такого не умеет).
- **storage** — JSON-файл с атомарной записью + `.bak` бэкап. Хранит админов, работников, кулдауны, статистику, реестр созданных чатов.

## Локальный запуск

1. Скопировать `.env.example` → `.env`, заполнить.
2. `pip install -r requirements.txt`
3. Сгенерировать сессию userbot'а (см. ниже).
4. `python -u bot.py`

## Переменные окружения

| Переменная | Описание |
|---|---|
| `BOT_TOKEN` | Токен бота от [@BotFather](https://t.me/BotFather) |
| `API_ID` / `API_HASH` | С [my.telegram.org/apps](https://my.telegram.org/apps) для userbot |
| `USERBOT_PHONE` | Телефон userbot-аккаунта (с международным кодом) |
| `STRING_SESSION` | Telethon string-сессия (генерируется один раз через `gen_session.py`) |
| `ADMIN_ID` | Telegram ID первого админа (узнать у [@userinfobot](https://t.me/userinfobot)) |
| `STORAGE_PATH` | Путь к state.json (по умолчанию `/app/data/state.json`) |

## Генерация STRING_SESSION

`gen_session.py` — двухфазный stateless-скрипт, рассчитанный на запуск прямо в Railway-контейнере (без локального диска).

**Фаза 1.** В Railway: временно поменять `startCommand` в `railway.json` на `python -u gen_session.py`, выставить `API_ID`, `API_HASH`, `USERBOT_PHONE`, redeploy. В логах появятся `PHONE_CODE_HASH` и `INITIAL_SESSION` — скопировать в Variables.

**Фаза 2.** Получить SMS-код от Telegram, добавить в Variables `TG_CODE` (и `TG_PASSWORD` при включённой 2FA), redeploy. В логах между `STRING_SESSION_BEGIN`/`STRING_SESSION_END` будет string-сессия — скопировать в `STRING_SESSION`.

После — вернуть `startCommand` обратно на `python -u bot.py`, удалить временные `TG_CODE` / `PHONE_CODE_HASH` / `INITIAL_SESSION` из Variables.

## Деплой на Railway

Деплой триггерится `git push` в ветку `main` через GitHub-интеграцию.
Сборка идёт по `Dockerfile` (см. `railway.json`).

State-файл лежит в Railway Volume, примонтированном к `/app/data` (путь задаётся `STORAGE_PATH`).

## Админка

- `/admin` — открыть панель (только для админов из storage).
- Первый админ задаётся `ADMIN_ID` в env. Дополнительный способ: бот при старте пишет в логи **секретную команду** `/admin_<random>` — кто отправит её боту, тот станет админом.
- Через панель: список работников, текст приветствия (с поддержкой premium-эмодзи через пересылку сообщения), кулдаун в минутах, статистика, список админов.

## Триггерные фразы

По умолчанию: «выдай рабочую беседу», «создай рабочую беседу», «новая беседа».
Также работает команда `/new_chat`.

## Зависимости

- Python 3.12
- `aiogram` 3.27 (bot API)
- `telethon` 1.43 (userbot)
- `cryptg` (ускоряет шифрование Telethon в ~10×, требует `gcc` для сборки)
