# workchat-bot — роадмап и контекст проекта

> Живой документ. Обновляется после каждой задачи в сессии Claude — чтобы при крэше или новой сессии всё было на руках.

**Снимок:** обновлено **2026-05-21** (сессия SIMBA + Claude через Cowork).

---

## 1. Что это

Telegram-экосистема для бизнеса **PRIDE** (поставки расчётных счетов / ИП + теперь кредитование).

- **Репо:** github.com/simba-stack/workchat-bot
- **Деплой:** Railway (native auto-deploy на push to `main`)
- **Владелец / разработчик:** SIMBA (@SIMBA_PRIDE_ADM = @PRIDE_CL)
- **Последний тег:** `v2.0.2` (16 мая) — основной поток. Запланирован `v2.0.3` для Electron desktop (popout звонки)

---

## 2. Архитектура (что где живёт)

```
workchat-bot/
├── bot.py                  entrypoint (29 KB) — aiogram main + dashboard + CRM + userbot в одном процессе
├── userbot.py             (442 KB!) — Telethon: создание чатов, AI-ответы, парсинг команд
├── crm_bot.py             (238 KB) — @PrideCONTROLE_bot (CRM для поставщиков + теперь + кредитование)
├── api.py                 (216 KB) — FastAPI для jarvis.html (десятки endpoint'ов)
├── storage.py             (147 KB) — JSON-state, ВСЁ хранилище (crm_* и credit_* параллельно)
├── brain.py               (37 KB) — Claude обёртка + tool_use
├── config.py              (4 KB)  — env vars (BOT_TOKEN, CREDIT_*_CHAT_ID и т.д.)
├── desktop/                       — Electron PRIDE J.A.R.V.I.S.
│   ├── main.js                    — main process + новое: call popout window
│   ├── preload.js                 — IPC bridge для main jarvis
│   ├── call-preload.js            — IPC bridge для popout звонка
│   ├── call-popout.html           — frameless UI окно звонка
│   ├── package.json               — v2.0.3 (ждёт тега для GH Actions сборки)
├── dashboard/jarvis.html  (483 KB SPA) — главный дашборд
└── guacamole/                     — отдельный Railway-сервис для RDP на дедики
```

### Боты Telegram

| Имя | Что |
|---|---|
| @PrideInviteWork_bot | основной (welcome клиента, создание work-чатов) |
| @PrideCONTROLE_bot | CRM-бот (поставщики + теперь и кредитование) |
| Юзербот (Telethon) | без @, телефон. AI-ответы клиентам, парсинг команд |

### Группы Telegram (захардкоженные)

| Назначение | chat_id | Где захардкожено |
|---|---|---|
| CRM **Доступы** (поставщики) | -1003852131311 | `crm_bot.py:HARDCODED_ADMIN_CHAT_ID` |
| CRM **Пароли** (поставщики) | -1003788743917 | `crm_bot.py:HARDCODED_PASSWORD_CHAT_ID` |
| **КРЕДИТ Доступы** | -1003457011118 | `config.py:CREDIT_ACCESS_CHAT_ID` |
| **КРЕДИТ Пароли** | -1003945639230 | `config.py:CREDIT_PASSWORD_CHAT_ID` |

> **Архитектура credit-чатов:**
> - **Централизованные** группы (выше в таблице) — куда CRM-бот пишет анкеты/ЛК всех клиентов кредитования (зеркало поставщиков). Хардкод в config.py.
> - **Рабочие** группы клиентов (где юрист общается с клиентом) — регистрируются командой «Ассистент возьми этот чат под кредитование - менеджер @ник» в самой этой группе. Сохраняются в `storage.credit_chats`. Пример рабочей: `-1003599725191` (юрист @hohu5000).
> - Обе цепочки активируют credit-track через `storage.is_credit_chat()`.

---

## 3. Хронология этой сессии (что сделано)

