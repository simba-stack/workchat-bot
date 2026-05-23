# workchat-bot — роадмап и контекст проекта

> Живой документ. Обновляется после каждой задачи в сессии Claude — чтобы при крэше или новой сессии всё было на руках.

**Снимок:** обновлено **2026-05-23** (сессия SIMBA + Claude через Cowork).
**Что в проде сейчас (Railway active deploy):** все этапы кредитования + КУЦ MVP без AI.

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

### 4.0. КУЦ (Кружок Удостоверения Клиента) — KYC через одноразовую ссылку ✅ **В ПРОДЕ**
**Сделано MVP без AI. Задеплоено 2026-05-23 (коммит `fix(kuc): добавлен python-multipart`).**
**Verified live:**
- `GET /healthz` → `{"status":"ok","subscribers":2}` ✓
- `GET /kuc/<invalid_token>` → «Ссылка недействительна» (HTML 404) ✓

**Известный gotcha #1:** первая попытка деплоя `feat(kuc): полный...` упала с healthcheck failure через 1:33 — причина: FastAPI requires `python-multipart` для `UploadFile = File(...)`. Исправлено добавлением `python-multipart>=0.0.9` в `requirements.txt`.

**Известный gotcha #2:** в Electron desktop `window.prompt()` НЕ показывает диалог, возвращает `null` (стандартное поведение Electron). Кнопка «📹 КУЦ» вызывала `prompt()` → у пользователя «ничего не происходило». Исправлено: заменил на HTML-модалку `kucReqModalOpen` с `<textarea>`. confirm() и alert() в Electron работают, их оставили. **Правило для будущих фич: НЕ используй prompt() в jarvis.html, делай HTML-модалку.**

**Архитектура:**

```
Работник в JARVIS → жмёт «📹 КУЦ» в карточке ЛК
  → API POST /api/system/lk/{id}/kuc/request  (prompt'ит текст инструкции)
  → storage.create_kuc_request(...) генерирует uuid-токен
  → enqueue userbot команду __send_kuc_link <chat_id> <token>
  → userbot пишет в work_chat клиента: «Пройдите проверку: https://.../kuc/<token>»

Клиент открывает ссылку на телефоне
  → GET /kuc/{token} отдаёт kuc_capture.html
  → MediaRecorder API: камера + 10s запись + submit
  → POST /kuc/{token}/submit с multipart video → сохраняется в /app/data/kuc/<token>.webm
  → SSE event "kuc-submitted"

Работник видит в карточке статус «📥 кружок получен · проверить»
  → клик → modal с <video> + ✅ Одобрить / ❌ Отклонить + заметка
  → POST /api/system/kuc/{token}/decide
```

**Файлы:**
- `storage.py` — новая сущность `kuc_requests`, helpers: `create_kuc_request`, `mark_kuc_url_sent`, `mark_kuc_opened`, `mark_kuc_submitted`, `decide_kuc`, `set_kuc_ai_result`, `get_kuc_for_droplk`, `get_work_chat_for_droplk`
- `api.py` — 7 endpoints: POST /kuc/request, GET /kuc/{token} (HTML), GET /kuc/{token}/info, POST /kuc/{token}/open, POST /kuc/{token}/submit, GET /api/system/kuc/{token}/video, POST /api/system/kuc/{token}/decide, GET /api/system/kuc/list
- `dashboard/kuc_capture.html` — новая страница для клиента (camera + MediaRecorder + submit)
- `userbot.py` — handler `__send_kuc_link <chat_id> <token>` пишет ссылку в work_chat
- `dashboard/jarvis.html` — кнопка «📹 КУЦ» во всех 4 вкладках System + state badge + модалка просмотра/approve/reject

**Env переменные (опциональные):**
- `KUC_VIDEO_DIR` (default `/app/data/kuc`) — папка для видео на Railway Volume
- `KUC_MAX_BYTES` (default 20 MB) — лимит размера видео
- `KUC_BASE_URL` (default `https://workchat-bot-production.up.railway.app`) — base URL для ссылок (можно переопределить если другой домен)