### 3.1. Аудит и исправление модалки «Новая заявка на обмен» в Операционной ✅
**Коммит:** `4b951e2` (запушен)
- Удалён сломанный дубль блока «Маржа preview» (из коммита 5c1776c — copy-paste)
- HTML маржи переписан под актуальные имена из `calcMargin()` (in_rub/out_rub/we_received_usdt/partner_payout_usdt/lk_prices_usdt)
- Селекты банков ВХОДА/ВЫХОДА расширены с 2/5 до 9 опций каждый
- Добавлен `submittingExchange` флаг против двойного клика на «Зафиксировать»

### 3.2. Live Event Feed + ops-floor — только на главной ✅
**Коммит:** ждёт пуша (вместе с остальными)
- Оба блока (`<section class="jarvis-panel">` с человечками и `<section>Live Event Feed`) обёрнуты в `x-show="currentView === 'office'"`
- На вкладках ЛК/Финансы/Operational/Support/CRM теперь чисто

### 3.3. Бухгалтерия — удалена ЛК-секция (stats + 3 очереди) ✅
- Удалены строки 5206-5259 в jarvis.html (USDT в очереди / Отпустить / Пополнить и отпустить + 3 таблицы очередей) — дубль ЛК Отдела
- JS (`accountingData`, `refreshAccounting`, `accountingNotes`) оставлен — может использоваться где-то ещё
- Остался: заголовок + категории kassa/suppliers/salaries/ads/sims + МАРЖА + Журнал записей

### 3.4. Голосовой звонок Часть A (внутри окна Electron) ✅
- **Топ-баннер** «🟢 В эфире · 3 участника» над `<nav class="view-tabs">`, x-show только когда `dcCurrentVoiceChannel`
- **Floating draggable окошко** (320px, position:fixed) с участниками + mute/deafen/leave/⚙
- Сворачивается в **пузырь 60×60**, позиция в `localStorage`
- Auto-open при входе в звонок, auto-close при выходе
- Использует **существующую** WebRTC-инфраструктуру: WebSocket /ws-discord, dcVoiceParticipants, joinDiscordVoice/leaveDiscordVoice/toggleMute/toggleDeafen

### 3.5. Голосовой звонок Часть B (Electron popout — вне границ окна) ✅
**Требует bump версии 2.0.2 → 2.0.3 + новый installer**
- `desktop/call-popout.html` — мини-страница UI звонка (frameless, native draggable region `-webkit-app-region: drag`)
- `desktop/call-preload.js` — contextBridge для popout
- `desktop/preload.js` — добавлен `window.pride.call.*` API в main jarvis
- `desktop/main.js` — `createCallPopoutWindow()` + 7 IPC handlers
- `desktop/package.json` — версия 2.0.3 + файлы в build.files
- `dashboard/jarvis.html` — кнопка `⇱` в floating window, IPC push state каждые 500мс
- **Архитектура:** popout НЕ держит WebRTC — он чистое UI-зеркало через IPC. Mute/leave идут в main-jarvis через IPC.

### 3.6. Панель 🚫 БЛОКИ в правой колонке ЛК Отдела ✅
- Фильтрует `this.cards` по `(c.status || '').startsWith('БЛОК')`
- Показывает: бейдж статуса, банк, ФИО, supplier, deal_id, block_amount_rub, block_note, время
- x-show только в `currentView === 'lk'`, перед Brain Notes
- Никаких новых API — используется существующий массив cards

### 3.7. КРЕДИТОВАНИЕ — Этап 1 (scaffold) ✅
**Параллельная инфраструктура к CRM поставщиков. Полная изоляция данных.**

**storage.py** — добавлены сущности:
- `credit_access_chat_id`, `credit_password_chat_id` (= 0, реальные ID в config.py)
- `credit_managers` (юристы со статистикой `{drops_total, drops_done, lks_total, lks_done}`)
- `credit_chats` (доп. чаты под конкретных менеджеров)
- `credit_drops` (анкеты, ключи `cdrp00001`...)
- `credit_drop_lks` (ЛК банков, ключи `clk00001`...)
- `credit_fsm` (FSM state для CRM-бота в credit-чатах)
- Helpers: `register_credit_manager`, `register_credit_chat`, `is_credit_chat`, `add_credit_drop`, `add_credit_drop_lk`, `update_credit_drop_lk`, `get_credit_fsm`, `bump_credit_manager_stat` и др.