**Срок жизни ссылки:** без лимита (живёт пока status != approved/rejected).

### 4.0.B. КУЦ — AI face match (Claude Vision) ⏳
**План:** при `POST /kuc/{token}/submit`:
- Извлечь один frame из видео через `ffmpeg -ss 00:00:05 -frames:v 1 frame.jpg`
- Взять `scan_file_ids[0]` (паспорт) из связанного drop
- Отправить оба в Claude Vision с промптом «Сравни лица. JSON: {score: 0-100, comment, same_person: bool}»
- Записать `kuc_request.ai_score` + `ai_comment` через `storage.set_kuc_ai_result`
- В JARVIS — показать AI score в модалке перед approve/reject

**Что нужно:** ffmpeg в Dockerfile (`apt-get install ffmpeg`), Anthropic Python SDK уже стоит.

### 4.0.C. OWNER PANEL ✅ (роли и разрешения)
**Storage:** `role_permissions: {role: {label, views, edit_actions}}` + `custom_roles: []` + 5 дефолтных ролей (owner / manager / system / accounting / operationist) с предзаполненными правами. Helpers: `list_role_permissions`, `set_role_permission`, `delete_role_permission`, `role_can_view`, `role_can_edit`, `list_all_known_views/actions`.

**API** (owner-only через `_require_owner(me)`):
- `GET /api/owner/roles` — все роли + список all_views + all_actions
- `POST /api/owner/roles` — создать/обновить (body: role, label, views, edit_actions)
- `DELETE /api/owner/roles/{role}` — удалить custom-роль (дефолтные нельзя)
- `GET /api/owner/users` — все юзеры из tg_user_info + их роли
- `POST /api/owner/users/{username}/role` — назначить роль (body: role, is_admin)

**Frontend:** новая вкладка `👑 OWNER` (x-show=role===owner) с двумя панелями:
- Роли — checkbox-чипы для каждой view/action (зелёный=включено, серый=выкл), кнопка «+ Роль» для создания custom
- Пользователи — список всех + dropdown ролей + аватары

### ⚠️ ВАЖНО ПРО Railway watchPatterns
**Если добавляешь новый статический файл в `dashboard/` — обязательно добавь его в `railway.json` → `watchPatterns`**, иначе Railway не сделает rebuild Docker-образа и отдаст **старую версию** этого файла (Docker COPY кэшируется на уровне Layer).

**Симптом:** локальный файл новый, в `git show HEAD:...` тоже новый, но прод отдаёт старый размер/контент.

**Текущий список:** `dashboard/index.html`, `dashboard/jarvis.html`, `dashboard/kuc_capture.html`, `dashboard/guest_call.html`. При добавлении новых — добавлять и сюда.

### 4.0.D. ГОСТЕВЫЕ ЗВОНКИ ✅ (Яндекс.Телемост-стиль)
**Storage:** `guest_calls: {room_id: {password, name, created_by, created_at, ended_at, max_participants, active_participants[]}}`. Helpers: `create_guest_call`, `get_guest_call`, `end_guest_call`, `add/remove_guest_participant`, `list_guest_calls`.

**API:**
- `POST /api/calls/create` (любой авторизованный) → `{room_id, password, url}`
- `GET /api/calls/list` — активные звонки
- `POST /api/calls/{id}/end` — завершить (creator или owner)
- `GET /call/{room_id}` (публичный) → отдаёт guest_call.html
- `GET /api/calls/{id}/info` (публичный) → имя+участники без пароля
- `POST /api/calls/{id}/join` (публичный) → `{name, password}` → `participant_id`
- `WebSocket /ws-guest-call?room_id&participant_id` — signaling mesh (peer-joined / peer-left / signal[ice/offer/answer])