**api.py** — 3 новых эндпоинта:
- `GET /api/system/credit_pending_lk` — параллель `/api/system/pending_lk`, фильтрует `credit_drop_lks`
- `GET /api/system/credit_passwords_inbox` — параллель `/api/system/passwords_inbox`
- `GET /api/system/credit_managers` — список юристов со статистикой

**crm_bot.py** — handler команды «Ассистент возьми этот чат под кредитование - менеджер @ник [как ПАРОЛИ]»:
- Triggered by `F.text.regexp(r"(?i)кредит\w*")`
- Только для CRM_OWNER_IDS (4 человека)
- Регистрирует chat + менеджера
- ⚠️ **Может не сработать** если CRM-бот не в чате (тогда срабатывает userbot handler — см. 3.7.fix)

**config.py** — добавлены `CREDIT_ACCESS_CHAT_ID = -1005116975272` и `CREDIT_PASSWORD_CHAT_ID = -1005234590907` (через env override). `storage.is_credit_chat()` читает из config приоритетно.

**dashboard/jarvis.html** — 2 новые вкладки в System:
- `systemSection === 'credit_access'` → «💳 КРЕДИТ | Доступы»
- `systemSection === 'credit_password'` → «💳 КРЕДИТ | Пароли»
- Загружают данные через `refreshCredit()` (dependent на 3 эндпоинта выше)
- Использует Linear-style карточки с border-left `#6366f1`

### 3.8. КРЕДИТОВАНИЕ — Этап 1.fix: команда в userbot.py ✅
**Проблема:** в TG бот @PrideCONTROLE_bot не был в кредитном чате — поэтому handler из 3.7 не сработал. Юзербот (всегда в чате) перехватил «Ассистент возьми этот чат...» и попытался резолвить «под» как @-mention → ошибка `Cannot find any entity corresponding to 'под'`.

**Решение:** добавлен `_maybe_handle_credit_capture(event, chat_id)` в `userbot.py` **ПЕРЕД** `_maybe_handle_takeover_command` — теперь юзербот ловит credit-команду первым и регистрирует чат.

Regex:
- `_AI_CMD_CREDIT_CAPTURE_RE = r"(?i)ассистент.*?(?:возьми|регистри|закрепи|зарегистрируй).*?кредитов\w*"`
- `_AI_CMD_CREDIT_MANAGER_RE = r"(?i)менеджер[\s,:\-—]*@?(\w{3,})"`

### 3.9. Нумерация ЛК + кнопка переноса между отделами ✅
**В API:**
- Helper `_compute_lk_slot(drop, droplk_id) -> (slot_number, slot_total)` — индекс в `drop.lk_card_ids` + 1
- Применён во всех 4 эндпоинтах (`pending_lk`, `passwords_inbox`, `credit_pending_lk`, `credit_passwords_inbox`)
- Возвращает доп. поля: `slot_number`, `slot_total`, `track` (`"supplier"` | `"credit"`), `drop_number`

**В storage:**
- `move_crm_lk_to_credit(droplk_id, manager_username)` — переносит ЛК между трекaми. Создаёт credit_drop если нет; копирует все поля; помечает `_moved_from_crm_lk`, `_moved_at`; удаляет из crm_drop_lks; меняет старого drop.status на `moved_to_credit` если пуст
- `move_credit_lk_to_crm(credit_droplk_id, owner_id=None)` — обратное

**В API:**
- `POST /api/system/lk/{id}/move_to_credit` (body: `{manager_username}`)
- `POST /api/system/credit_lk/{id}/move_to_supplier` (body: `{owner_id?}`)

**В UI:**
- CSS `.lk-slot-badge` (фиолетовый для supplier / зелёный для credit) + `.btn-move-track`
- JS `moveLkTrack(lk, target)` с prompt/confirm
- Бейджи `#N/total` + кнопки «→ 💳 в КРЕДИТ» / «→ 🤝 ПОСТАВЩИК» во всех 4 вкладках System

---

## 4. Активный план (что ещё в работе)

### 4.1. Нумерация в TG-сообщениях бота (Шаг B) ⏳
- Нужно пройтись по crm_bot.py — найти ~20 мест где формируются сообщения с упоминанием ЛК (accept_drop, post_to_pass_group, post_anketa и т.д.)
- Добавить `#slot_number/slot_total` в формат

### 4.2. КРЕДИТОВАНИЕ — Этап 2 (routing-методы) ✅
**Сделано:** auto-detect по префиксу ID (cdrp/clk → credit, остальное → crm).

**storage.py** — добавлены методы:
- `get_drop_any(drop_id)`, `get_drop_lk_any(droplk_id)` — read
- `list_drop_lks_any(drop_id=None)` — без drop_id возвращает оба склеенно
- `update_drop_any(drop_id, **fields)`, `update_drop_lk_any(droplk_id, **fields)`
- `delete_drop_lk_any(droplk_id)`, `append_drop_sms_any(droplk_id, code, time_str)`
- Helpers для credit: `delete_credit_drop_lk`, `append_credit_sms`

**crm_bot.py** — 121 вызов заменён глобально:
- `crm_storage.get_crm_drop(` → `get_drop_any(`
- `crm_storage.get_crm_drop_lk(` → `get_drop_lk_any(`
- `crm_storage.list_crm_drop_lks(` → `list_drop_lks_any(`
- `crm_storage.update_crm_drop(` → `update_drop_any(`
- `crm_storage.update_crm_drop_lk(` → `update_drop_lk_any(`
- `crm_storage.delete_crm_drop_lk(` → `delete_drop_lk_any(`
- `crm_storage.append_crm_sms(` → `append_drop_sms_any(`

**Что теперь работает в credit-чатах:**
- ✅ Все callback'и принимающие drop_id/droplk_id: `acceptdrop`, `declinedrop`, `dropdone`, `dropproblem`, `lkview`, `lkdelete`, `filldrop`, `smsadv`, `cliready`, `cligivecode` и т.д. — теперь корректно работают если ID начинается с `cdrp`/`clk`
- ✅ FillForm FSM (заполнение паролей юристом) пишет в credit_drop_lks
- ✅ SMSForm FSM пишет sms_history в credit_drop_lks
- ✅ Перенос ЛК с поставщика на юриста (Этап 1.9) + полная работа юриста с перенесённым ЛК через бота

### 4.3. КРЕДИТОВАНИЕ — Этап 3 (create новых credit_drop через бот) ⏳
**Не сделано:** `add_crm_drop` остаётся завязан на owner_id (партнёра-поставщика).

Чтобы юрист мог создать **новую** анкету через бота прямо в credit-чате (а не только импорт через перенос) — нужно:
- Развилка в `cb_newdrop` (новая анкета) и DropForm FSM: если `is_credit_chat(message.chat.id)` → `add_credit_drop(...)` (требует manager_username вместо owner_id)
- Альтернатива: новые отдельные callback'и `cnewdrop:` / `cnewlk:` (с префиксом `c`) → отдельные FSM-handler'ы для credit
- ~200-300 строк, делать аккуратно

Для **миграции существующих** ЛК уже сейчас работает кнопка «→ 💳 в КРЕДИТ» в JARVIS (Этап 1.9).