**Frontend:**
- `dashboard/guest_call.html` — новая страница: ввод имени+пароля → getUserMedia → WebRTC mesh с STUN (Google) → видео-tiles, кнопки 🎙/📹/🖥/🚪. Screen share через `getDisplayMedia` + `replaceTrack`.
- В JARVIS → Owner Panel — кнопка «📞 Создать звонок» (модалка с url+password)

**WebRTC:** P2P mesh без SFU (до 10 человек).
- STUN: stun.l.google.com
- **TURN: openrelay.metered.ca** (бесплатный, public, поддерживает 80/443/443+tcp) — нужен для звонков через симметричный NAT. Если openrelay упадёт — заменить на свой coturn или Twilio (см. ICE_SERVERS в guest_call.html).
- iceRestart автоматический при `pc.connectionState === 'failed'`
- НЕ закрываем peer на `disconnected` (это часто временное состояние, восстанавливается)

**UI guest_call.html:**
- Адаптивный grid layout (1/2/3-4/5-6/7-9/10) под количество участников
- Аватар-плейсхолдер с инициалом пока нет video track (вместо чёрного экрана)
- Audio constraints: echoCancellation + noiseSuppression + autoGainControl
- Badges: 🔇 (muted), ⏳ (connecting), 🔄 (disconnected), ⚠ (failed)
- Speaking indicator (зелёная обводка тайла) — TODO в Этапе B (нужен Web Audio API analyser)

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

### 4.3. КРЕДИТОВАНИЕ — Этап 3 (create новых credit_drop через бот) ✅
**Сделано:** юрист в credit-чате может создавать новые анкеты через CRM-бота, они идут в `credit_drops`.

**Подход:** новый отдельный callback `cnewdrop:<manager>` + развилка в существующих handlers (НЕ дублируя FSM-state классы — DropForm используется для обоих).

**storage.py — write-routing методы:**
- `add_drop_for_chat(chat_id, fio, owner_id=None, manager_username=None, work_chat_id=None, ...)` — выбирает между `add_crm_drop` и `add_credit_drop` по `is_credit_chat(chat_id)`
- `add_drop_lk_for_drop(drop_id, bank, value, deal, owner_id=None)` — routing по префиксу drop_id (`cdrp` → credit). Для CRM сам подтягивает `owner_id` из дропа

**crm_bot.py:**
- `cmd_clients` — развилка: если `is_credit_chat(message.chat.id)` → `_show_credit_clients(message, manager)` (показывает `credit_drops` менеджера + кнопку «➕ Новая анкета (кредит)»)
- Новый `_show_credit_clients(message, manager_username)` — параллель `_show_clients`
- Новый callback `@router.callback_query(F.data.startswith("cnewdrop:"))` → `cb_credit_newdrop` — запускает DropForm FSM с `track="credit"` в FSM data
- `handle_fio` — теперь читает `track` из FSM data и вызывает `add_drop_for_chat(...)` который сам роутит
- 2 вызова `add_crm_drop_lk` → `add_drop_lk_for_drop` (через replace_all)

**Flow юриста (как работает):**
1. Зарегистрировать credit-чат: «Ассистент возьми этот чат под кредитование - менеджер @ник» (Этап 1.fix)
2. В credit-чате: `/clients` → видит свой список (пока пустой) + кнопку «➕ Новая анкета (кредит)»
3. Жмёт кнопку → вводит ФИО → создаётся `credit_drop` с префиксом `cdrp00001`
4. Дальше — точно как у поставщиков (через единые FSM-классы) — заполнение сканов, добавление ЛК, паролей, SMS-флоу
5. Все callback'и (`acceptdrop`, `lkview`, `filldrop`, `smsadv` и т.д.) — работают для credit благодаря routing-методам Этапа 2

**Что НЕ изменено в существующем flow поставщиков:**
- `cb_newdrop` остался с owner_id (просто добавлен `track="crm"` в FSM data)
- `_show_clients` без изменений
- DropForm/LKForm/FillForm/SMSForm — те же FSM-классы для обоих треков

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