### 4.3. Operational редизайн (отложен)
- Toolbar с фильтрами (поиск ФИО/#заявки, chips банков, метод оплаты, время)
- Карточки 4-5 в ряд (компактнее)
- Секции в 2 колонки
- Linear matte стиль через класс `.op-modern` (только в Operational)

### 4.4. Support редизайн (отложен)
- Аналогично Operational, минималистичный inbox + чат

### 4.5. TRC Wallet step 4 (отложен)
- Выводы партнёрам + история транзакций + уведомления

---

## 5. Известные проблемы / технический долг

### 5.1. Сломанный git-индекс (ЕЩЁ НЕ ИСПРАВЛЕНО)
17 файлов помечены `D` (deleted) в индексе, но физически на диске лежат как `??` (untracked): userbot.py, storage.py, requirements.txt, railway.json, run_local.bat, learn.py, leo.py, memory.py, migrate_from_old_crm.py, outreach.py, knowledge/style.md, tests/test_ai_triggers.py.

**Фикс:** `git add -A && git diff --staged --stat` для проверки что ничего реально не удалено.

### 5.2. Утечка CRM-токена в публичном репо (ЕЩЁ НЕ ИСПРАВЛЕНО)
`crm_bot.py:81` → `_HARDCODED_TOKEN = "8929170452:AAE6zXBd80CL4CaKSqNgilBiMBKV1lPMCJ8"` в публичном `github.com/simba-stack/workchat-bot`. **Любой может управлять @PrideCONTROLE_bot.**

**Фикс:** revoke у @BotFather → новый токен в Railway env `CRM_BOT_TOKEN` → убрать `_HARDCODED_TOKEN`.

### 5.3. Гигантские файлы
userbot.py 442KB, crm_bot.py 238KB, api.py 216KB, storage.py 147KB, jarvis.html 483KB. При следующем рефакторинге — разбивать по доменам.

---

## 6. Бэкапы (до того как Claude правил файлы)

Все в `/sessions/.../outputs/`:
- `jarvis.before-fix.html` — до фикса модалки заявки
- `jarvis.before-commit1.html` — до сужения видимости ops-floor + Live Feed
- `jarvis.before-accounting-cleanup.html` — до удаления ЛК-секции из Бухгалтерии
- `jarvis.before-voice-banner.html` — до Части A звонков

При проблеме откатиться можно из git: `git checkout HEAD~1 -- dashboard/jarvis.html`

---

## 7. Команды деплоя

```bash
cd C:\Users\sycev\workchat-bot

# Frontend + bot изменения → Railway автодеплой
git add storage.py api.py crm_bot.py userbot.py config.py dashboard/jarvis.html
git commit -m "<сообщение>"
git push

# Electron (для звонка-popout):
git tag v2.0.3
git push origin v2.0.3
# → GH Actions соберёт .exe → electron-updater подхватит за ~30 мин
```

---

## 8. Где что лежит (быстрый референс)

| Что | Где |
|---|---|
| Код | `C:\Users\sycev\workchat-bot` |
| GitHub | https://github.com/simba-stack/workchat-bot |
| Railway env (секреты) | Railway → workchat-bot → Variables |
| Дашборд URL | `https://workchat-bot-production.up.railway.app/` |
| Локальный запуск | `run_local.bat` (заполнить env в файле) |
| Healthcheck | `GET /healthz` |
| Knowledge vault | `C:\Users\sycev\workchat-bot\knowledge\` (Obsidian) |
| Память Claude | `C:\Users\sycev\AppData\Roaming\Claude\local-agent-mode-sessions\*\spaces\*\memory\` |

---

## 9. Чек-лист после крэша сессии (как восстановить контекст)

1. Прочитать этот файл (ROADMAP.md) целиком
2. Запустить `git log --oneline -30` — увидеть какие коммиты ушли
3. Запустить `git status` + `git diff --staged` — увидеть что в индексе
4. Прочитать `memory/MEMORY.md` + `memory/project_*.md`
5. Спросить SIMBA: «На чём остановились? Раздел 4 (активный план) ROADMAP актуален?»

---

*Документ ведёт Claude после каждой задачи в Cowork-сессии. При значимом изменении — обновляется секция 3 (Хронология) или 4 (Активный план).*
