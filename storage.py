"""JSON-based persistent storage for bot state. Atomic via .tmp+os.replace."""
import json
import os
import re
import asyncio
import time
import secrets
import string
from typing import Optional

import config

_lock = asyncio.Lock()

# Чаты старше этого количества секунд удаляются при очистке
_CHAT_TTL_SEC = 30 * 24 * 3600  # 30 дней


def _gen_secret_command() -> str:
    alphabet = string.ascii_letters + string.digits
    suffix = ''.join(secrets.choice(alphabet) for _ in range(12))
    return f"admin_{suffix}"


def _norm_chat_id(cid) -> str:
    """Normalize chat_id to consistent string key.

    Telegram supergroup IDs arrive in two forms:
      - From Telethon (channel.id):  1234567890       (no prefix)
      - From aiogram events:        -1001234567890    (with -100 prefix)

    Always store the bare ID (without -100). Strip '100' only when len >= 12.
    """
    n = abs(int(cid))
    s = str(n)
    if len(s) >= 12 and s.startswith('100'):
        s = s[3:]
    return s


def _norm_deal_id(deal_id) -> str:
    """Нормализует deal_id: убирает решётку и пробелы."""
    return str(deal_id or "").lstrip("#").strip()


def _default_state() -> dict:
    return {
        "admins": [],
        "workers": list(config.DEFAULT_WORKERS),
        "welcome_message": config.DEFAULT_WELCOME,
        "welcome_entities": [],
        "cooldown_minutes": config.DEFAULT_COOLDOWN_MIN,
        "trigger_phrases": list(config.DEFAULT_TRIGGERS),
        "stats": {"total_chats_created": 0, "creations_by_user": {}},
        "user_cooldowns": {},
        "managed_chats": {},
        "admin_secret_command": "",
        "brain_chat_id": 0,
        "client_idle_minutes": 5,
        "source_stats": {},
        "user_sources": {},
        "ai_enabled": True,  # дефолт включён — иначе бот тупо молчит на новой инсталляции
        "ai_model": "",
        "ai_stats": {
            "replies_total": 0,
            "input_tokens_total": 0,
            "output_tokens_total": 0,
            "errors_total": 0,
            "skipped_worker_active": 0,
        },
        "ai_writeback_enabled": False,
        "writeback_stats": {
            "commits_total": 0,
            "skipped_total": 0,
            "errors_total": 0,
        },
        "coordination_chat_id": 0,
        "escalate_stats": {
            "calls_total": 0,
            "by_specialist": {},
            "errors_total": 0,
        },
        # deals: deal_id -> {client_username, fio, bank, amount, fee, method,
        # status, created_at, history, work_chat_id}
        # Видимость для команды — через карточку ЛК в Группе 1 (lk_group_id),
        # отдельной публикации в чате сделок больше нет.
        "deals": {},
        "deals_stats": {
            "created_total": 0,
            "by_status": {},
            "errors_total": 0,
        },
        # Чат «Бухгалтерия» (Группа 2 V2 — заявки + расчёт маржи).
        "accounting_group_id": 0,
        # Группа 1 «Личные кабинеты» V2 — анкеты ЛК.
        "lk_group_id": 0,
        # Анкеты ЛК. Ключ — card_id ("lk001"...). Структура:
        # {card_id, supplier (@username), bank, fio, price_usdt,
        #  payment_method (USDT_TRC20/GUARANTOR_BEFORE/_AFTER/_AFTER_WORK),
        #  deal_id, usdt_address, status (В_РАБОТЕ/ОТРАБОТАН/ПОПОЛНИТЬ_И_ОТПУСТИТЬ
        #  /БРАК/БЛОК/ЗАВЕРШЁН), block_amount_rub, block_note, brak_reason,
        #  client_id, client_username, work_chat_id, lk_group_msg_id,
        #  created_at, history}.
        "lk_cards": {},
        # Sequence для генерации card_id
        "lk_cards_seq": 0,
        # Заявки V2: {date_str: [{id, intake, outputs, course_withdrawal,
        # course_payout, partner_pct, computed, ts}, ...]}
        "applications_v2": {},
        # Обратный индекс client_username -> chat_id (последний по created_at).
        # Telegram username case-insensitive, ключи храним в lowercase, без @.
        "client_username_index": {},
        # Роли работников: {uname_lower: {role: str, is_admin: bool}}.
        # При создании новой work_chat юзербот приглашает каждого worker'а
        # и выдаёт ему админ-права (с rank=role в Telegram) если is_admin=True.
        "worker_roles": {},
        # Сводки массовых импортов: {msg_id: {chat_id, html, card_ids[]}}.
        # При удалении карточки из такого импорта — юзербот находит msg по
        # ключу и редактирует html (зачёркивает строку этой карточки).
        "import_summaries": {},
        # Прайс ЛК: {bank_upper: price_usdt}. Единый источник цен для
        # accounting2.lookup_pricing и AI (через knowledge/pricing.md).
        # Менятся через команду «прайс БАНК ЦЕНА» в брейн-чате.
        "pricing": {},
        # Воронка конверсии: ежедневные счётчики операций (для дашборда).
        # Структура: {YYYY-MM-DD: {starts, chats_created, chats_active,
        #   chats_junk, ip_interest, bank_interest, rs_handed, ...}}
        "funnel_stats": {},
        # Статистика по менеджерам/работникам.
        # Структура: {uname_lower: {messages, chats_touched, last_active_ts,
        #   payments_made, lk_completed, junk_handled, ...}}
        "manager_stats": {},
        # Предпочтения клиентов — память на уровне @username (а не чата).
        # Используется чтобы при перевязе НОВОГО ЛК того же клиента AI
        # уже знал прошлый метод оплаты / USDT-адрес — не спрашивал заново.
        # Структура: {uname_lower: {payment_method, usdt_address,
        #   last_updated_ts, lk_count, fio_last, bank_last}}
        "client_preferences": {},
        # Пользователи которые нажимали /start у бота. Структура:
        # {user_id: {first_name, username, first_seen_ts, last_seen_ts,
        #            entered_work_chat: bool}}
        # Нужно для рассылок: "всем", "только тем кто не вошёл в work-чат".
        "bot_users": {},
        # Очередь команд от дашборда (приходят через /api/commands).
        # Userbot периодически опрашивает, выполняет и помечает done=True.
        # Структура: [{id, ts, text, status, result, source}, ...]
        "dashboard_commands": [],
        # Антиспам теги менеджеров в work-чатах.
        # Структура: {chat_id_norm: {specialist_uname: {last_tag_ts,
        #   last_reply_ts, reason_last, tags_total, replies_total}}}
        "escalation_tags": {},
        # === Discord-like: внутренний хаб для админов ===
        # Каналы и сообщения. Голос — отдельно через WebRTC (next iteration).
        # Структура каналов: {id: {name, type ("text"|"voice"), category,
        #   topic, created_at, created_by}}
        "discord_channels": {},
        # Сообщения: [{id, channel_id, ts, author, author_avatar, text,
        #              attachments[], mentions[], reply_to}]
        "discord_messages": [],
        # Активные звонки: {channel_id: {started_at, participants[]}}
        # ⚠ Устарело: presence идёт через WebSocket-сессии в памяти api.py.
        # Здесь не пишем — только legacy.
        "discord_calls": {},
        # Профили админов из Telegram OAuth: {user_id: {username,
        # first_name, last_name, photo_url, last_seen_ts}}
        "tg_user_info": {},
        # Реакции на сообщения Discord: {message_id: {emoji: [user1, user2]}}
        "discord_reactions": {},
        # Закреплённые сообщения: {channel_id: [message_id, ...]}
        "discord_pins": {},
        # Прочитанные сообщения: {user: {channel_id: last_read_ts}}
        "discord_reads": {},
        # ==== CRM (новый бот-СRM для поставщиков, отдельный аккаунт) ====
        # Конфиг CRM-системы.
        # {admin_chat_id, password_chat_id, otr_chat_id, notify_chat_id}
        "crm_config": {},
        # Поставщики (Owner в старой модели).
        # {owner_id: {tg_user_id, username, name, joined_at, last_active_ts,
        #             total_drops, total_revenue_usd, banned_until, rating,
        #             work_chat_id}}
        "crm_owners": {},
        # Дропы (клиенты партнёров).
        # {drop_id: {owner_id, work_chat_id, fio, about, scan_file_ids[],
        #            price_usdt, status: 'draft'|'pending'|'accepted'|'done',
        #            accept_ts, send_ts, done_ts, prolit_count,
        #            admin_msg_id, owner_msg_id, lk_card_ids[]}}
        "crm_drops": {},
        # ЛК банков под дропами.
        # {droplk_id: {drop_id, owner_id, bank, value, deal,
        #              sms_history: [{code, time}], status: 'new'|'pending'|'ready'|'done',
        #              new_password, new_mail, new_number, ded_ip,
        #              ded_login, ded_pass, link_pass, msgid_pass}}
        "crm_drop_lks": {},
        # CRM-чаты — какой чат закреплён за каким owner'ом + флаги admin/password.
        # {chat_id_norm: {owner_id, is_admin, is_password, is_otr, registered_at}}
        "crm_chats": {},
        # Sequential ID counters
        "crm_owners_seq": 0,
        "crm_drops_seq": 0,
        "crm_drop_lks_seq": 0,
        # FSM-state поставщиков (action + payload).
        # {tg_user_id: {action: 'newdrop_fio'|'editlk_value'|..., data: {...}, msg_id, expires_at}}
        "crm_fsm": {},
        # Заметки от LEO (через голосовой чат или вручную через API).
        # Структура: [{id, ts, category, priority, text, source, author,
        #              tags[], synced_to_knowledge: bool, knowledge_url}]
        # Категории: fact / rule / task / idea / correction / client / deal
        "leo_notes": [],
        # ==== Outreach (рассылочный отдел) ====
        # Юзерботы для рассылки. Каждый = отдельный Telethon session.
        "outreach_bots": [],
        # Кампании рассылки.
        "outreach_campaigns": [],
        # Отправленные сообщения (для дедупликации + статистики).
        "outreach_messages": [],
        # Входящие ответы — для обработки/перевода менеджеру.
        "outreach_responses": [],
        # Временное состояние авторизации (phone -> session-data) пока юзер
        # вводит SMS-код. Очищается после успешной авторизации.
        "outreach_pending_auth": {},
        # ==== InviteWork-бот настройки (welcome + premium emoji + GIF) ====
        # file_id GIF/анимации для welcome (из Telegram, получается по reply
        # на GIF командой /setwelcomegif админом).
        "invite_welcome_gif_id": "",
        # Premium-emoji custom_emoji document_id'ы для замены текстовых эмодзи
        # в welcome. Структура: {emoji_char: document_id_str}.
        # Например {'🔥': '5462863737368090301', '🤝': '5462863737368090302'}.
        # Без премиум-аккаунта боты могут их пересылать если document_id
        # известен (полученный от premium-юзера).
        "invite_premium_emoji": {},
        # Текст разделов welcome (для редактирования без перезаписи кода).
        "invite_jobs_text": "",

        # ==== OWNER PANEL: разрешения по ролям + custom roles ====
        # Структура: {role_name: {label, views: [view_id, ...], edit_actions: [action_id, ...]}}
        # views — список вкладок navigation которые видит роль. "*" = все.
        # edit_actions — список действий (request_kuc, decide_kuc, move_lk_track, и т.д.). "*" = все.
        # Дефолты создаются в _ensure_default_role_permissions() при первом обращении.
        "role_permissions": {},
        # Доп. роли (custom — созданы owner'ом сверх готовых owner/manager/system/accounting/operationist).
        # Список имён + краткое описание (нужно чтобы UI знал какие роли можно назначать).
        "custom_roles": [],

        # ==== GUEST CALLS (звонки по одноразовой ссылке — типа Яндекс.Телемост) ====
        # Структура: {room_id: {
        #   room_id, password, name (комната), created_by (username),
        #   created_at, ended_at, max_participants,
        #   active_participants: [{participant_id, name, joined_at}],
        # }}
        # Без TTL — комната живёт пока owner не завершит (или процесс упадёт — тогда state.json restore).
        "guest_calls": {},

        # ==== KUC (Кружок Удостоверения Клиента — KYC через одноразовую ссылку) ====
        # Структура: {token: {
        #   token, droplk_id, work_chat_id, requested_by, request_text,
        #   created_at, url_sent_at, used_at, submitted_at,
        #   status: 'pending' | 'submitted' | 'approved' | 'rejected',
        #   video_file_path (на Railway Volume), video_size_bytes, video_mime,
        #   ai_score (0-100, после AI-этапа), ai_comment,
        #   decision_by, decision_at, decision_note,
        # }}
        # Token = uuid4().hex[:24]. Без TTL (живёт пока не approved/rejected).
        "kuc_requests": {},

        # ==== OUTSOURCE (Аутсорс — маркетплейс ЛК для управляющих) ====
        # Идея: владелец передаёт ЛК в "лавку PRIDE" — управляющие платят взнос
        # и берут ЛК под управление. Параллельно CRM поставщиков и Кредитования.
        # В будущем: оплата (TRC20 wallet) + подписка на ЛК + SMS API для входящих кодов.
        "outsource_managers": {},   # {username_lower: {tg_user_id, first_seen_ts, last_active_ts,
                                    #   wallet_balance_usdt, paid_total_usdt,
                                    #   stats: {drops_total, drops_done, lks_total, lks_done}}}
        "outsource_chats": {},      # {chat_id_norm: {manager_username, is_access, is_password,
                                    #   registered_at, registered_by_owner_id}}
        "outsource_drops": {},      # анкеты (зеркало credit_drops), prefix 'odrp'
        "outsource_drop_lks": {},   # ЛК (зеркало credit_drop_lks), prefix 'olk'
        "outsource_drops_seq": 0,
        "outsource_drop_lks_seq": 0,
        "outsource_fsm": {},
        # ==== СВЯЗКИ ЛК (bundle) — продаются одним пакетом ====
        # {bundle_id: {id, name, list_price_usdt, lk_ids[], in_pool, manager_username,
        #              bought_at, created_at, created_by}}
        # Покупка bundle = списываем list_price_usdt + всем ЛК ставим manager + in_pool=False
        "outsource_bundles": {},
        "outsource_bundles_seq": 0,
        # ==== ОПЛАТА (TRC20 USDT) ====
        # Корп-кошелёк куда юзеры шлют USDT TRC20. Задаётся через JARVIS Settings.
        "outsource_corp_wallet_trc20": "",
        # last_processed_block_ts — timestamp последней обработанной транзакции (ms)
        # чтобы не перебирать всю историю кошелька с начала.
        "outsource_tron_last_ts_ms": 0,
        # {request_id: {username, base_amount, unique_amount, status, created_at,
        #               expires_at, txid, credited_at, credited_block_ts}}
        # status: 'pending' | 'credited' | 'expired' | 'rejected'
        "outsource_topup_requests": {},
        "outsource_topup_seq": 0,
        # ==== Редактируемые тексты @marketplace_PRIDE_BOT ====
        # {key: value} — override дефолтов из outsource_bot_texts.DEFAULT_TEXTS
        # Если key отсутствует — используется дефолт.
        "outsource_bot_texts": {},
        # ==== ОЧЕРЕДЬ АВТО-ПОЧИНКИ ПРАВ В ЧАТАХ ====
        # CRM-бот добавляет chat_id когда падает с CHAT_RESTRICTED / kicked.
        # Userbot-воркер (см. userbot.py) подбирает и пробует:
        # 1. Пригласить CRM-бота в чат
        # 2. Сделать его админом с правами posting+pin+delete
        # 3. При успехе — удаляет из очереди
        # {chat_id_str: {reason, added_at, attempts, last_error, last_attempt_at}}
        "pending_chat_fixes": {},
        # ==== ОТКУПЫ — обмен RUB → USDT TRC20 ====
        # Клиенты в payments_chat_id пишут «дай 100К СБП», бот ловит, считает,
        # форвардит в outkup_team_chat_id. Откупщики выдают реквизиты вручную.
        "outkup_settings": {
            "rate_rub_per_usdt": 100.0,        # 100 рублей за 1 USDT (старт)
            "payments_chat_id": 0,             # чат куда пишут клиенты
            "outkup_team_chat_id": 0,          # чат откупщиков (заявки приходят сюда)
            "min_amount_rub": 5000,            # минимальная сумма заявки
            "max_amount_rub": 5000000,         # максимум
            "enabled": True,                   # глобальный тумблер модуля
        },
        # {order_id: {client_chat_id, client_user_id, client_username,
        #             client_msg_id, amount_rub, method, calculated_usdt, rate,
        #             status, created_at, assigned_to, assigned_at,
        #             paid_at, completed_at, cancelled_at, txid, notes}}
        # status: pending_confirm | awaiting_payment | paid | completed | cancelled
        # method: sbp | card | full
        "outkup_orders": {},
        "outkup_orders_seq": 0,
        # ==== CREDIT (Кредитование — параллельно CRM поставщиков) ====
        # Юристы готовят счета к подаче заявки на кредит.
        # Структура зеркалит crm_* для поставщиков, но изолирована.
        # ID групп Telegram (по умолчанию 0 — задать через env или /admin).
        "credit_access_chat_id": 0,    # «КРЕДИТОВАНИЕ — ДОСТУПЫ» — приёмка анкет
        "credit_password_chat_id": 0,  # «КРЕДИТОВАНИЕ — ПАРОЛИ» — RDP + пароли ЛК
        # Менеджеры (юристы). Ключ — username_lower (без @).
        # {username_lower: {tg_user_id, first_seen_ts, last_active_ts,
        #   stats: {drops_total, drops_done, lks_total, lks_done}}}
        "credit_managers": {},
        # Доп. чаты кредитования, привязанные к менеджерам.
        # {chat_id_norm: {manager_username, is_access, is_password,
        #   registered_at, registered_by_owner_id}}
        # Помимо двух главных захардкоженных есть и доп.чаты под конкретных менеджеров.
        "credit_chats": {},
        # Анкеты кредитования (по аналогии с crm_drops).
        # {credit_drop_id: {chat_id, manager_username, fio, about, scan_file_ids[],
        #   status: 'draft'|'pending'|'accepted'|'done',
        #   accept_ts, send_ts, done_ts, admin_msg_id, lk_card_ids[]}}
        "credit_drops": {},
        # ЛК банков под анкетами кредитования (зеркало crm_drop_lks).
        # {credit_droplk_id: {credit_drop_id, manager_username, bank, value, deal,
        #   sms_history: [{code, time}], sms_stage,
        #   new_login, new_password, new_mail, new_number, code_word,
        #   ded_ip, ded_login, ded_pass, ded_location, msgid_pass,
        #   sms_tracker_msg_id, created_at, updated_at}}
        "credit_drop_lks": {},
        # Sequential ID counters
        "credit_drops_seq": 0,
        "credit_drop_lks_seq": 0,
        # FSM для CRM-бота в credit-чатах (зеркало crm_fsm).
        "credit_fsm": {},
    }


class Storage:
    def __init__(self, path: str):
        self.path = path
        self.state = _default_state()
        d = os.path.dirname(path) or "."
        os.makedirs(d, exist_ok=True)
        # Сразу проверяем что путь похож на persistent volume.
        # На Railway по умолчанию /app/data, и если volume не настроен —
        # данные пропадают при каждом редеплое.
        try:
            self._persistence_check()
        except Exception as e:
            print(f"[storage] persistence_check error: {e}")

    def _persistence_check(self):
        """Печатает предупреждение если storage не на persistent volume."""
        suspicious = (
            "/tmp/" in self.path
            or self.path.startswith("./")
            or self.path == "state.json"
        )
        if suspicious:
            print(
                "[storage] ⚠️  STORAGE_PATH=%s выглядит непостоянным. "
                "На Railway данные пропадут при каждом редеплое! "
                "Создайте Volume через Railway → Settings → Volumes, "
                "смонтируйте его в /app/data и поставьте "
                "STORAGE_PATH=/app/data/state.json в env." % self.path
            )

    def reload_sync(self) -> bool:
        """Синхронный hot-reload state.json — для дашборд-API.
        Bot.py и userbot.py живут разными процессами и каждый держит свой
        in-memory storage.state. Когда userbot пишет в файл, bot.py об этом
        не знает. Дашборд читает через bot.py → видит устаревшее.
        Вызывай эту функцию из API endpoints чтоб подтянуть свежее с диска.
        Возвращает True если что-то перечитал."""
        if not os.path.exists(self.path):
            return False
        try:
            mtime = os.path.getmtime(self.path)
        except OSError:
            return False
        # Не перечитываем если файл не менялся с прошлого раза
        last = getattr(self, "_last_reload_mtime", 0)
        if mtime <= last:
            return False
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                loaded = json.load(f)
        except Exception as e:
            print(f"[storage] reload_sync failed: {e}")
            return False
        # Миграция V2: те же ключи что в load()
        for legacy_key in (
            "accounts_group_id", "pending_accounts_posts",
            "accounting", "deals_group_id",
        ):
            loaded.pop(legacy_key, None)
        for d in (loaded.get("deals") or {}).values():
            if isinstance(d, dict):
                d.pop("accounts_group_msg_id", None)
                d.pop("deals_group_msg_id", None)
        defaults = _default_state()
        for k, v in defaults.items():
            if k not in loaded:
                loaded[k] = v
        self.state = loaded
        self._last_reload_mtime = mtime
        return True

    async def load(self):
        async with _lock:
            if os.path.exists(self.path):
                try:
                    with open(self.path, "r", encoding="utf-8") as f:
                        loaded = json.load(f)
                    # Миграция V2: дропаем ключи старой схемы
                    # (accounts_group_id, pending_accounts_posts, accounting V1,
                    # deals_group_id — устарела, видимость теперь через Группу 1 ЛК)
                    for legacy_key in (
                        "accounts_group_id",
                        "pending_accounts_posts",
                        "accounting",
                        "deals_group_id",
                    ):
                        loaded.pop(legacy_key, None)
                    # Из каждой сделки убираем поля устаревших публикаций.
                    for d in (loaded.get("deals") or {}).values():
                        if isinstance(d, dict):
                            d.pop("accounts_group_msg_id", None)
                            d.pop("deals_group_msg_id", None)
                    defaults = _default_state()
                    for k, v in defaults.items():
                        if k not in loaded:
                            loaded[k] = v
                    self.state = loaded
                except Exception as e:
                    print(f"[storage] load failed, using defaults: {e}")
            if not self.state.get("admin_secret_command"):
                self.state["admin_secret_command"] = _gen_secret_command()
            if config.ADMIN_ID and config.ADMIN_ID not in self.state["admins"]:
                self.state["admins"].append(config.ADMIN_ID)
            await self._save_unlocked()

    async def save(self):
        """Публичный thread-safe save (для API endpoints которые меняют state напрямую)."""
        async with _lock:
            await self._save_unlocked()

    async def set_state_fields(self, **fields):
        """Помощник: одной операцией обновить несколько полей в state + save."""
        async with _lock:
            for k, v in fields.items():
                self.state[k] = v
            await self._save_unlocked()

    async def _save_unlocked(self):
        """Async-safe save: вся I/O работа уезжает в thread executor,
        чтобы event loop оставался отзывчивым (Railway healthcheck +
        Telegram polling не должны блокироваться на json.dump)."""
        try:
            # Сериализация в строку — синхронная, но быстрая (<50ms даже для МБ-state).
            # Делаем под caller-holding _lock, поэтому self.state не мутируется параллельно.
            snapshot = json.dumps(self.state, ensure_ascii=False, indent=2)
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, self._do_write_sync, snapshot)
        except Exception as e:
            print(f"[storage] save failed: {e}")

    def _do_write_sync(self, snapshot_str: str):
        """Sync atomic write. Выполняется в thread executor — НЕ блокирует
        event loop. Атомарно: .tmp → os.replace + .bak от предыдущего state."""
        try:
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(snapshot_str)
            if os.path.exists(self.path):
                try:
                    os.replace(self.path, self.path + ".bak")
                except Exception:
                    pass
            os.replace(tmp, self.path)
        except Exception as e:
            print(f"[storage] sync write failed: {e}")

    async def save(self):
        async with _lock:
            await self._save_unlocked()

    def is_admin(self, user_id: int) -> bool:
        return user_id in self.state["admins"]

    async def add_admin(self, user_id: int):
        async with _lock:
            if user_id not in self.state["admins"]:
                self.state["admins"].append(user_id)
            await self._save_unlocked()

    async def remove_admin(self, user_id: int):
        async with _lock:
            if user_id in self.state["admins"]:
                self.state["admins"].remove(user_id)
            await self._save_unlocked()

    def get_admins(self):
        return list(self.state["admins"])

    def get_workers(self):
        return list(self.state["workers"])

    async def add_worker(self, username: str):
        username = username.lstrip("@").strip()
        async with _lock:
            if username and username not in self.state["workers"]:
                self.state["workers"].append(username)
            await self._save_unlocked()

    async def remove_worker(self, username: str):
        username = username.lstrip("@").strip()
        async with _lock:
            if username in self.state["workers"]:
                self.state["workers"].remove(username)
            # Заодно чистим роли — чтобы worker_roles не разрастался
            roles = self.state.setdefault("worker_roles", {})
            roles.pop(username.lower(), None)
            await self._save_unlocked()

    # === Роли работников ===
    # Для каждого worker'а можно задать роль (например "Менеджер", "Оператор")
    # и флаг is_admin. При создании новой work_chat юзербот приглашает worker'а
    # и выдаёт ему админ-права с rank=role если is_admin=True. Если роли нет —
    # worker добавляется обычным участником.

    def get_worker_role(self, username: str) -> dict:
        """Возвращает {role: str, is_admin: bool} для worker'а по username,
        либо пустой dict если роль не задана."""
        if not username:
            return {}
        key = username.lstrip("@").lower().strip()
        if not key:
            return {}
        roles = self.state.get("worker_roles") or {}
        return dict(roles.get(key) or {})

    def list_worker_roles(self) -> dict:
        """Возвращает копию всего worker_roles dict (uname_lower → {role, is_admin})."""
        return dict(self.state.get("worker_roles") or {})

    async def set_worker_role(self, username: str, role: str, is_admin: bool = False):
        """Задаёт роль и админ-флаг для worker'а. Если worker'а не было в
        списке — добавляет (worker_roles работает только с членами workers)."""
        clean = username.lstrip("@").strip()
        if not clean:
            return False
        key = clean.lower()
        async with _lock:
            # Гарантируем что worker в списке
            if clean not in self.state["workers"]:
                self.state["workers"].append(clean)
            roles = self.state.setdefault("worker_roles", {})
            roles[key] = {
                "role": (role or "").strip()[:16] or "Сотрудник",
                "is_admin": bool(is_admin),
            }
            await self._save_unlocked()
            return True

    async def remove_worker_role(self, username: str):
        """Удаляет только роль (worker остаётся в списке)."""
        if not username:
            return
        key = username.lstrip("@").lower().strip()
        if not key:
            return
        async with _lock:
            roles = self.state.setdefault("worker_roles", {})
            roles.pop(key, None)
            await self._save_unlocked()

    def get_welcome(self) -> str:
        return self.state["welcome_message"]

    def get_welcome_entities(self) -> list:
        return list(self.state.get("welcome_entities") or [])

    async def set_welcome(self, text: str, entities: Optional[list] = None):
        async with _lock:
            self.state["welcome_message"] = text
            self.state["welcome_entities"] = entities or []
            await self._save_unlocked()

    def get_cooldown_minutes(self) -> int:
        return self.state["cooldown_minutes"]

    async def set_cooldown_minutes(self, minutes: int):
        async with _lock:
            self.state["cooldown_minutes"] = minutes
            await self._save_unlocked()

    def check_cooldown(self, user_id: int) -> Optional[int]:
        last = self.state["user_cooldowns"].get(str(user_id))
        if not last:
            return None
        cd_sec = self.state["cooldown_minutes"] * 60
        elapsed = time.time() - last
        if elapsed < cd_sec:
            return int(cd_sec - elapsed)
        return None

    async def mark_creation(self, user_id: int):
        async with _lock:
            self.state["user_cooldowns"][str(user_id)] = time.time()
            self.state["stats"]["total_chats_created"] += 1
            uid = str(user_id)
            cur = self.state["stats"]["creations_by_user"].get(uid, 0)
            self.state["stats"]["creations_by_user"][uid] = cur + 1
            await self._save_unlocked()

    def get_triggers(self):
        return list(self.state["trigger_phrases"])

    # ---- InviteWork-бот: welcome GIF + premium emoji ----
    def get_invite_welcome_gif(self) -> str:
        return (self.state.get("invite_welcome_gif_id") or "").strip()

    async def set_invite_welcome_gif(self, file_id: str):
        async with _lock:
            self.state["invite_welcome_gif_id"] = (file_id or "").strip()
            await self._save_unlocked()

    def get_invite_premium_emoji(self) -> dict:
        return dict(self.state.get("invite_premium_emoji") or {})

    async def set_invite_premium_emoji(self, mapping: dict):
        async with _lock:
            self.state["invite_premium_emoji"] = {
                str(k): str(v) for k, v in (mapping or {}).items() if k and v
            }
            await self._save_unlocked()

    async def set_invite_premium_emoji_one(self, emoji: str, document_id: str):
        async with _lock:
            m = dict(self.state.get("invite_premium_emoji") or {})
            if document_id and document_id != "-":
                m[emoji] = document_id
            else:
                m.pop(emoji, None)
            self.state["invite_premium_emoji"] = m
            await self._save_unlocked()

    def get_invite_jobs_text(self) -> str:
        return (self.state.get("invite_jobs_text") or "").strip() or (
            "💼 <b>Открытые вакансии:</b>\n\n"
            "🟢 <b>Менеджер ИП-направления</b> — работа с поставщиками, опыт от 6 мес.\n"
            "🟢 <b>Оператор перевязки</b> — техническая часть, обучаем.\n\n"
            "📩 Резюме и вопросы: пиши <b>SIMBA</b> в личку."
        )

    async def set_invite_jobs_text(self, text: str):
        async with _lock:
            self.state["invite_jobs_text"] = text or ""
            await self._save_unlocked()

    def _username_index_set_unlocked(self, username: str, chat_id) -> bool:
        """Кладёт chat_id в обратный индекс client_username_index по lower-key.
        Если запись уже была — побеждает та, у которой managed_chat свежее
        (по created_at). Возвращает True если индекс обновился."""
        if not username:
            return False
        key_uname = username.lstrip("@").lower().strip()
        if not key_uname:
            return False
        index = self.state.setdefault("client_username_index", {})
        new_key = _norm_chat_id(chat_id)
        if not new_key:
            return False
        existing = index.get(key_uname)
        if existing == new_key:
            return False
        # Сравниваем created_at — оставляем самую свежую беседу.
        managed = self.state.get("managed_chats", {})
        new_at = (managed.get(new_key) or {}).get("created_at", 0)
        old_at = (managed.get(existing) or {}).get("created_at", 0) if existing else -1
        if new_at >= old_at:
            index[key_uname] = new_key
            return True
        return False

    async def update_client_username(self, chat_id, username: str) -> bool:
        """Обновляет client_username в managed_chats[chat_id] и обратный индекс.
        Используется юзерботом при /sync_clients и при первом сообщении
        клиента в managed_chat (если username пустой/устарел)."""
        if not username:
            return False
        clean = username.lstrip("@").strip()
        if not clean:
            return False
        key = _norm_chat_id(chat_id)
        async with _lock:
            info = self.state["managed_chats"].get(key)
            if info is None:
                return False
            changed = False
            if (info.get("client_username") or "") != clean:
                info["client_username"] = clean
                changed = True
            if self._username_index_set_unlocked(clean, key):
                changed = True
            if changed:
                await self._save_unlocked()
            return changed

    def find_chat_by_client_username(self, username: str):
        """Возвращает chat_id (нормализованный ключ managed_chats) клиента
        по @username, либо None. Поиск через обратный индекс."""
        if not username:
            return None
        key_uname = username.lstrip("@").lower().strip()
        if not key_uname:
            return None
        index = self.state.get("client_username_index") or {}
        return index.get(key_uname)

    def get_client_username_index(self) -> dict:
        return dict(self.state.get("client_username_index") or {})

    async def register_chat(self, chat_id, client_id: int, client_name: str, client_username: str = ""):
        key = _norm_chat_id(chat_id)
        clean_uname = (client_username or "").lstrip("@").strip()
        async with _lock:
            self.state["managed_chats"][key] = {
                "client_id": client_id,
                "client_name": client_name,
                "client_username": clean_uname,
                "created_at": time.time(),
                "welcome_sent": False,
                # Метод оплаты + USDT адрес — заполняются AI через
                # tool set_payment_method, когда клиент сделал выбор.
                "payment_method": "",
                "usdt_address": "",
            }
            if clean_uname:
                self._username_index_set_unlocked(clean_uname, key)
            await self._save_unlocked()

    async def set_chat_payment_info(
        self, chat_id, method: str = "", usdt_address: str = "",
        client_username: str = "",
    ) -> bool:
        """Запоминает метод оплаты (USDT_TRC20/GUARANTOR), USDT-адрес и
        username клиента для managed-чата. Используется юзерботом в
        перевяз-форварде когда сделки в storage ещё нет."""
        key = _norm_chat_id(chat_id)
        async with _lock:
            info = self.state["managed_chats"].get(key)
            if info is None:
                return False
            if method:
                info["payment_method"] = (method or "").upper()
            if usdt_address:
                info["usdt_address"] = usdt_address.strip()
            if client_username:
                clean = client_username.lstrip("@").strip()
                info["client_username"] = clean
                if clean:
                    self._username_index_set_unlocked(clean, key)
            await self._save_unlocked()
            return True

    async def set_pending_perevyaz(self, chat_id, bank: str = "", fio: str = ""):
        """Сохраняем bank+fio когда перевязка пришла, а метод оплаты ещё не
        задан. Как только клиент назовёт метод — заберём pending и создадим
        карточку без повторного перевязного события."""
        key = _norm_chat_id(chat_id)
        async with _lock:
            info = self.state["managed_chats"].get(key)
            if info is None:
                return False
            info["pending_perevyaz"] = {
                "bank": (bank or "").strip(),
                "fio": (fio or "").strip(),
                "ts": time.time(),
            }
            await self._save_unlocked()
            return True

    def get_pending_perevyaz(self, chat_id) -> dict:
        info = self.state["managed_chats"].get(_norm_chat_id(chat_id)) or {}
        return dict(info.get("pending_perevyaz") or {})

    async def pop_pending_perevyaz(self, chat_id) -> dict:
        key = _norm_chat_id(chat_id)
        async with _lock:
            info = self.state["managed_chats"].get(key)
            if info is None:
                return {}
            pending = info.pop("pending_perevyaz", None) or {}
            if pending:
                await self._save_unlocked()
            return dict(pending)

    def get_chat_info(self, chat_id) -> Optional[dict]:
        return self.state["managed_chats"].get(_norm_chat_id(chat_id))

    def get_managed_chat_ids(self) -> list:
        return list(self.state.get("managed_chats", {}).keys())

    # === SUPPORT / HELPDESK (Phase 1: триггер «позвать оператора») ===

    def get_support_state(self, chat_id) -> dict:
        """Возвращает поддержку-состояние чата (status, dept, assigned_to, ratings)."""
        info = self.get_chat_info(chat_id) or {}
        return dict(info.get("support") or {})

    async def set_support_state(self, chat_id, **fields):
        """Обновляет support-state чата. Поля:
          status: 'idle' | 'operator_requested' | 'in_progress' | 'closed'
          department: 'managers' | 'system' | 'accounting'
          assigned_to: int (tg_user_id менеджера)
          opened_at, closed_at, rating, last_unread_count
        """
        key = _norm_chat_id(chat_id)
        async with _lock:
            info = self.state["managed_chats"].get(key)
            if info is None:
                return False
            sup = info.setdefault("support", {})
            for k, v in fields.items():
                sup[k] = v
            await self._save_unlocked()
            return True

    def list_support_inbox(
        self, status: Optional[str] = None,
        department: Optional[str] = None,
        assigned_to: Optional[int] = None,
    ) -> list:
        """Возвращает список чатов с support-состоянием (для inbox дашборда)."""
        out = []
        for key, info in (self.state.get("managed_chats") or {}).items():
            sup = info.get("support") or {}
            sup_status = sup.get("status") or "idle"
            if status and sup_status != status:
                continue
            if department and sup.get("department") != department:
                continue
            if assigned_to and sup.get("assigned_to") != assigned_to:
                continue
            out.append({
                "chat_id": key,
                "client_name": info.get("client_name") or "",
                "client_username": info.get("client_username") or "",
                "client_id": info.get("client_id") or 0,
                "support": dict(sup),
                "last_message_at": info.get("last_message_at") or 0,
            })
        # Свежие сверху
        out.sort(key=lambda x: x.get("last_message_at") or 0, reverse=True)
        return out

    async def support_take(self, chat_id, manager_uid: int,
                           department: str = "managers") -> bool:
        """Менеджер берёт чат на себя: status=in_progress."""
        return await self.set_support_state(
            chat_id,
            status="in_progress",
            assigned_to=int(manager_uid),
            department=department,
            taken_at=time.time(),
        )

    async def support_release(self, chat_id) -> bool:
        """Менеджер отдаёт чат обратно AI. status=idle, очищаем assigned_to."""
        return await self.set_support_state(
            chat_id, status="idle", assigned_to=0,
            released_at=time.time(),
        )

    async def support_close(self, chat_id, rating: int = 0) -> bool:
        """Закрытие чата с опциональной оценкой."""
        return await self.set_support_state(
            chat_id, status="closed", rating=int(rating),
            closed_at=time.time(),
        )

    async def support_transfer(self, chat_id, department: str,
                                from_manager: int = 0) -> bool:
        """Передать чат в другое подразделение. Снимаем assigned_to."""
        return await self.set_support_state(
            chat_id, status="operator_requested",
            department=department, assigned_to=0,
            transferred_at=time.time(),
            transferred_from=int(from_manager),
        )

    async def bump_ai_reply_count(self, chat_id):
        """Инкрементирует счётчик AI-ответов в чате — для логики первого/второго ответа."""
        key = _norm_chat_id(chat_id)
        async with _lock:
            info = self.state["managed_chats"].get(key)
            if info is None:
                return False
            info["ai_reply_count"] = int(info.get("ai_reply_count") or 0) + 1
            await self._save_unlocked()
            return True

    async def reset_ai_reply_count(self, chat_id):
        """Сбрасывает счётчик. Используется после close/reset диалога."""
        key = _norm_chat_id(chat_id)
        async with _lock:
            info = self.state["managed_chats"].get(key)
            if info is None:
                return False
            info["ai_reply_count"] = 0
            await self._save_unlocked()
            return True

    async def bump_last_message_ts(self, chat_id, ts: Optional[float] = None):
        """Обновляет timestamp последнего сообщения в чате (для сортировки inbox)."""
        key = _norm_chat_id(chat_id)
        async with _lock:
            info = self.state["managed_chats"].get(key)
            if info:
                info["last_message_at"] = float(ts or time.time())
                await self._save_unlocked()

    # === WORKER SESSIONS (per-manager Telethon sessions) ===

    def get_worker_session(self, manager_uid: int) -> Optional[dict]:
        """Возвращает данные сессии менеджера: {string_session, phone, connected_at}.
        string_session возвращается зашифрованной — для расшифровки нужен SESSION_SECRET."""
        sessions = self.state.get("worker_sessions") or {}
        return sessions.get(str(int(manager_uid)))

    async def set_worker_session(self, manager_uid: int, encrypted_session: str,
                                  phone: str = ""):
        """Сохраняет зашифрованную StringSession менеджера в storage."""
        async with _lock:
            sessions = self.state.setdefault("worker_sessions", {})
            sessions[str(int(manager_uid))] = {
                "string_session": encrypted_session,
                "phone": phone or "",
                "connected_at": time.time(),
                "last_active": time.time(),
            }
            await self._save_unlocked()

    async def remove_worker_session(self, manager_uid: int) -> bool:
        async with _lock:
            sessions = self.state.setdefault("worker_sessions", {})
            if str(int(manager_uid)) in sessions:
                del sessions[str(int(manager_uid))]
                await self._save_unlocked()
                return True
            return False

    def list_worker_sessions(self) -> dict:
        return dict(self.state.get("worker_sessions") or {})

    async def mark_welcome_sent(self, chat_id):
        key = _norm_chat_id(chat_id)
        async with _lock:
            info = self.state["managed_chats"].get(key)
            if info:
                info["welcome_sent"] = True
            await self._save_unlocked()

    def was_assistant_hint_sent(self, chat_id) -> bool:
        """True если в этом чате уже отправляли подсказку «напиши Ассистент»."""
        key = _norm_chat_id(chat_id)
        info = self.state.get("managed_chats", {}).get(key) or {}
        return bool(info.get("assistant_hint_sent"))

    async def mark_assistant_hint_sent(self, chat_id) -> bool:
        """Помечает, что подсказка «напишите Ассистент» отправлена в чат.
        Возвращает True если флаг был обновлён (False — уже стоял)."""
        key = _norm_chat_id(chat_id)
        async with _lock:
            info = self.state.get("managed_chats", {}).get(key)
            if info is None:
                return False
            if info.get("assistant_hint_sent"):
                return False
            info["assistant_hint_sent"] = True
            info["assistant_hint_sent_at"] = time.time()
            await self._save_unlocked()
            return True

    async def bump_ai_relevance_stats(
        self, skipped: int = 0, responded: int = 0,
    ):
        """Метрика классификатора релевантности."""
        async with _lock:
            stats = self.state.setdefault("ai_stats", {})
            if skipped:
                stats["relevance_skipped"] = int(stats.get("relevance_skipped", 0)) + skipped
            if responded:
                stats["relevance_responded"] = int(stats.get("relevance_responded", 0)) + responded
            await self._save_unlocked()

    # === Антиспам тегов менеджеров ===

    def get_escalation_tag_state(self, chat_id, specialist: str) -> dict:
        """Состояние тега менеджера в конкретном чате."""
        key_chat = _norm_chat_id(chat_id)
        uname = (specialist or "").lstrip("@").lower().strip()
        per_chat = (self.state.get("escalation_tags") or {}).get(key_chat) or {}
        return dict(per_chat.get(uname) or {})

    async def can_tag_specialist(
        self, chat_id, specialist: str, cooldown_sec: int = 300,
    ) -> tuple[bool, str]:
        """Можно ли тегать менеджера в этом чате прямо сейчас?

        Правила (строгие):
        1. Менеджер ответил после последнего тега → нельзя (он уже здесь)
        2. С последнего тега прошло < cooldown_sec (5 мин) → нельзя
        Returns: (can_tag, reason). reason описывает почему отказ.
        """
        st = self.get_escalation_tag_state(chat_id, specialist)
        if not st:
            return (True, "")
        last_tag_ts = float(st.get("last_tag_ts") or 0)
        last_reply_ts = float(st.get("last_reply_ts") or 0)
        now = time.time()
        if last_reply_ts > last_tag_ts:
            return (False, f"manager already replied {int(now - last_reply_ts)}s ago")
        elapsed = now - last_tag_ts
        if elapsed < cooldown_sec:
            return (False, f"cooldown {int(cooldown_sec - elapsed)}s left")
        return (True, "")

    async def record_specialist_tag(
        self, chat_id, specialist: str, reason: str = "",
    ):
        """Фиксирует факт тега менеджера в чате."""
        key_chat = _norm_chat_id(chat_id)
        uname = (specialist or "").lstrip("@").lower().strip()
        if not uname:
            return
        async with _lock:
            tags = self.state.setdefault("escalation_tags", {})
            per_chat = tags.setdefault(key_chat, {})
            entry = per_chat.setdefault(uname, {})
            entry["last_tag_ts"] = time.time()
            entry["reason_last"] = (reason or "")[:200]
            entry["tags_total"] = int(entry.get("tags_total", 0)) + 1
            await self._save_unlocked()

    async def record_specialist_reply(self, chat_id, specialist: str):
        """Фиксирует факт ответа менеджера в чате — сбрасывает cooldown."""
        key_chat = _norm_chat_id(chat_id)
        uname = (specialist or "").lstrip("@").lower().strip()
        if not uname:
            return
        async with _lock:
            tags = self.state.setdefault("escalation_tags", {})
            per_chat = tags.setdefault(key_chat, {})
            entry = per_chat.setdefault(uname, {})
            entry["last_reply_ts"] = time.time()
            entry["replies_total"] = int(entry.get("replies_total", 0)) + 1
            await self._save_unlocked()

    # === Discord-like хаб для админов ===

    async def add_discord_channel(
        self, name: str, ch_type: str = "text", category: str = "general",
        topic: str = "", created_by: str = "",
    ) -> str:
        """Создать канал. Возвращает channel_id."""
        cid = f"ch{int(time.time() * 1000)}"
        async with _lock:
            chs = self.state.setdefault("discord_channels", {})
            chs[cid] = {
                "id": cid,
                "name": (name or "новый").strip(),
                "type": ch_type if ch_type in ("text", "voice") else "text",
                "category": (category or "general").strip(),
                "topic": (topic or "").strip(),
                "created_at": time.time(),
                "created_by": (created_by or "").lstrip("@"),
            }
            await self._save_unlocked()
            return cid

    def list_discord_channels(self) -> list:
        chs = (self.state.get("discord_channels") or {}).values()
        return sorted(chs, key=lambda c: (c.get("category", ""), c.get("name", "")))

    async def delete_discord_channel(self, channel_id: str) -> bool:
        async with _lock:
            chs = self.state.get("discord_channels") or {}
            if channel_id not in chs:
                return False
            del chs[channel_id]
            # Удалим сообщения этого канала
            msgs = self.state.get("discord_messages") or []
            self.state["discord_messages"] = [
                m for m in msgs if m.get("channel_id") != channel_id
            ]
            await self._save_unlocked()
            return True

    async def add_discord_message(
        self, channel_id: str, author: str, text: str = "",
        attachments: Optional[list] = None,
        mentions: Optional[list] = None,
        reply_to: Optional[str] = None,
        author_avatar: str = "",
    ) -> dict:
        """Добавить сообщение в канал. Возвращает созданный entry."""
        msg = {
            "id": f"msg{int(time.time() * 1000)}",
            "channel_id": channel_id,
            "ts": time.time(),
            "author": (author or "system").lstrip("@"),
            "author_avatar": author_avatar or "",
            "text": (text or "").strip(),
            "attachments": list(attachments or []),
            "mentions": list(mentions or []),
            "reply_to": reply_to,
            "edited": False,
        }
        async with _lock:
            msgs = self.state.get("discord_messages")
            if not isinstance(msgs, list):
                msgs = []
                self.state["discord_messages"] = msgs
            msgs.append(msg)
            # Cap: храним максимум 5000 последних сообщений (защита от роста)
            if len(msgs) > 5000:
                self.state["discord_messages"] = msgs[-5000:]
            await self._save_unlocked()
            return msg

    def list_discord_messages(
        self, channel_id: str, limit: int = 100, before_ts: Optional[float] = None,
    ) -> list:
        msgs = self.state.get("discord_messages") or []
        # Защита от corrupted state (если кто-то записал dict вместо list)
        if not isinstance(msgs, list):
            try:
                msgs = list(msgs.values()) if isinstance(msgs, dict) else []
            except Exception:
                msgs = []
        filtered = []
        for m in msgs:
            if not isinstance(m, dict):
                continue
            if m.get("channel_id") != channel_id:
                continue
            if before_ts and (m.get("ts") or 0) >= before_ts:
                continue
            filtered.append(m)
        filtered.sort(key=lambda m: m.get("ts") or 0)
        return filtered[-limit:] if limit else filtered

    async def delete_discord_message(self, message_id: str) -> bool:
        async with _lock:
            msgs = self.state.get("discord_messages") or []
            before = len(msgs)
            self.state["discord_messages"] = [
                m for m in msgs if m.get("id") != message_id
            ]
            changed = len(self.state["discord_messages"]) < before
            if changed:
                await self._save_unlocked()
            return changed

    async def edit_discord_message(self, message_id: str, new_text: str) -> bool:
        async with _lock:
            msgs = self.state.get("discord_messages") or []
            for m in msgs:
                if m.get("id") == message_id:
                    m["text"] = (new_text or "").strip()
                    m["edited"] = True
                    m["edited_at"] = time.time()
                    await self._save_unlocked()
                    return True
            return False

    # === TG user info (для аватарок и имён в Discord-хабе) ===

    async def record_tg_user_info(
        self, user_id: int, username: str = "", first_name: str = "",
        last_name: str = "", photo_url: str = "",
    ):
        """Сохранить профиль админа из Telegram OAuth."""
        if not user_id:
            return
        async with _lock:
            info = self.state.setdefault("tg_user_info", {})
            entry = info.setdefault(str(user_id), {})
            entry["user_id"] = int(user_id)
            if username:
                entry["username"] = username.lstrip("@")
            if first_name:
                entry["first_name"] = first_name
            if last_name:
                entry["last_name"] = last_name
            if photo_url:
                entry["photo_url"] = photo_url
            entry["last_seen_ts"] = time.time()
            await self._save_unlocked()

    def get_tg_user_info(self, user_id) -> dict:
        info = self.state.get("tg_user_info") or {}
        return dict(info.get(str(user_id)) or {})

    def list_tg_user_info(self) -> dict:
        return dict(self.state.get("tg_user_info") or {})

    # === Реакции на сообщения Discord ===

    async def add_discord_reaction(
        self, message_id: str, emoji: str, user: str,
    ) -> dict:
        async with _lock:
            reactions = self.state.setdefault("discord_reactions", {})
            msg_reacts = reactions.setdefault(message_id, {})
            lst = msg_reacts.setdefault(emoji, [])
            u = (user or "").lstrip("@")
            if u and u not in lst:
                lst.append(u)
            await self._save_unlocked()
            return dict(msg_reacts)

    async def remove_discord_reaction(
        self, message_id: str, emoji: str, user: str,
    ) -> dict:
        async with _lock:
            reactions = self.state.setdefault("discord_reactions", {})
            msg_reacts = reactions.get(message_id) or {}
            if emoji in msg_reacts:
                u = (user or "").lstrip("@")
                msg_reacts[emoji] = [x for x in msg_reacts[emoji] if x != u]
                if not msg_reacts[emoji]:
                    del msg_reacts[emoji]
            if not msg_reacts and message_id in reactions:
                del reactions[message_id]
            await self._save_unlocked()
            return dict(msg_reacts)

    def get_discord_reactions(self, message_id: str) -> dict:
        reactions = self.state.get("discord_reactions") or {}
        return dict(reactions.get(message_id) or {})

    def get_all_discord_reactions(self) -> dict:
        return dict(self.state.get("discord_reactions") or {})

    # === Pin / unread ===

    async def pin_discord_message(self, channel_id: str, message_id: str) -> bool:
        async with _lock:
            pins = self.state.setdefault("discord_pins", {})
            arr = pins.setdefault(channel_id, [])
            if message_id in arr:
                return False
            arr.append(message_id)
            await self._save_unlocked()
            return True

    async def unpin_discord_message(self, channel_id: str, message_id: str) -> bool:
        async with _lock:
            pins = self.state.get("discord_pins") or {}
            arr = pins.get(channel_id) or []
            if message_id not in arr:
                return False
            pins[channel_id] = [m for m in arr if m != message_id]
            await self._save_unlocked()
            return True

    def get_pinned_messages(self, channel_id: str) -> list:
        pins = self.state.get("discord_pins") or {}
        return list(pins.get(channel_id) or [])

    async def mark_channel_read(
        self, user: str, channel_id: str, ts: Optional[float] = None,
    ):
        u = (user or "").lstrip("@")
        if not u:
            return
        async with _lock:
            reads = self.state.setdefault("discord_reads", {})
            per_user = reads.setdefault(u, {})
            per_user[channel_id] = float(ts or time.time())
            await self._save_unlocked()

    def get_last_read_ts(self, user: str, channel_id: str) -> float:
        u = (user or "").lstrip("@")
        reads = self.state.get("discord_reads") or {}
        per_user = reads.get(u) or {}
        return float(per_user.get(channel_id) or 0)

    async def remove_managed_chat(self, chat_id) -> bool:
        """Удаляет чат из managed_chats — AI перестаёт там отвечать.
        Используется командой 'Ассистент забудь этот чат'."""
        key = _norm_chat_id(chat_id)
        async with _lock:
            managed = self.state.get("managed_chats") or {}
            if key not in managed:
                return False
            managed.pop(key, None)
            # Заодно почистим обратный индекс username, если он указывал сюда.
            idx = self.state.get("client_username_index") or {}
            for uname_key, mapped in list(idx.items()):
                if str(mapped) == str(key):
                    idx.pop(uname_key, None)
            await self._save_unlocked()
            return True

    async def cleanup(self):
        now = time.time()
        changed = False
        async with _lock:
            old_chats = [
                k for k, v in self.state["managed_chats"].items()
                if now - v.get("created_at", now) > _CHAT_TTL_SEC
            ]
            for k in old_chats:
                del self.state["managed_chats"][k]
            if old_chats:
                changed = True
                print(f"[storage] cleanup: removed {len(old_chats)} old managed_chats")

            cd_sec = self.state["cooldown_minutes"] * 60
            cutoff = now - cd_sec * 2
            old_cooldowns = [
                k for k, v in self.state["user_cooldowns"].items()
                if v < cutoff
            ]
            for k in old_cooldowns:
                del self.state["user_cooldowns"][k]
            if old_cooldowns:
                changed = True
                print(f"[storage] cleanup: removed {len(old_cooldowns)} expired cooldowns")

            if changed:
                await self._save_unlocked()

    def get_stats(self) -> dict:
        return dict(self.state["stats"])

    def get_secret_command(self) -> str:
        return self.state["admin_secret_command"]

    def get_brain_chat_id(self) -> int:
        return int(self.state.get("brain_chat_id") or 0)

    async def set_brain_chat_id(self, chat_id: int):
        async with _lock:
            self.state["brain_chat_id"] = int(chat_id)
            await self._save_unlocked()

    def get_client_idle_minutes(self) -> int:
        return int(self.state.get("client_idle_minutes") or 5)

    async def set_client_idle_minutes(self, minutes: int):
        async with _lock:
            self.state["client_idle_minutes"] = int(minutes)
            await self._save_unlocked()

    async def register_source(self, user_id: int, source: str) -> bool:
        source = (source or "").strip()
        if not source:
            return False
        async with _lock:
            uid = str(user_id)
            sources = self.state.setdefault("user_sources", {})
            stats = self.state.setdefault("source_stats", {})
            if uid in sources:
                return False
            sources[uid] = source
            stats[source] = stats.get(source, 0) + 1
            await self._save_unlocked()
            return True

    def get_source_stats(self) -> dict:
        return dict(self.state.get("source_stats", {}))

    def get_user_source(self, user_id: int) -> Optional[str]:
        return self.state.get("user_sources", {}).get(str(user_id))

    def is_ai_enabled(self) -> bool:
        return bool(self.state.get("ai_enabled", False))

    async def set_ai_enabled(self, enabled: bool):
        async with _lock:
            self.state["ai_enabled"] = bool(enabled)
            await self._save_unlocked()

    def get_ai_model(self) -> str:
        return self.state.get("ai_model") or ""

    async def set_ai_model(self, model: str):
        async with _lock:
            self.state["ai_model"] = (model or "").strip()
            await self._save_unlocked()

    def get_ai_stats(self) -> dict:
        return dict(self.state.get("ai_stats") or {})

    async def bump_ai_stats(
        self,
        *,
        replies: int = 0,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cache_read_tokens: int = 0,
        cache_write_tokens: int = 0,
        errors: int = 0,
        skipped_worker_active: int = 0,
        skipped_ack: int = 0,
        model: str = "",
    ):
        """Стата + расчёт стоимости в USD.
        Haiku 4.5: $1/1M input, $5/1M output, $0.10/1M cache-read, $1.25/1M cache-write.
        Sonnet 4.6: $3/1M, $15/1M, $0.30/1M, $3.75/1M.
        """
        # Тарифы (USD per 1M tokens)
        is_sonnet = "sonnet" in (model or "").lower()
        price_in = 3.0 if is_sonnet else 1.0
        price_out = 15.0 if is_sonnet else 5.0
        price_cache_read = 0.30 if is_sonnet else 0.10
        price_cache_write = 3.75 if is_sonnet else 1.25
        delta_cost = (
            input_tokens * price_in / 1_000_000
            + output_tokens * price_out / 1_000_000
            + cache_read_tokens * price_cache_read / 1_000_000
            + cache_write_tokens * price_cache_write / 1_000_000
        )
        async with _lock:
            stats = self.state.setdefault(
                "ai_stats",
                {
                    "replies_total": 0,
                    "input_tokens_total": 0,
                    "output_tokens_total": 0,
                    "cache_read_total": 0,
                    "cache_write_total": 0,
                    "cost_usd_total": 0.0,
                    "errors_total": 0,
                    "skipped_worker_active": 0,
                    "skipped_ack": 0,
                },
            )
            stats["replies_total"] = int(stats.get("replies_total", 0)) + replies
            stats["input_tokens_total"] = int(stats.get("input_tokens_total", 0)) + input_tokens
            stats["output_tokens_total"] = int(stats.get("output_tokens_total", 0)) + output_tokens
            stats["cache_read_total"] = int(stats.get("cache_read_total", 0)) + cache_read_tokens
            stats["cache_write_total"] = int(stats.get("cache_write_total", 0)) + cache_write_tokens
            stats["cost_usd_total"] = float(stats.get("cost_usd_total", 0.0)) + delta_cost
            stats["errors_total"] = int(stats.get("errors_total", 0)) + errors
            stats["skipped_worker_active"] = (
                int(stats.get("skipped_worker_active", 0)) + skipped_worker_active
            )
            stats["skipped_ack"] = int(stats.get("skipped_ack", 0)) + skipped_ack
            await self._save_unlocked()

    def is_writeback_enabled(self) -> bool:
        return bool(self.state.get("ai_writeback_enabled", False))

    async def set_writeback_enabled(self, enabled: bool):
        async with _lock:
            self.state["ai_writeback_enabled"] = bool(enabled)
            await self._save_unlocked()

    def get_writeback_stats(self) -> dict:
        return dict(self.state.get("writeback_stats") or {})

    async def bump_writeback_stats(
        self, *, commits: int = 0, skipped: int = 0, errors: int = 0
    ):
        async with _lock:
            stats = self.state.setdefault(
                "writeback_stats",
                {"commits_total": 0, "skipped_total": 0, "errors_total": 0},
            )
            stats["commits_total"] = int(stats.get("commits_total", 0)) + commits
            stats["skipped_total"] = int(stats.get("skipped_total", 0)) + skipped
            stats["errors_total"] = int(stats.get("errors_total", 0)) + errors
            await self._save_unlocked()

    def get_coordination_chat_id(self) -> int:
        return int(self.state.get("coordination_chat_id") or 0)

    async def set_coordination_chat_id(self, chat_id: int):
        async with _lock:
            self.state["coordination_chat_id"] = int(chat_id)
            await self._save_unlocked()

    def get_escalate_stats(self) -> dict:
        return dict(self.state.get("escalate_stats") or {})

    async def bump_escalate_stats(self, *, specialist: str = "", error: bool = False):
        async with _lock:
            stats = self.state.setdefault(
                "escalate_stats",
                {"calls_total": 0, "by_specialist": {}, "errors_total": 0},
            )
            if error:
                stats["errors_total"] = int(stats.get("errors_total", 0)) + 1
            else:
                stats["calls_total"] = int(stats.get("calls_total", 0)) + 1
                if specialist:
                    by = stats.setdefault("by_specialist", {})
                    by[specialist] = int(by.get(specialist, 0)) + 1
            await self._save_unlocked()

    def get_deal(self, deal_id: str) -> Optional[dict]:
        return (self.state.get("deals") or {}).get(_norm_deal_id(deal_id))

    def list_deals(self, status: Optional[str] = None) -> dict:
        all_deals = self.state.get("deals") or {}
        if status is None:
            return dict(all_deals)
        return {k: v for k, v in all_deals.items() if v.get("status") == status}

    def find_deal_by(
        self,
        deal_id: Optional[str] = None,
        username: Optional[str] = None,
        fio: Optional[str] = None,
        bank: Optional[str] = None,
    ) -> list:
        out = []
        deals = self.state.get("deals") or {}
        for did, d in deals.items():
            if deal_id and _norm_deal_id(deal_id) != did:
                continue
            if username:
                u = (d.get("client_username") or "").lstrip("@").lower()
                if username.lstrip("@").lower() != u:
                    continue
            if fio:
                if fio.lower() not in (d.get("fio") or "").lower():
                    continue
            if bank:
                if bank.lower() not in (d.get("bank") or "").lower():
                    continue
            out.append({"deal_id": did, **d})
        return out

    async def add_deal(
        self,
        deal_id: str,
        client_username: str,
        fio: str,
        bank: str,
        amount,
        fee,
        method: str,
        status: str = "ПОПОЛНИТЬ",
        work_chat_id=None,
    ) -> bool:
        deal_id = _norm_deal_id(deal_id)
        if not deal_id:
            return False
        async with _lock:
            deals = self.state.setdefault("deals", {})
            if deal_id in deals:
                return False
            deals[deal_id] = {
                "client_username": (client_username or "").lstrip("@"),
                "fio": fio or "",
                "bank": bank or "",
                "amount": amount,
                "fee": fee,
                "method": method or "",
                "status": status,
                "created_at": time.time(),
                "history": [{"ts": time.time(), "status": status}],
                "work_chat_id": work_chat_id,
            }
            stats = self.state.setdefault(
                "deals_stats",
                {"created_total": 0, "by_status": {}, "errors_total": 0},
            )
            stats["created_total"] = int(stats.get("created_total", 0)) + 1
            by = stats.setdefault("by_status", {})
            by[status] = int(by.get(status, 0)) + 1
            await self._save_unlocked()
            return True

    async def update_deal_status(self, deal_id: str, new_status: str) -> bool:
        deal_id = _norm_deal_id(deal_id)
        new_status = (new_status or "").strip()
        if not deal_id or not new_status:
            return False
        async with _lock:
            deals = self.state.get("deals") or {}
            d = deals.get(deal_id)
            if not d:
                return False
            old_status = d.get("status", "")
            d["status"] = new_status
            d.setdefault("history", []).append(
                {"ts": time.time(), "status": new_status}
            )
            stats = self.state.setdefault(
                "deals_stats",
                {"created_total": 0, "by_status": {}, "errors_total": 0},
            )
            by = stats.setdefault("by_status", {})
            if old_status:
                by[old_status] = max(0, int(by.get(old_status, 0)) - 1)
            by[new_status] = int(by.get(new_status, 0)) + 1
            await self._save_unlocked()
            return True

    def get_deals_stats(self) -> dict:
        return dict(self.state.get("deals_stats") or {})

    # === Бухгалтерия V2: Группа 2 «Бухгалтерия» (заявки v2) ===

    def get_accounting_group_id(self) -> int:
        return int(self.state.get("accounting_group_id") or 0)

    async def set_accounting_group_id(self, chat_id: int):
        async with _lock:
            self.state["accounting_group_id"] = int(chat_id)
            await self._save_unlocked()

    # === ЛК-карточки (Группа 1) ===

    def get_lk_group_id(self) -> int:
        return int(self.state.get("lk_group_id") or 0)

    async def set_lk_group_id(self, chat_id: int):
        async with _lock:
            self.state["lk_group_id"] = int(chat_id)
            await self._save_unlocked()

    # ===== Очереди выплат (payout queues) =====
    # 3 очереди по методу оплаты:
    #   release        — GUARANTOR_BEFORE: сделка уже пополнена, надо отпустить
    #   fund_release   — GUARANTOR_AFTER_WORK: клиент создал сделку, надо пополнить + отпустить
    #   usdt           — USDT TRC20: надо отправить USDT на адрес

    def _payouts_state(self) -> dict:
        return self.state.setdefault("payout_queues", {
            "release": [], "fund_release": [], "usdt": [],
        })

    def list_payouts(self, queue: str) -> list:
        q = self._payouts_state().get(queue) or []
        return list(q)

    async def add_payout(self, queue: str, item: dict) -> int:
        async with _lock:
            qs = self._payouts_state()
            arr = qs.setdefault(queue, [])
            cid = item.get("card_id")
            # Дедуп: если в этой очереди уже есть запись на этот card_id —
            # МЕРЖИМ поля, не создаём новую. Это спасает от дублей при
            # повторном изменении статуса / срабатывании бэкфилла.
            if cid:
                for existing in arr:
                    if existing.get("card_id") == cid:
                        for k, v in item.items():
                            if v in (None, "", 0):
                                continue
                            existing[k] = v
                        existing.setdefault("status", "pending")
                        await self._save_unlocked()
                        return existing.get("id") or 0
            new_id = (max((i.get("id") or 0) for i in arr) + 1) if arr else 1
            item = dict(item)
            item["id"] = new_id
            item["ts"] = time.time()
            item["status"] = item.get("status") or "pending"
            arr.append(item)
            await self._save_unlocked()
            return new_id

    async def dedupe_payouts(self) -> int:
        """Одноразовая чистка дублей по card_id в каждой очереди.
        Оставляет первую запись, мержит остальные в неё. Возвращает кол-во удалённых."""
        async with _lock:
            qs = self._payouts_state()
            removed = 0
            for qname in ("release", "fund_release", "usdt"):
                arr = qs.get(qname) or []
                seen = {}  # card_id -> kept item
                new_arr = []
                for item in arr:
                    cid = item.get("card_id")
                    if not cid:
                        new_arr.append(item)
                        continue
                    if cid in seen:
                        # мержим в первого — берём непустые поля
                        kept = seen[cid]
                        for k, v in item.items():
                            if v in (None, "", 0):
                                continue
                            if kept.get(k) in (None, "", 0):
                                kept[k] = v
                        removed += 1
                    else:
                        seen[cid] = item
                        new_arr.append(item)
                qs[qname] = new_arr
            if removed > 0:
                await self._save_unlocked()
            return removed

    async def update_payout(self, queue: str, payout_id: int, **fields) -> bool:
        async with _lock:
            arr = self._payouts_state().get(queue) or []
            for item in arr:
                if int(item.get("id") or 0) == int(payout_id):
                    for k, v in fields.items():
                        item[k] = v
                    await self._save_unlocked()
                    return True
            return False

    async def remove_payout(self, queue: str, payout_id: int) -> bool:
        async with _lock:
            qs = self._payouts_state()
            arr = qs.get(queue) or []
            new_arr = [i for i in arr if int(i.get("id") or 0) != int(payout_id)]
            if len(new_arr) == len(arr):
                return False
            qs[queue] = new_arr
            await self._save_unlocked()
            return True

    def find_payout_by_card(self, card_id: str, queue: str = None) -> Optional[tuple]:
        """Возвращает (queue_name, item) для первого совпадения по card_id."""
        qs = self._payouts_state()
        queues = [queue] if queue else ("release", "fund_release", "usdt")
        for q in queues:
            for item in (qs.get(q) or []):
                if item.get("card_id") == card_id:
                    return (q, item)
        return None

    def find_payout_by_deal(self, deal_id: str) -> Optional[tuple]:
        """Поиск выплаты по номеру сделки в любой очереди."""
        if not deal_id:
            return None
        target = str(deal_id).lstrip("#").strip()
        for q in ("release", "fund_release"):
            for item in (self._payouts_state().get(q) or []):
                if str(item.get("deal_id") or "").lstrip("#").strip() == target:
                    return (q, item)
        return None

    # ===== Ideas Inbox =====
    def get_ideas_chat_id(self) -> int:
        return int(self.state.get("ideas_chat_id") or 0)

    async def set_ideas_chat_id(self, chat_id: int):
        async with _lock:
            self.state["ideas_chat_id"] = int(chat_id)
            await self._save_unlocked()

    def list_ideas(self, only_unresolved: bool = False) -> list:
        items = list(self.state.get("ideas_inbox") or [])
        if only_unresolved:
            items = [i for i in items if not i.get("resolved")]
        return items

    async def add_idea(self, text: str, author: str = "", chat_id: int = 0,
                       msg_id: int = 0, kind: str = "idea") -> int:
        async with _lock:
            inbox = self.state.setdefault("ideas_inbox", [])
            new_id = (max((i.get("id") or 0) for i in inbox) + 1) if inbox else 1
            inbox.append({
                "id": new_id,
                "text": (text or "").strip(),
                "author": author or "",
                "chat_id": int(chat_id),
                "msg_id": int(msg_id),
                "kind": kind,  # "idea" / "bug"
                "ts": time.time(),
                "resolved": False,
            })
            await self._save_unlocked()
            return new_id

    async def mark_idea_resolved(self, idea_id: int, resolved: bool = True) -> bool:
        async with _lock:
            for i in (self.state.get("ideas_inbox") or []):
                if i.get("id") == idea_id:
                    i["resolved"] = resolved
                    await self._save_unlocked()
                    return True
            return False

    async def clear_resolved_ideas(self) -> int:
        async with _lock:
            inbox = self.state.get("ideas_inbox") or []
            before = len(inbox)
            self.state["ideas_inbox"] = [i for i in inbox if not i.get("resolved")]
            await self._save_unlocked()
            return before - len(self.state["ideas_inbox"])

    def get_lk_card(self, card_id: str) -> Optional[dict]:
        return (self.state.get("lk_cards") or {}).get(str(card_id))

    def list_lk_cards(self, status: Optional[str] = None) -> dict:
        all_cards = self.state.get("lk_cards") or {}
        if status is None:
            return dict(all_cards)
        return {k: v for k, v in all_cards.items() if v.get("status") == status}

    def find_lk_card(
        self,
        bank: Optional[str] = None,
        fio: Optional[str] = None,
        supplier: Optional[str] = None,
        work_chat_id=None,
    ) -> list:
        """Поиск карточек по любому набору полей. AND-логика, case-insensitive
        substring для bank/fio/supplier."""
        out = []
        cards = self.state.get("lk_cards") or {}
        wc = _norm_chat_id(work_chat_id) if work_chat_id else None
        for cid, c in cards.items():
            if bank and bank.lower() not in (c.get("bank") or "").lower():
                continue
            if fio and fio.lower() not in (c.get("fio") or "").lower():
                continue
            if supplier:
                s = (c.get("supplier") or "").lstrip("@").lower()
                if supplier.lstrip("@").lower() not in s:
                    continue
            if wc and _norm_chat_id(c.get("work_chat_id") or 0) != wc:
                continue
            out.append({"card_id": cid, **c})
        return out

    # === Import summaries (массовые импорты ЛК) ===

    async def save_import_summary(
        self, msg_id: int, chat_id, html_text: str, card_ids: list,
    ):
        """Сохраняет HTML сводки массового импорта по msg_id — для дальнейшего
        зачёркивания строк удалённых карточек."""
        if not msg_id:
            return False
        key = str(int(msg_id))
        async with _lock:
            summaries = self.state.setdefault("import_summaries", {})
            summaries[key] = {
                "chat_id": _norm_chat_id(chat_id),
                "html": html_text or "",
                "card_ids": list(card_ids or []),
            }
            await self._save_unlocked()
            return True

    def get_import_summary(self, msg_id: int) -> dict:
        if not msg_id:
            return {}
        key = str(int(msg_id))
        return dict(self.state.get("import_summaries", {}).get(key) or {})

    async def update_import_summary_html(self, msg_id: int, new_html: str):
        """Обновляет сохранённый HTML сводки (после edit_message в Telegram)."""
        if not msg_id:
            return False
        key = str(int(msg_id))
        async with _lock:
            summaries = self.state.setdefault("import_summaries", {})
            entry = summaries.get(key)
            if entry is None:
                return False
            entry["html"] = new_html or ""
            await self._save_unlocked()
            return True

    # ========================================================================
    # === Кошелёк партнёра (TRC20 USDT) ===
    # Custodial-схема:
    #   - У каждого партнёра (поставщика ЛК) есть `balance_usdt` в state.json.
    #   - Партнёр пополняет наш единый TRC20-адрес (env TRC20_DEPOSIT_ADDRESS)
    #     и присылает админу TXID — админ подтверждает зачисление вручную.
    #   - Партнёр запрашивает вывод → бот создаёт pending payout → админ-чат
    #     получает кнопку «Подтвердить выплачено» → админ делает manual TX
    #     в кошельке и жмёт кнопку → баланс списывается.
    #
    # СТРУКТУРА state["partner_wallets"]:
    # {
    #   "username1": {
    #     "balance_usdt": 0.0,
    #     "address_default": "T...",          # последний адрес вывода
    #     "history": [{ts, type, amount, reason, txid, address, lk_card_id, payout_id}, ...],
    #     "pending_payouts": [{payout_id, ts, amount, address}, ...],
    #   }
    # }
    # ========================================================================

    @staticmethod
    def _norm_partner_uname(u: str) -> str:
        return (u or "").lstrip("@").strip().lower()

    def get_partner_wallet(self, username: str) -> dict:
        """Возвращает кошелёк партнёра (создаёт пустой если не было)."""
        key = self._norm_partner_uname(username)
        if not key:
            return {}
        wallets = self.state.get("partner_wallets") or {}
        return wallets.get(key) or {
            "balance_usdt": 0.0, "address_default": "",
            "history": [], "pending_payouts": [],
        }

    def list_partner_wallets(self) -> dict:
        """Все кошельки (для admin/dashboard)."""
        return dict(self.state.get("partner_wallets") or {})

    def list_pending_payouts(self) -> list:
        """Все pending выводы по всем партнёрам (для admin списка)."""
        result = []
        for uname, w in (self.state.get("partner_wallets") or {}).items():
            for p in (w.get("pending_payouts") or []):
                result.append({"username": uname, **p})
        result.sort(key=lambda x: x.get("ts") or 0, reverse=True)
        return result

    async def _ensure_partner_wallet(self, username: str) -> dict:
        """Внутренний: создать пустой wallet если ещё не существует.
        Вызывается ВНУТРИ _lock — не дёргать снаружи."""
        key = self._norm_partner_uname(username)
        wallets = self.state.setdefault("partner_wallets", {})
        if key not in wallets:
            wallets[key] = {
                "balance_usdt": 0.0, "address_default": "",
                "history": [], "pending_payouts": [],
            }
        return wallets[key]

    async def wallet_credit(
        self, username: str, amount_usdt: float,
        reason: str = "", txid: str = "", lk_card_id: str = "",
    ) -> bool:
        """Зачислить USDT на кошелёк партнёра.
        reason: 'lk_payout' (за ЛК), 'manual_deposit' (адмиздачисление по TXID),
                'correction' (правка), 'refund' (возврат)."""
        key = self._norm_partner_uname(username)
        if not key:
            return False
        try:
            amt = float(amount_usdt or 0)
        except (TypeError, ValueError):
            return False
        if amt <= 0:
            return False
        async with _lock:
            w = await self._ensure_partner_wallet(key)
            w["balance_usdt"] = round(float(w.get("balance_usdt") or 0) + amt, 6)
            w.setdefault("history", []).append({
                "ts": time.time(),
                "type": "credit",
                "amount": amt,
                "reason": reason or "credit",
                "txid": txid or "",
                "lk_card_id": lk_card_id or "",
            })
            await self._save_unlocked()
            return True

    async def wallet_debit(
        self, username: str, amount_usdt: float,
        reason: str = "", txid: str = "", address: str = "",
        payout_id: str = "",
    ) -> bool:
        """Списать USDT (для confirm payout или manual correction).
        Возвращает False если баланс < amount."""
        key = self._norm_partner_uname(username)
        if not key:
            return False
        try:
            amt = float(amount_usdt or 0)
        except (TypeError, ValueError):
            return False
        if amt <= 0:
            return False
        async with _lock:
            w = await self._ensure_partner_wallet(key)
            cur = float(w.get("balance_usdt") or 0)
            if cur + 1e-9 < amt:  # с поправкой на float
                return False
            w["balance_usdt"] = round(cur - amt, 6)
            w.setdefault("history", []).append({
                "ts": time.time(),
                "type": "debit",
                "amount": amt,
                "reason": reason or "debit",
                "txid": txid or "",
                "address": address or "",
                "payout_id": payout_id or "",
            })
            await self._save_unlocked()
            return True

    async def wallet_create_payout_request(
        self, username: str, amount_usdt: float, trc20_address: str,
    ) -> Optional[str]:
        """Создаёт pending payout request. НЕ списывает баланс (списание
        происходит только когда admin подтвердит выплату через wallet_confirm_payout).
        Однако ПРОВЕРЯЕТ что баланс >= amount, иначе возвращает None.
        Возвращает payout_id или None если баланса не хватает."""
        key = self._norm_partner_uname(username)
        if not key:
            return None
        try:
            amt = float(amount_usdt or 0)
        except (TypeError, ValueError):
            return None
        if amt <= 0:
            return None
        addr = (trc20_address or "").strip()
        if not addr:
            return None
        async with _lock:
            w = await self._ensure_partner_wallet(key)
            # Сумма pending payouts уже зарезервирована — учитываем
            reserved = sum(
                float(p.get("amount") or 0)
                for p in (w.get("pending_payouts") or [])
            )
            available = float(w.get("balance_usdt") or 0) - reserved
            if available + 1e-9 < amt:
                return None
            seq = int(self.state.get("partner_wallets_payout_seq", 0)) + 1
            self.state["partner_wallets_payout_seq"] = seq
            payout_id = f"pw{seq:04d}"
            w.setdefault("pending_payouts", []).append({
                "payout_id": payout_id,
                "ts": time.time(),
                "amount": amt,
                "address": addr,
            })
            w["address_default"] = addr
            w.setdefault("history", []).append({
                "ts": time.time(),
                "type": "payout_request",
                "amount": amt,
                "address": addr,
                "payout_id": payout_id,
            })
            await self._save_unlocked()
            return payout_id

    async def wallet_confirm_payout(
        self, payout_id: str, txid: str = "",
    ) -> Optional[dict]:
        """Admin подтвердил что выплата сделана (manual TX).
        Списывает баланс + перемещает payout в history. Возвращает данные
        выплаты (username, amount, address) или None если не найдено."""
        if not payout_id:
            return None
        async with _lock:
            wallets = self.state.get("partner_wallets") or {}
            for uname, w in wallets.items():
                for i, p in enumerate(w.get("pending_payouts") or []):
                    if p.get("payout_id") == payout_id:
                        amt = float(p.get("amount") or 0)
                        addr = p.get("address") or ""
                        # Списать с баланса
                        cur = float(w.get("balance_usdt") or 0)
                        if cur + 1e-9 < amt:
                            return None  # парадокс: pending без покрытия
                        w["balance_usdt"] = round(cur - amt, 6)
                        # Удалить из pending
                        w["pending_payouts"].pop(i)
                        # В history
                        w.setdefault("history", []).append({
                            "ts": time.time(),
                            "type": "payout_done",
                            "amount": amt,
                            "address": addr,
                            "txid": txid or "",
                            "payout_id": payout_id,
                        })
                        await self._save_unlocked()
                        return {
                            "username": uname, "amount": amt,
                            "address": addr, "payout_id": payout_id,
                        }
            return None

    async def wallet_reject_payout(
        self, payout_id: str, reason: str = "",
    ) -> Optional[dict]:
        """Admin отклонил вывод (например подозрительный адрес).
        НЕ списывает баланс — просто удаляет из pending."""
        if not payout_id:
            return None
        async with _lock:
            wallets = self.state.get("partner_wallets") or {}
            for uname, w in wallets.items():
                for i, p in enumerate(w.get("pending_payouts") or []):
                    if p.get("payout_id") == payout_id:
                        amt = float(p.get("amount") or 0)
                        addr = p.get("address") or ""
                        w["pending_payouts"].pop(i)
                        w.setdefault("history", []).append({
                            "ts": time.time(),
                            "type": "payout_rejected",
                            "amount": amt,
                            "address": addr,
                            "payout_id": payout_id,
                            "reason": reason or "",
                        })
                        await self._save_unlocked()
                        return {
                            "username": uname, "amount": amt,
                            "address": addr, "reason": reason,
                        }
            return None

    # === Прайс ЛК ===
    # Единый источник цен. Меняется через команду «прайс БАНК ЦЕНА» в
    # брейн-чате. accounting2.lookup_pricing использует это в первую очередь.

    # Дефолтные цены по банкам (USD за ЛК) — fallback из knowledge когда
    # в storage прайс не задан или price_usdt у конкретного ЛК пустой.
    # Используется в resolve_lk_price(): если у ЛК нет price_usdt, берём
    # из storage.pricing, а если и там нет — из этого hardcoded словаря.
    DEFAULT_LK_PRICES = {
        "АЛЬФА":   400.0,
        "ЛОКО":    200.0,
        "ТОЧКА":   200.0,
        "ОЗОН":    400.0,
        "РАЙФ":    400.0,
    }

    @staticmethod
    def _norm_bank_key(bank: str) -> str:
        return (bank or "").strip().upper()

    def get_pricing(self, bank: str):
        """Возвращает цену из storage или None если не задана."""
        if not bank:
            return None
        prices = self.state.get("pricing") or {}
        return prices.get(self._norm_bank_key(bank))

    def resolve_lk_price(self, bank: str, price_usdt) -> float:
        """Резолвит цену ЛК по приоритетам:
          1) Если price_usdt > 0 — возвращает как есть (явно задано)
          2) Иначе цена из storage.pricing (по команде «прайс БАНК N»)
          3) Иначе DEFAULT_LK_PRICES (hardcoded fallback из knowledge)
          4) Иначе 0
        Используется в list_lk_in_work и других местах где нужна цена
        для отображения / выплат.
        """
        try:
            p = float(price_usdt or 0)
            if p > 0:
                return p
        except (TypeError, ValueError):
            pass
        key = self._norm_bank_key(bank)
        if not key:
            return 0.0
        # 2) storage.pricing
        stored = (self.state.get("pricing") or {}).get(key)
        if stored:
            try:
                return float(stored)
            except (TypeError, ValueError):
                pass
        # 3) hardcoded defaults
        return float(self.DEFAULT_LK_PRICES.get(key, 0))

    def list_pricing(self) -> dict:
        """Копия всего прайса {BANK_UPPER: price_usdt}."""
        return dict(self.state.get("pricing") or {})

    async def set_pricing(self, bank: str, price: float) -> bool:
        if not bank:
            return False
        key = self._norm_bank_key(bank)
        if not key:
            return False
        try:
            price_f = float(price)
        except (TypeError, ValueError):
            return False
        async with _lock:
            prices = self.state.setdefault("pricing", {})
            prices[key] = price_f
            await self._save_unlocked()
            return True

    async def remove_pricing(self, bank: str) -> bool:
        if not bank:
            return False
        key = self._norm_bank_key(bank)
        async with _lock:
            prices = self.state.setdefault("pricing", {})
            if key not in prices:
                return False
            prices.pop(key, None)
            await self._save_unlocked()
            return True

    async def delete_lk_card(self, card_id: str) -> bool:
        """Удаляет одну карточку ЛК по card_id. Возвращает True если
        карточка существовала и удалена."""
        if not card_id:
            return False
        key = str(card_id).strip().lstrip("#")
        async with _lock:
            cards = self.state.get("lk_cards") or {}
            if key not in cards:
                return False
            cards.pop(key, None)
            await self._save_unlocked()
            return True

    async def delete_all_lk_cards(self) -> int:
        """Удаляет ВСЕ карточки ЛК. Возвращает количество удалённых.

        ⚠️ Деструктивная операция — вызывается только после двойного
        подтверждения (Тимон + админ) через команду «Ассистент удалить все ЛК»
        в Группе 1."""
        async with _lock:
            cards = self.state.get("lk_cards") or {}
            n = len(cards)
            self.state["lk_cards"] = {}
            self.state["lk_cards_seq"] = 0
            await self._save_unlocked()
            return n

    def find_active_lk_card(
        self, supplier: str = "", bank: str = "", fio: str = "",
    ) -> Optional[dict]:
        """Ищет существующую активную карточку по (supplier, bank, fio).
        Активная = status НЕ в (ЗАВЕРШЁН, БРАК, БЛОК, УДАЛЁНА).
        Возвращает первую совпадающую или None.

        Используется для dedupe в _create_lk_card_from_perevyaz и
        _tool_create_lk_card — чтобы не плодить дубли при повторном триггере.
        """
        if not bank:
            return None
        sup = (supplier or "").lstrip("@").lower().strip()
        bnk = bank.upper().strip()
        f = (fio or "").lower().strip()
        terminal = {"ЗАВЕРШЁН", "ЗАВЕРШЕН", "БРАК", "БЛОК", "УДАЛЕНА", "УДАЛЁНА"}
        for c in (self.state.get("lk_cards") or {}).values():
            if not c:
                continue
            if (c.get("status") or "").upper() in terminal:
                continue
            if (c.get("bank") or "").upper().strip() != bnk:
                continue
            if sup and (c.get("supplier") or "").lstrip("@").lower().strip() != sup:
                continue
            if f and (c.get("fio") or "").lower().strip() != f:
                continue
            return c
        return None

    async def add_lk_card(self, **fields) -> str:
        """Создаёт новую карточку. Возвращает card_id ("lk001"...).

        Обязательные: bank, fio, price_usdt, payment_method.
        """
        async with _lock:
            seq = int(self.state.get("lk_cards_seq", 0)) + 1
            card_id = f"lk{seq:03d}"
            self.state["lk_cards_seq"] = seq
            cards = self.state.setdefault("lk_cards", {})
            base = {
                "card_id": card_id,
                "supplier": (fields.get("supplier") or "").lstrip("@"),
                "bank": fields.get("bank") or "",
                "fio": fields.get("fio") or "",
                "price_usdt": float(fields.get("price_usdt") or 0),
                "payment_method": fields.get("payment_method") or "",
                "deal_id": fields.get("deal_id") or "",
                "usdt_address": fields.get("usdt_address") or "",
                "status": fields.get("status") or "В_РАБОТЕ",
                "block_amount_rub": 0.0,
                "block_note": "",
                "brak_reason": "",
                "client_id": int(fields.get("client_id") or 0),
                "client_username": (fields.get("client_username") or "").lstrip("@"),
                "work_chat_id": fields.get("work_chat_id") or 0,
                "lk_group_msg_id": 0,
                "payout_buttons_msg_id": 0,  # msg_id reply'я с кнопками выплаты
                "created_at": time.time(),
                "history": [{
                    "ts": time.time(),
                    "status": fields.get("status") or "В_РАБОТЕ",
                    "by": fields.get("created_by") or "system",
                }],
            }
            cards[card_id] = base
            await self._save_unlocked()
            return card_id

    async def update_lk_card(self, card_id: str, **fields) -> bool:
        async with _lock:
            cards = self.state.get("lk_cards") or {}
            c = cards.get(str(card_id))
            if c is None:
                return False
            # ЗАЩИТА: payment_method можно МЕНЯТЬ только если явно передан флаг
            # _allow_payment_method_change=True. Иначе игнорируем (это спасает
            # карточку от случайного перезатирания методом по умолчанию через
            # шорткаты/бэкфилл).
            allow_pm_change = bool(fields.pop("_allow_payment_method_change", False))
            old_status = (c.get("status") or "").upper()
            for k, v in fields.items():
                if k == "history":
                    continue
                if k == "payment_method" and not allow_pm_change:
                    old = (c.get("payment_method") or "").upper()
                    new = (v or "").upper()
                    if old and new and old != new:
                        try:
                            import logging
                            logging.getLogger(__name__).warning(
                                "REFUSED payment_method change for %s: %s → %s "
                                "(pass _allow_payment_method_change=True если нужно)",
                                card_id, old, new,
                            )
                        except Exception:
                            pass
                        continue
                    # Разрешаем set если поле было пустым ИЛИ значения совпадают
                c[k] = v
            # === АВТО-ЗАЧИСЛЕНИЕ В КОШЕЛЁК ПАРТНЁРА ===
            # Если переход статуса на ЗАВЕРШЁН/ОТРАБОТАН + метод USDT_TRC20
            # + ещё не зачисляли — кладём price_usdt на partner_wallet.
            new_status = (c.get("status") or "").upper()
            method = (c.get("payment_method") or "").upper()
            terminal = {"ЗАВЕРШЁН", "ЗАВЕРШЕН", "ОТРАБОТАН"}
            if (
                old_status != new_status
                and new_status in terminal
                and method == "USDT_TRC20"
                and not c.get("wallet_credited_at")
            ):
                supplier = (c.get("supplier") or "").lstrip("@").strip()
                price = float(c.get("price_usdt") or 0)
                if supplier and price > 0:
                    # Записываем в wallet через прямой доступ (мы УЖЕ внутри _lock)
                    wallets = self.state.setdefault("partner_wallets", {})
                    key = supplier.lower()
                    if key not in wallets:
                        wallets[key] = {
                            "balance_usdt": 0.0, "address_default": "",
                            "history": [], "pending_payouts": [],
                        }
                    w = wallets[key]
                    w["balance_usdt"] = round(float(w.get("balance_usdt") or 0) + price, 6)
                    w.setdefault("history", []).append({
                        "ts": time.time(),
                        "type": "credit",
                        "amount": price,
                        "reason": "lk_payout",
                        "lk_card_id": card_id,
                    })
                    c["wallet_credited_at"] = time.time()
                    c["wallet_credited_amount"] = price
                    try:
                        import logging
                        logging.getLogger(__name__).info(
                            "[wallet] auto-credit @%s +%.2f USDT за %s (%s)",
                            supplier, price, card_id, new_status,
                        )
                    except Exception:
                        pass
            await self._save_unlocked()
            return True

    async def set_lk_card_status(
        self, card_id: str, status: str, **extra
    ) -> bool:
        """Меняет статус + добавляет запись в history. Дополнительные
        поля (block_amount_rub, block_note, brak_reason, deal_id) — в extra."""
        async with _lock:
            cards = self.state.get("lk_cards") or {}
            c = cards.get(str(card_id))
            if c is None:
                return False
            c["status"] = status
            for k, v in extra.items():
                c[k] = v
            c.setdefault("history", []).append({
                "ts": time.time(),
                "status": status,
                "by": extra.get("by") or "system",
                "extra": {k: v for k, v in extra.items() if k != "by"},
            })
            await self._save_unlocked()
            return True

    async def set_lk_card_msg_id(self, card_id: str, msg_id) -> bool:
        async with _lock:
            cards = self.state.get("lk_cards") or {}
            c = cards.get(str(card_id))
            if c is None:
                return False
            c["lk_group_msg_id"] = int(msg_id or 0)
            await self._save_unlocked()
            return True

    # === Заявки V2 (Группа 2 «Бухгалтерия») ===

    def get_applications_v2(self, date_str: str) -> list:
        return list((self.state.get("applications_v2") or {}).get(date_str) or [])

    def list_applications_v2_dates(self) -> list:
        return sorted((self.state.get("applications_v2") or {}).keys())

    async def add_application_v2(self, date_str: str, app_data: dict) -> int:
        """Добавляет заявку. Возвращает её sequence id (1, 2, ... в рамках дня)."""
        async with _lock:
            apps_by_date = self.state.setdefault("applications_v2", {})
            day = apps_by_date.setdefault(date_str, [])
            new_id = len(day) + 1
            entry = {**app_data, "id": new_id, "ts": time.time()}
            day.append(entry)
            await self._save_unlocked()
            return new_id

    async def update_application_v2(
        self, date_str: str, app_id: int, **fields
    ) -> bool:
        """Обновляет произвольные поля заявки на месте. Возвращает True если
        нашли и обновили."""
        async with _lock:
            apps_by_date = self.state.setdefault("applications_v2", {})
            day = apps_by_date.get(date_str) or []
            for app in day:
                if int(app.get("id", 0)) == int(app_id):
                    for k, v in fields.items():
                        app[k] = v
                    await self._save_unlocked()
                    return True
            return False

    async def remove_application_v2(self, date_str: str, app_id: int) -> bool:
        async with _lock:
            apps_by_date = self.state.setdefault("applications_v2", {})
            day = apps_by_date.get(date_str) or []
            for i, app in enumerate(day):
                if int(app.get("id", 0)) == int(app_id):
                    day.pop(i)
                    await self._save_unlocked()
                    return True
            return False

    # === Воронка конверсии (funnel_stats) ===
    # Каждый ключ — отдельный счётчик за день. Ключи:
    #   starts          — нажатий /start в боте
    #   chats_created   — создано work-чатов (бот пригласил клиента)
    #   chats_active    — work-чаты где клиент написал хоть что-то
    #   chats_junk      — work-чаты признанные мусором (ни о чём не спросили)
    #   ip_interest     — клиенты выразившие интерес открыть ИП
    #   bank_interest   — клиенты обсуждавшие конкретные банки
    #   rs_handed       — клиенты сдали РС (карточка ЛК создана)
    #   lk_done         — карточек ЛК переведено в ЗАВЕРШЁН
    #   blocks          — карточек в статусе БЛОК
    #   margin_usdt     — суммарная маржа за день (USDT)

    async def bump_funnel(self, key: str, value: float = 1.0, date_str: str = None):
        """Инкрементирует счётчик воронки на value (по умолчанию +1)."""
        if not key:
            return
        if date_str is None:
            date_str = time.strftime("%Y-%m-%d", time.localtime())
        async with _lock:
            f = self.state.setdefault("funnel_stats", {})
            day = f.setdefault(date_str, {})
            day[key] = float(day.get(key, 0)) + float(value)
            await self._save_unlocked()

    def get_funnel(self, date_str: str = None) -> dict:
        if date_str is None:
            date_str = time.strftime("%Y-%m-%d", time.localtime())
        f = self.state.get("funnel_stats") or {}
        return dict(f.get(date_str) or {})

    def get_funnel_range(self, days: int = 7) -> list:
        """Возвращает список словарей {date, ...counters} за N последних дней."""
        f = self.state.get("funnel_stats") or {}
        out = []
        for i in range(max(1, days)):
            d = time.strftime(
                "%Y-%m-%d", time.localtime(time.time() - i * 86400),
            )
            entry = dict(f.get(d) or {})
            entry["date"] = d
            out.append(entry)
        return out

    # === Стата по менеджерам/работникам ===

    async def bump_manager(self, username: str, key: str, value: float = 1.0):
        """Инкрементирует счётчик активности менеджера/работника."""
        uname = (username or "").lstrip("@").lower().strip()
        if not uname or not key:
            return
        async with _lock:
            ms = self.state.setdefault("manager_stats", {})
            entry = ms.setdefault(uname, {})
            entry[key] = float(entry.get(key, 0)) + float(value)
            entry["last_active_ts"] = time.time()
            await self._save_unlocked()

    def list_manager_stats(self) -> dict:
        """Полная статистика по всем работникам."""
        return dict(self.state.get("manager_stats") or {})

    def get_manager_stat(self, username: str) -> dict:
        """Стата конкретного менеджера."""
        uname = (username or "").lstrip("@").lower().strip()
        return dict((self.state.get("manager_stats") or {}).get(uname) or {})

    # === Предпочтения клиентов (память на уровне @username) ===

    def get_client_preferences(self, username: str) -> dict:
        """Вернуть прошлые предпочтения этого клиента (метод оплаты,
        USDT-адрес и т.д.). Пустой dict если клиент новый."""
        uname = (username or "").lstrip("@").lower().strip()
        if not uname:
            return {}
        prefs = self.state.get("client_preferences") or {}
        return dict(prefs.get(uname) or {})

    async def restore_lk_card(self, card_id: str, fields: dict) -> str:
        """Восстановить карточку с конкретным card_id (например, из истории
        сообщений в Группе 1 после потери state.json).
        Если карточка с таким id уже есть — обновляет поля (без затирания
        history). Иначе создаёт новую с указанным card_id и обновляет
        lk_cards_seq."""
        if not card_id:
            return ""
        cid = str(card_id).lower().lstrip("#")
        async with _lock:
            cards = self.state.setdefault("lk_cards", {})
            existing = cards.get(cid)
            if existing:
                # Обновляем поля кроме служебных
                for k, v in fields.items():
                    if k in ("card_id", "history", "created_at"):
                        continue
                    if v is not None and v != "":
                        existing[k] = v
                await self._save_unlocked()
                return cid
            # Создаём новую с заданным id
            base = {
                "card_id": cid,
                "supplier": (fields.get("supplier") or "").lstrip("@"),
                "bank": fields.get("bank") or "",
                "fio": fields.get("fio") or "",
                "price_usdt": float(fields.get("price_usdt") or 0),
                "payment_method": fields.get("payment_method") or "",
                "deal_id": fields.get("deal_id") or "",
                "usdt_address": fields.get("usdt_address") or "",
                "status": fields.get("status") or "В_РАБОТЕ",
                "block_amount_rub": float(fields.get("block_amount_rub") or 0),
                "block_note": fields.get("block_note") or "",
                "brak_reason": fields.get("brak_reason") or "",
                "client_id": int(fields.get("client_id") or 0),
                "client_username": (fields.get("client_username") or "").lstrip("@"),
                "work_chat_id": fields.get("work_chat_id") or 0,
                "lk_group_msg_id": int(fields.get("lk_group_msg_id") or 0),
                "created_at": float(fields.get("created_at") or time.time()),
                "history": [{
                    "ts": time.time(),
                    "status": fields.get("status") or "В_РАБОТЕ",
                    "by": "sync",
                }],
            }
            cards[cid] = base
            # Обновляем seq если card_id больше текущего
            try:
                num = int(re.sub(r"\D", "", cid))
                if num > int(self.state.get("lk_cards_seq", 0)):
                    self.state["lk_cards_seq"] = num
            except Exception:
                pass
            await self._save_unlocked()
            return cid

    async def save_client_preferences(
        self, username: str, payment_method: str = "",
        usdt_address: str = "", fio: str = "", bank: str = "",
    ) -> bool:
        """Сохраняет/обновляет предпочтения клиента. Инкрементит lk_count
        и обновляет timestamp. Пустые поля не затирают существующие."""
        uname = (username or "").lstrip("@").lower().strip()
        if not uname:
            return False
        async with _lock:
            prefs = self.state.setdefault("client_preferences", {})
            entry = prefs.setdefault(uname, {})
            if payment_method:
                entry["payment_method"] = payment_method.upper()
            if usdt_address:
                entry["usdt_address"] = usdt_address.strip()
            if fio:
                entry["fio_last"] = fio.strip()
            if bank:
                entry["bank_last"] = bank.strip()
            entry["last_updated_ts"] = time.time()
            entry["lk_count"] = int(entry.get("lk_count", 0)) + 1
            await self._save_unlocked()
            return True


    # === Bot users registry (для рассылок) ===

    async def track_bot_user(
        self, user_id: int, first_name: str = "", username: str = "",
    ):
        """Запомнить пользователя бота. Зовётся из /start."""
        if not user_id:
            return
        uid = str(int(user_id))
        async with _lock:
            users = self.state.setdefault("bot_users", {})
            entry = users.setdefault(uid, {})
            now = time.time()
            entry.setdefault("first_seen_ts", now)
            entry["last_seen_ts"] = now
            if first_name:
                entry["first_name"] = first_name
            if username:
                entry["username"] = (username or "").lstrip("@")
            entry.setdefault("entered_work_chat", False)
            await self._save_unlocked()

    async def mark_user_entered_work_chat(self, user_id: int):
        """Пометить что пользователь зашёл в свою work-беседу."""
        if not user_id:
            return
        uid = str(int(user_id))
        async with _lock:
            users = self.state.setdefault("bot_users", {})
            entry = users.get(uid)
            if entry is not None:
                entry["entered_work_chat"] = True
                await self._save_unlocked()

    def list_bot_users(self) -> dict:
        return dict(self.state.get("bot_users") or {})

    def list_inactive_bot_users(self) -> list:
        """Юзеры которые нажали /start но ещё не вошли в work-чат."""
        out = []
        for uid, info in (self.state.get("bot_users") or {}).items():
            if not info.get("entered_work_chat"):
                out.append({"user_id": int(uid), **info})
        return out

    # === Dashboard commands queue ===

    async def enqueue_dashboard_command(self, text: str, source: str = "dashboard") -> dict:
        """Добавляет команду в очередь для userbot."""
        if not text or not text.strip():
            return {}
        async with _lock:
            q = self.state.setdefault("dashboard_commands", [])
            entry = {
                "id": int(time.time() * 1000),
                "ts": time.time(),
                "text": text.strip(),
                "status": "pending",
                "result": "",
                "source": source,
            }
            q.append(entry)
            # храним не больше 200
            if len(q) > 200:
                self.state["dashboard_commands"] = q[-200:]
            await self._save_unlocked()
            return dict(entry)

    def get_pending_dashboard_commands(self) -> list:
        return [
            c for c in (self.state.get("dashboard_commands") or [])
            if c.get("status") == "pending"
        ]

    def list_dashboard_commands(self, limit: int = 50) -> list:
        q = list(self.state.get("dashboard_commands") or [])
        return q[-limit:][::-1]

    async def mark_dashboard_command_done(
        self, cmd_id: int, result: str = "", status: str = "done",
    ):
        async with _lock:
            for c in self.state.get("dashboard_commands") or []:
                if int(c.get("id", 0)) == int(cmd_id):
                    c["status"] = status
                    c["result"] = (result or "")[:1500]
                    c["finished_ts"] = time.time()
                    break
            await self._save_unlocked()


    # === LEO Notes (заметки от голосового LEO с записью в knowledge graph) ===

    async def add_leo_note(
        self, text: str, category: str = "fact", priority: str = "normal",
        source: str = "voice", author: str = "", tags: list = None,
    ) -> dict:
        """Сохраняет заметку. Возвращает созданную запись."""
        if not text or not text.strip():
            return {}
        async with _lock:
            notes = self.state.setdefault("leo_notes", [])
            entry = {
                "id": int(time.time() * 1000),
                "ts": time.time(),
                "category": (category or "fact").lower(),
                "priority": (priority or "normal").lower(),
                "text": text.strip()[:2000],
                "source": source or "voice",
                "author": author or "",
                "tags": list(tags or []),
                "synced_to_knowledge": False,
                "knowledge_url": "",
            }
            notes.append(entry)
            # Храним последние 1000 заметок (старше — обрезаем)
            if len(notes) > 1000:
                self.state["leo_notes"] = notes[-1000:]
            await self._save_unlocked()
            return dict(entry)

    async def mark_leo_note_synced(self, note_id: int, knowledge_url: str):
        async with _lock:
            for n in self.state.get("leo_notes") or []:
                if int(n.get("id", 0)) == int(note_id):
                    n["synced_to_knowledge"] = True
                    n["knowledge_url"] = knowledge_url or ""
                    break
            await self._save_unlocked()

    def list_leo_notes(self, limit: int = 100, category: str = None) -> list:
        notes = list(self.state.get("leo_notes") or [])
        if category:
            notes = [n for n in notes if (n.get("category") or "") == category]
        return notes[-limit:][::-1]  # последние первыми

    async def delete_leo_note(self, note_id: int) -> bool:
        async with _lock:
            notes = self.state.get("leo_notes") or []
            new = [n for n in notes if int(n.get("id", 0)) != int(note_id)]
            if len(new) == len(notes):
                return False
            self.state["leo_notes"] = new
            await self._save_unlocked()
            return True


    # ===== OUTREACH HELPERS =====

    async def add_outreach_bot(self, **fields) -> dict:
        async with _lock:
            bots = self.state.setdefault("outreach_bots", [])
            entry = {
                "id": int(time.time() * 1000),
                "phone": fields.get("phone") or "",
                "session_string": fields.get("session_string") or "",
                "name": fields.get("name") or "",
                "tg_user_id": fields.get("tg_user_id") or 0,
                "tg_username": fields.get("tg_username") or "",
                "status": fields.get("status") or "active",
                "sent_today": 0,
                "last_send_ts": 0,
                "flood_wait_until": 0,
                "created_at": time.time(),
            }
            bots.append(entry)
            await self._save_unlocked()
            return dict(entry)

    def list_outreach_bots(self) -> list:
        return list(self.state.get("outreach_bots") or [])

    def get_outreach_bot(self, bot_id: int) -> Optional[dict]:
        for b in self.state.get("outreach_bots") or []:
            if int(b.get("id", 0)) == int(bot_id):
                return dict(b)
        return None

    async def update_outreach_bot(self, bot_id: int, **fields) -> bool:
        async with _lock:
            for b in self.state.get("outreach_bots") or []:
                if int(b.get("id", 0)) == int(bot_id):
                    for k, v in fields.items():
                        b[k] = v
                    await self._save_unlocked()
                    return True
            return False

    async def delete_outreach_bot(self, bot_id: int) -> bool:
        async with _lock:
            bots = self.state.get("outreach_bots") or []
            new = [b for b in bots if int(b.get("id", 0)) != int(bot_id)]
            if len(new) == len(bots):
                return False
            self.state["outreach_bots"] = new
            await self._save_unlocked()
            return True

    async def set_pending_auth(self, phone: str, data: dict):
        async with _lock:
            pa = self.state.setdefault("outreach_pending_auth", {})
            pa[phone] = {**data, "ts": time.time()}
            await self._save_unlocked()

    def get_pending_auth(self, phone: str) -> Optional[dict]:
        return (self.state.get("outreach_pending_auth") or {}).get(phone)

    async def clear_pending_auth(self, phone: str):
        async with _lock:
            pa = self.state.setdefault("outreach_pending_auth", {})
            pa.pop(phone, None)
            await self._save_unlocked()

    # === Campaigns ===

    async def add_outreach_campaign(self, **fields) -> dict:
        async with _lock:
            campaigns = self.state.setdefault("outreach_campaigns", [])
            entry = {
                "id": int(time.time() * 1000),
                "name": fields.get("name") or f"Campaign #{len(campaigns)+1}",
                "text": fields.get("text") or "",
                "targets": list(fields.get("targets") or []),
                "manager_username": (fields.get("manager_username") or "").lstrip("@"),
                "rate_per_hour": int(fields.get("rate_per_hour") or 20),
                "jitter_min_sec": int(fields.get("jitter_min_sec") or 90),
                "jitter_max_sec": int(fields.get("jitter_max_sec") or 240),
                "active_hours_from": int(fields.get("active_hours_from") or 9),
                "active_hours_to": int(fields.get("active_hours_to") or 21),
                "status": "draft",  # draft / running / paused / done
                "stats": {
                    "sent": 0, "errors": 0, "replied": 0,
                    "transferred": 0, "skipped": 0,
                },
                "created_at": time.time(),
                "started_at": 0,
                "finished_at": 0,
            }
            campaigns.append(entry)
            await self._save_unlocked()
            return dict(entry)

    def list_outreach_campaigns(self) -> list:
        return list(self.state.get("outreach_campaigns") or [])

    def get_outreach_campaign(self, cid: int) -> Optional[dict]:
        for c in self.state.get("outreach_campaigns") or []:
            if int(c.get("id", 0)) == int(cid):
                return dict(c)
        return None

    async def update_outreach_campaign(self, cid: int, **fields) -> bool:
        async with _lock:
            for c in self.state.get("outreach_campaigns") or []:
                if int(c.get("id", 0)) == int(cid):
                    for k, v in fields.items():
                        if k == "stats" and isinstance(v, dict):
                            stats = c.setdefault("stats", {})
                            for sk, sv in v.items():
                                stats[sk] = (stats.get(sk, 0) or 0) + (sv if isinstance(sv, (int, float)) else 0)
                        else:
                            c[k] = v
                    await self._save_unlocked()
                    return True
            return False

    async def delete_outreach_campaign(self, cid: int) -> bool:
        async with _lock:
            campaigns = self.state.get("outreach_campaigns") or []
            new = [c for c in campaigns if int(c.get("id", 0)) != int(cid)]
            if len(new) == len(campaigns):
                return False
            self.state["outreach_campaigns"] = new
            await self._save_unlocked()
            return True

    # === Messages & responses ===

    async def add_outreach_message(self, **fields):
        async with _lock:
            msgs = self.state.setdefault("outreach_messages", [])
            msgs.append({
                "id": int(time.time() * 1000),
                "ts": time.time(),
                **fields,
            })
            if len(msgs) > 5000:
                self.state["outreach_messages"] = msgs[-5000:]
            await self._save_unlocked()

    def list_outreach_messages(self, campaign_id: int = None, limit: int = 200) -> list:
        msgs = list(self.state.get("outreach_messages") or [])
        if campaign_id is not None:
            msgs = [m for m in msgs if int(m.get("campaign_id", 0)) == int(campaign_id)]
        return msgs[-limit:][::-1]

    def was_target_sent(self, campaign_id: int, target_chat_id) -> bool:
        for m in self.state.get("outreach_messages") or []:
            if (int(m.get("campaign_id", 0)) == int(campaign_id)
                    and str(m.get("target_chat_id")) == str(target_chat_id)
                    and m.get("status") == "sent"):
                return True
        return False

    async def add_outreach_response(self, **fields) -> dict:
        async with _lock:
            resps = self.state.setdefault("outreach_responses", [])
            entry = {
                "id": int(time.time() * 1000),
                "ts": time.time(),
                "handled": False,
                **fields,
            }
            resps.append(entry)
            if len(resps) > 5000:
                self.state["outreach_responses"] = resps[-5000:]
            await self._save_unlocked()
            return dict(entry)

    def list_outreach_responses(
        self, handled: bool = None, intent: str = None, limit: int = 200,
    ) -> list:
        resps = list(self.state.get("outreach_responses") or [])
        if handled is not None:
            resps = [r for r in resps if bool(r.get("handled")) == handled]
        if intent:
            resps = [r for r in resps if (r.get("ai_intent") or "") == intent]
        return resps[-limit:][::-1]

    async def mark_outreach_response(self, resp_id: int, **fields) -> bool:
        async with _lock:
            for r in self.state.get("outreach_responses") or []:
                if int(r.get("id", 0)) == int(resp_id):
                    for k, v in fields.items():
                        r[k] = v
                    await self._save_unlocked()
                    return True
            return False

    # ============================================================
    # CRM Bot — поставщики, дропы, ЛК банков
    # ============================================================

    # ---- CONFIG ----
    def get_crm_config(self) -> dict:
        return dict(self.state.get("crm_config") or {})

    async def set_crm_config(self, **fields):
        async with _lock:
            cfg = self.state.setdefault("crm_config", {})
            for k, v in fields.items():
                cfg[k] = v
            await self._save_unlocked()

    # ---- OWNERS (поставщики) ----
    def list_crm_owners(self) -> dict:
        return dict(self.state.get("crm_owners") or {})

    def get_crm_owner(self, owner_id) -> Optional[dict]:
        return (self.state.get("crm_owners") or {}).get(str(owner_id))

    def find_crm_owner_by_tg(self, tg_user_id: int) -> Optional[dict]:
        for oid, o in (self.state.get("crm_owners") or {}).items():
            if int(o.get("tg_user_id") or 0) == int(tg_user_id):
                return dict(o, owner_id=oid)
        return None

    def find_crm_owner_by_username(self, username: str) -> Optional[dict]:
        u = (username or "").lstrip("@").lower().strip()
        if not u:
            return None
        for oid, o in (self.state.get("crm_owners") or {}).items():
            if (o.get("username") or "").lower() == u:
                return dict(o, owner_id=oid)
        return None

    async def add_crm_owner(
        self, tg_user_id: int, username: str, name: str = "",
        work_chat_id: Optional[int] = None,
    ) -> str:
        async with _lock:
            seq = int(self.state.get("crm_owners_seq", 0)) + 1
            self.state["crm_owners_seq"] = seq
            owner_id = f"o{seq:03d}"
            self.state.setdefault("crm_owners", {})[owner_id] = {
                "owner_id": owner_id,
                "tg_user_id": int(tg_user_id),
                "username": (username or "").lstrip("@"),
                "name": name or "",
                "joined_at": time.time(),
                "last_active_ts": time.time(),
                "total_drops": 0,
                "total_revenue_usd": 0.0,
                "rating": 5.0,
                "banned_until": 0,
                "work_chat_id": work_chat_id,
            }
            await self._save_unlocked()
            return owner_id

    async def update_crm_owner(self, owner_id: str, **fields) -> bool:
        async with _lock:
            o = (self.state.get("crm_owners") or {}).get(str(owner_id))
            if not o:
                return False
            for k, v in fields.items():
                o[k] = v
            o["last_active_ts"] = time.time()
            await self._save_unlocked()
            return True

    # ---- CHATS (CRM-чаты) ----
    def get_crm_chat(self, chat_id) -> Optional[dict]:
        key = _norm_chat_id(chat_id)
        return (self.state.get("crm_chats") or {}).get(key)

    def list_crm_chats(self) -> dict:
        return dict(self.state.get("crm_chats") or {})

    async def register_crm_chat(
        self, chat_id, owner_id: str,
        is_admin: bool = False, is_password: bool = False, is_otr: bool = False,
    ):
        key = _norm_chat_id(chat_id)
        async with _lock:
            self.state.setdefault("crm_chats", {})[key] = {
                "chat_id": int(chat_id),
                "owner_id": owner_id,
                "is_admin": bool(is_admin),
                "is_password": bool(is_password),
                "is_otr": bool(is_otr),
                "registered_at": time.time(),
            }
            await self._save_unlocked()

    async def unregister_crm_chat(self, chat_id) -> bool:
        key = _norm_chat_id(chat_id)
        async with _lock:
            chats = self.state.get("crm_chats") or {}
            if key in chats:
                del chats[key]
                await self._save_unlocked()
                return True
            return False

    def find_crm_admin_chat(self) -> Optional[int]:
        """Возвращает chat_id админ-чата CRM (куда падают новые дропы)."""
        for k, c in (self.state.get("crm_chats") or {}).items():
            if c.get("is_admin"):
                return int(c.get("chat_id") or 0)
        return None

    def find_crm_password_chat(self) -> Optional[int]:
        """Возвращает chat_id password-чата (где заполняют RDP/пароли)."""
        for k, c in (self.state.get("crm_chats") or {}).items():
            if c.get("is_password"):
                return int(c.get("chat_id") or 0)
        return None

    # ---- DROPS (клиенты) ----
    def list_crm_drops(self, owner_id: Optional[str] = None) -> dict:
        drops = self.state.get("crm_drops") or {}
        if owner_id is None:
            return dict(drops)
        return {k: v for k, v in drops.items() if v.get("owner_id") == owner_id}

    def get_crm_drop(self, drop_id) -> Optional[dict]:
        return (self.state.get("crm_drops") or {}).get(str(drop_id))

    async def add_crm_drop(
        self, owner_id: str, fio: str, work_chat_id: Optional[int] = None,
    ) -> str:
        async with _lock:
            seq = int(self.state.get("crm_drops_seq", 0)) + 1
            self.state["crm_drops_seq"] = seq
            drop_id = f"d{seq:04d}"
            self.state.setdefault("crm_drops", {})[drop_id] = {
                "drop_id": drop_id,
                "owner_id": owner_id,
                "work_chat_id": work_chat_id,
                "fio": (fio or "").strip(),
                "about": "",
                "scan_file_ids": [],
                "price_usdt": 0,
                "status": "draft",     # draft / pending / accepted / done / brak
                "created_at": time.time(),
                "accept_ts": 0,
                "send_ts": 0,
                "done_ts": 0,
                "prolit_count": 0,
                "admin_msg_id": 0,
                "owner_msg_id": 0,
                "lk_card_ids": [],     # связь с нашими lk_cards (после «в работу»)
            }
            # bump owner's drop count
            o = self.state.get("crm_owners", {}).get(owner_id)
            if o:
                o["total_drops"] = int(o.get("total_drops", 0)) + 1
            await self._save_unlocked()
            return drop_id

    async def update_crm_drop(self, drop_id: str, **fields) -> bool:
        async with _lock:
            d = (self.state.get("crm_drops") or {}).get(str(drop_id))
            if not d:
                return False
            # Audit log: записываем что изменилось (без больших полей и без
            # служебных msg_id'ов чтобы не шуметь)
            history = d.setdefault("history", [])
            audit_fields = {
                k: v for k, v in fields.items()
                if k not in ("admin_msg_id", "owner_msg_id", "last_remind_ts")
                and not isinstance(v, (list, dict))
            }
            if audit_fields:
                history.append({
                    "ts": time.time(),
                    "fields": audit_fields,
                })
                # Ограничиваем размер истории
                if len(history) > 50:
                    d["history"] = history[-50:]
            for k, v in fields.items():
                d[k] = v
            await self._save_unlocked()
            return True

    async def add_crm_drop_history(self, drop_id: str, action: str, details: dict = None):
        """Внешний хук для записи произвольных action'ов в history дропа."""
        async with _lock:
            d = (self.state.get("crm_drops") or {}).get(str(drop_id))
            if not d:
                return False
            history = d.setdefault("history", [])
            history.append({
                "ts": time.time(),
                "action": action,
                "details": details or {},
            })
            if len(history) > 50:
                d["history"] = history[-50:]
            await self._save_unlocked()
            return True

    async def delete_crm_drop(self, drop_id: str) -> bool:
        async with _lock:
            drops = self.state.get("crm_drops") or {}
            d = drops.get(str(drop_id))
            if not d:
                return False
            # удалить все ЛК дропа
            lks = self.state.get("crm_drop_lks") or {}
            for lkid in list(lks.keys()):
                if lks[lkid].get("drop_id") == drop_id:
                    del lks[lkid]
            del drops[str(drop_id)]
            await self._save_unlocked()
            return True

    # ---- DROP LKs (ЛК банков) ----
    def list_crm_drop_lks(self, drop_id: Optional[str] = None) -> dict:
        lks = self.state.get("crm_drop_lks") or {}
        if drop_id is None:
            return dict(lks)
        return {k: v for k, v in lks.items() if v.get("drop_id") == drop_id}

    def get_crm_drop_lk(self, droplk_id) -> Optional[dict]:
        return (self.state.get("crm_drop_lks") or {}).get(str(droplk_id))

    async def add_crm_drop_lk(
        self, drop_id: str, owner_id: str, bank: str, value: str = "",
    ) -> str:
        async with _lock:
            seq = int(self.state.get("crm_drop_lks_seq", 0)) + 1
            self.state["crm_drop_lks_seq"] = seq
            lkid = f"lk{seq:04d}"
            self.state.setdefault("crm_drop_lks", {})[lkid] = {
                "droplk_id": lkid,
                "drop_id": drop_id,
                "owner_id": owner_id,
                "bank": (bank or "").upper().strip(),
                "value": (value or "").strip(),
                "deal": "",
                "sms_history": [],
                "status": "new",   # new / pending / ready / done
                "new_login": "",        # новый логин (новые данные после перевязки)
                "new_password": "",
                "new_mail": "",
                "new_number": "",
                "code_word": "",
                "ded_ip": "",
                "ded_login": "Administrator",
                "ded_pass": "",
                "ded_location": "",
                "link_pass": "",
                "msgid_pass": 0,
                "sms_stage": "",
                "sms_login_code": "",
                "sms_perevyaz_code": "",
                "sms_tracker_msg_id": 0,
                "created_at": time.time(),
            }
            await self._save_unlocked()
            return lkid

    async def update_crm_drop_lk(self, droplk_id, **fields):
        async with _lock:
            lk = (self.state.get("crm_drop_lks") or {}).get(str(droplk_id))
            if not lk:
                return False
            for k, v in fields.items():
                lk[k] = v
            await self._save_unlocked()
            return True

    async def delete_crm_drop_lk(self, droplk_id):
        async with _lock:
            lks = self.state.get("crm_drop_lks") or {}
            if str(droplk_id) in lks:
                del lks[str(droplk_id)]
                await self._save_unlocked()
                return True
            return False

    async def append_crm_sms(self, droplk_id, code, time_str=""):
        async with _lock:
            lk = (self.state.get("crm_drop_lks") or {}).get(str(droplk_id))
            if not lk:
                return False
            sms = lk.setdefault("sms_history", [])
            sms.append({"code": code, "time": time_str or time.strftime("%d.%m.%Y %H:%M")})
            await self._save_unlocked()
            return True

    def get_crm_fsm(self, tg_user_id):
        return (self.state.get("crm_fsm") or {}).get(str(tg_user_id)) or {}

    async def set_crm_fsm(self, tg_user_id, action=None, data=None, msg_id=None, chat_id=None):
        async with _lock:
            fsm = self.state.setdefault("crm_fsm", {})
            if action is None:
                fsm.pop(str(tg_user_id), None)
            else:
                fsm[str(tg_user_id)] = {
                    "action": action, "data": dict(data or {}),
                    "msg_id": msg_id, "chat_id": chat_id,
                    "updated_at": time.time(),
                    "expires_at": time.time() + 1800,
                }
            await self._save_unlocked()

    async def clear_crm_fsm(self, tg_user_id):
        await self.set_crm_fsm(tg_user_id, action=None)

    # =====================================================================
    # KUC (Кружок Удостоверения Клиента — KYC через одноразовую ссылку)
    # =====================================================================
    def list_kuc_requests(self) -> dict:
        return self.state.get("kuc_requests") or {}

    def get_kuc_request(self, token: str) -> Optional[dict]:
        return (self.state.get("kuc_requests") or {}).get(str(token))

    def get_kuc_for_droplk(self, droplk_id: str) -> Optional[dict]:
        """Возвращает последний (активный) КУЦ-запрос для этого ЛК.
        Активный = status в ('pending', 'submitted'). Если все decided — возвращает последний по created_at."""
        kucs = self.state.get("kuc_requests") or {}
        matching = [k for k in kucs.values() if k.get("droplk_id") == str(droplk_id)]
        if not matching:
            return None
        active = [k for k in matching if k.get("status") in ("pending", "submitted")]
        if active:
            return sorted(active, key=lambda k: -(k.get("created_at") or 0))[0]
        return sorted(matching, key=lambda k: -(k.get("created_at") or 0))[0]

    def list_kuc_for_droplk(self, droplk_id: str) -> list:
        """Возвращает ВСЕ КУЦ-запросы этого ЛК (история), отсортированы по created_at desc."""
        kucs = self.state.get("kuc_requests") or {}
        matching = [k for k in kucs.values() if k.get("droplk_id") == str(droplk_id)]
        return sorted(matching, key=lambda k: -(k.get("created_at") or 0))

    async def create_kuc_request(
        self, droplk_id: str, work_chat_id: Optional[int],
        requested_by: str, request_text: str = "",
    ) -> str:
        """Создаёт новый KUC-запрос. Возвращает token."""
        import uuid as _uuid
        async with _lock:
            token = _uuid.uuid4().hex[:24]
            kucs = self.state.setdefault("kuc_requests", {})
            kucs[token] = {
                "token": token,
                "droplk_id": str(droplk_id),
                "work_chat_id": int(work_chat_id or 0),
                "requested_by": (requested_by or "").lstrip("@").lower(),
                "request_text": request_text or "",
                "created_at": time.time(),
                "url_sent_at": 0,
                "used_at": 0,
                "submitted_at": 0,
                "status": "pending",
                "video_file_path": "",
                "video_size_bytes": 0,
                "video_mime": "",
                "ai_score": 0,
                "ai_comment": "",
                "decision_by": "",
                "decision_at": 0,
                "decision_note": "",
            }
            await self._save_unlocked()
            return token

    async def mark_kuc_url_sent(self, token: str) -> bool:
        async with _lock:
            k = (self.state.get("kuc_requests") or {}).get(str(token))
            if not k:
                return False
            k["url_sent_at"] = time.time()
            await self._save_unlocked()
            return True

    async def mark_kuc_opened(self, token: str) -> bool:
        async with _lock:
            k = (self.state.get("kuc_requests") or {}).get(str(token))
            if not k:
                return False
            if not k.get("used_at"):
                k["used_at"] = time.time()
                await self._save_unlocked()
            return True

    async def mark_kuc_submitted(
        self, token: str, video_file_path: str,
        video_size_bytes: int = 0, video_mime: str = "",
    ) -> bool:
        async with _lock:
            k = (self.state.get("kuc_requests") or {}).get(str(token))
            if not k:
                return False
            k["video_file_path"] = video_file_path or ""
            k["video_size_bytes"] = int(video_size_bytes or 0)
            k["video_mime"] = video_mime or ""
            k["submitted_at"] = time.time()
            k["status"] = "submitted"
            await self._save_unlocked()
            return True

    async def decide_kuc(
        self, token: str, decision: str, decision_by: str = "",
        decision_note: str = "",
    ) -> bool:
        """decision: 'approved' | 'rejected'"""
        if decision not in ("approved", "rejected"):
            return False
        async with _lock:
            k = (self.state.get("kuc_requests") or {}).get(str(token))
            if not k:
                return False
            k["status"] = decision
            k["decision_by"] = (decision_by or "").lstrip("@").lower()
            k["decision_at"] = time.time()
            k["decision_note"] = decision_note or ""
            await self._save_unlocked()
            return True

    async def set_kuc_ai_result(
        self, token: str, score: float, comment: str = "",
    ) -> bool:
        """Записать результат AI-проверки лица (Этап B — Claude Vision)."""
        async with _lock:
            k = (self.state.get("kuc_requests") or {}).get(str(token))
            if not k:
                return False
            k["ai_score"] = float(score or 0)
            k["ai_comment"] = comment or ""
            await self._save_unlocked()
            return True

    # ════════════════════════════════════════════════════════════
    # OUTSOURCE — лавка PRIDE (@marketplace_PRIDE_BOT)
    # ════════════════════════════════════════════════════════════
    async def register_outsource_manager(self, username: str, tg_user_id: int) -> None:
        """Регистрирует управляющего при первом /start. Идемпотентна."""
        username = (username or "").lstrip("@").lower()
        if not username:
            return
        async with _lock:
            mgrs = self.state.setdefault("outsource_managers", {})
            now = time.time()
            existing = mgrs.get(username)
            if existing:
                existing["last_active_ts"] = now
                if tg_user_id and not existing.get("tg_user_id"):
                    existing["tg_user_id"] = int(tg_user_id)
            else:
                mgrs[username] = {
                    "tg_user_id": int(tg_user_id or 0),
                    "first_seen_ts": now,
                    "last_active_ts": now,
                    "wallet_balance_usdt": 0.0,
                    "paid_total_usdt": 0.0,
                    "stats": {"drops_total": 0, "lks_total": 0, "lks_done": 0},
                }
            await self._save_unlocked()

    def get_outsource_manager(self, username: str) -> Optional[dict]:
        username = (username or "").lstrip("@").lower()
        return (self.state.get("outsource_managers") or {}).get(username)

    def list_outsource_managers(self) -> dict:
        return dict(self.state.get("outsource_managers") or {})

    def list_outsource_drops(self) -> dict:
        return dict(self.state.get("outsource_drops") or {})

    def list_outsource_drop_lks(self) -> dict:
        return dict(self.state.get("outsource_drop_lks") or {})

    def get_outsource_drop(self, drop_id) -> Optional[dict]:
        if not drop_id:
            return None
        return (self.state.get("outsource_drops") or {}).get(str(drop_id))

    def get_outsource_drop_lk(self, droplk_id) -> Optional[dict]:
        if not droplk_id:
            return None
        return (self.state.get("outsource_drop_lks") or {}).get(str(droplk_id))

    async def update_outsource_drop_lk(self, droplk_id, **fields) -> bool:
        if not droplk_id:
            return False
        async with _lock:
            lks = self.state.get("outsource_drop_lks") or {}
            lk = lks.get(str(droplk_id))
            if not lk:
                return False
            for k, v in fields.items():
                lk[k] = v
            await self._save_unlocked()
            return True

    async def update_outsource_drop(self, drop_id, **fields) -> bool:
        if not drop_id:
            return False
        async with _lock:
            drops = self.state.get("outsource_drops") or {}
            drop = drops.get(str(drop_id))
            if not drop:
                return False
            for k, v in fields.items():
                drop[k] = v
            await self._save_unlocked()
            return True

    async def add_outsource_drop(self, **fields) -> str:
        """Создаёт анкету в outsource. id с префиксом 'odrp'."""
        async with _lock:
            self.state["outsource_drops_seq"] = int(self.state.get("outsource_drops_seq") or 0) + 1
            new_id = f"odrp{self.state['outsource_drops_seq']}"
            drops = self.state.setdefault("outsource_drops", {})
            drops[new_id] = {"id": new_id, "created_at": time.time(), **fields}
            await self._save_unlocked()
            return new_id

    async def add_outsource_drop_lk(self, drop_id: str, **fields) -> str:
        """Создаёт ЛК в outsource. id с префиксом 'olk'."""
        async with _lock:
            self.state["outsource_drop_lks_seq"] = int(self.state.get("outsource_drop_lks_seq") or 0) + 1
            new_id = f"olk{self.state['outsource_drop_lks_seq']}"
            lks = self.state.setdefault("outsource_drop_lks", {})
            lks[new_id] = {
                "id": new_id,
                "outsource_drop_id": drop_id,
                "created_at": time.time(),
                **fields,
            }
            await self._save_unlocked()
            return new_id

    async def update_outsource_manager_balance(
        self, username: str, delta: float = 0.0, paid_delta: float = 0.0,
    ) -> Optional[dict]:
        """Атомарно меняет баланс управляющего. Возвращает обновлённую запись."""
        username = (username or "").lstrip("@").lower()
        if not username:
            return None
        async with _lock:
            mgr = (self.state.get("outsource_managers") or {}).get(username)
            if not mgr:
                return None
            mgr["wallet_balance_usdt"] = float(mgr.get("wallet_balance_usdt") or 0) + float(delta)
            if paid_delta:
                mgr["paid_total_usdt"] = float(mgr.get("paid_total_usdt") or 0) + float(paid_delta)
            mgr["last_active_ts"] = time.time()
            await self._save_unlocked()
            return mgr

    async def append_outsource_sms(self, droplk_id, code, time_str: str = "") -> bool:
        """Добавить SMS-код в historу ЛК (для outsource SMS API)."""
        if not droplk_id:
            return False
        async with _lock:
            lk = (self.state.get("outsource_drop_lks") or {}).get(str(droplk_id))
            if not lk:
                return False
            sms = lk.setdefault("sms_history", [])
            sms.append({"code": str(code), "time_str": time_str, "ts": time.time()})
            await self._save_unlocked()
            return True

    # ════════════════════════════════════════════════════════════
    # OUTSOURCE BUNDLES — связки ЛК (продаются одним пакетом)
    # ════════════════════════════════════════════════════════════
    def list_outsource_bundles(self) -> dict:
        return dict(self.state.get("outsource_bundles") or {})

    def get_outsource_bundle(self, bundle_id) -> Optional[dict]:
        if not bundle_id:
            return None
        return (self.state.get("outsource_bundles") or {}).get(str(bundle_id))

    async def create_outsource_bundle(
        self, lk_ids: list, list_price_usdt: float,
        name: str = "", created_by: str = "",
    ) -> Optional[str]:
        """Создаёт связку из >=2 ЛК.

        Все ЛК должны быть в пуле outsource (in_pool=True, manager_username пустой,
        не в другой связке). Возвращает bundle_id или None если что-то не так.
        """
        ids = [str(x) for x in (lk_ids or []) if x]
        if len(ids) < 2:
            return None
        try:
            price = float(list_price_usdt or 0)
        except Exception:
            price = 0.0
        if price <= 0:
            return None
        async with _lock:
            lks = self.state.get("outsource_drop_lks") or {}
            # Валидация всех ЛК
            for lkid in ids:
                lk = lks.get(lkid)
                if not lk:
                    return None
                if not lk.get("in_pool"):
                    return None
                if lk.get("manager_username"):
                    return None
                if lk.get("bundle_id"):
                    return None
            # OK, создаём связку
            self.state["outsource_bundles_seq"] = int(self.state.get("outsource_bundles_seq") or 0) + 1
            bundle_id = f"obnd{self.state['outsource_bundles_seq']}"
            bundles = self.state.setdefault("outsource_bundles", {})
            now = time.time()
            bundles[bundle_id] = {
                "id": bundle_id,
                "name": (name or "").strip(),
                "list_price_usdt": price,
                "lk_ids": ids,
                "in_pool": True,
                "manager_username": "",
                "bought_at": 0,
                "created_at": now,
                "created_by": (created_by or "").lstrip("@").lower(),
            }
            # Помечаем каждый ЛК что он в связке (но in_pool остаётся True для bundle учёта)
            for lkid in ids:
                lks[lkid]["bundle_id"] = bundle_id
            await self._save_unlocked()
            return bundle_id

    async def dissolve_outsource_bundle(self, bundle_id) -> bool:
        """Расформировывает связку — ЛК возвращаются в общий пул как одиночки.
        Доступно только пока bundle.in_pool=True (не куплен).
        """
        if not bundle_id:
            return False
        async with _lock:
            bundles = self.state.get("outsource_bundles") or {}
            bundle = bundles.get(str(bundle_id))
            if not bundle:
                return False
            if not bundle.get("in_pool"):
                return False  # уже куплен, нельзя
            lks = self.state.get("outsource_drop_lks") or {}
            for lkid in bundle.get("lk_ids", []):
                lk = lks.get(str(lkid))
                if lk and lk.get("bundle_id") == str(bundle_id):
                    lk.pop("bundle_id", None)
            del bundles[str(bundle_id)]
            await self._save_unlocked()
            return True

    # ════════════════════════════════════════════════════════════
    # OUTSOURCE ОПЛАТА (TRC20 USDT) — корп-кошелёк + top-up requests
    # ════════════════════════════════════════════════════════════
    def get_outsource_corp_wallet(self) -> str:
        return (self.state.get("outsource_corp_wallet_trc20") or "").strip()

    async def set_outsource_corp_wallet(self, address: str) -> bool:
        address = (address or "").strip()
        # Минимальная валидация TRC20 (начинается с T, длина 34)
        if address and (not address.startswith("T") or len(address) != 34):
            return False
        async with _lock:
            self.state["outsource_corp_wallet_trc20"] = address
            await self._save_unlocked()
            return True

    def get_outsource_tron_last_ts(self) -> int:
        return int(self.state.get("outsource_tron_last_ts_ms") or 0)

    async def set_outsource_tron_last_ts(self, ts_ms: int) -> None:
        async with _lock:
            self.state["outsource_tron_last_ts_ms"] = int(ts_ms or 0)
            await self._save_unlocked()

    async def create_outsource_topup_request(
        self, username: str, base_amount: float, ttl_seconds: int = 1800,
    ) -> Optional[dict]:
        """Создаёт запрос на пополнение с уникальной суммой (base + рандом 0.0001..0.9999).

        Returns dict с request_id, unique_amount, expires_at — или None если что-то не так.
        """
        username = (username or "").lstrip("@").lower()
        if not username:
            return None
        try:
            base = float(base_amount)
        except Exception:
            return None
        if base <= 0:
            return None
        async with _lock:
            # Генерим уникальную сумму — гарантируем что её ещё нет в pending
            # Точность 4 знака (=0.0001 USDT), диапазон 0.0001..0.9999 → 9999 уникальных значений
            existing_pending_amounts = set()
            now = time.time()
            for r in (self.state.get("outsource_topup_requests") or {}).values():
                if r.get("status") == "pending" and r.get("expires_at", 0) > now:
                    existing_pending_amounts.add(round(float(r.get("unique_amount") or 0), 4))
            # Пытаемся 50 раз найти свободную сумму
            unique_amount = None
            for _ in range(50):
                tail = secrets.randbelow(9999) + 1  # 1..9999
                cand = round(base + tail / 10000.0, 4)
                if cand not in existing_pending_amounts:
                    unique_amount = cand
                    break
            if unique_amount is None:
                return None  # Слишком много pending одновременно — крайне редко
            self.state["outsource_topup_seq"] = int(self.state.get("outsource_topup_seq") or 0) + 1
            request_id = f"otop{self.state['outsource_topup_seq']}"
            req = {
                "id": request_id,
                "username": username,
                "base_amount": base,
                "unique_amount": unique_amount,
                "status": "pending",
                "created_at": now,
                "expires_at": now + ttl_seconds,
                "txid": "",
                "credited_at": 0,
            }
            self.state.setdefault("outsource_topup_requests", {})[request_id] = req
            await self._save_unlocked()
            return req

    def list_outsource_topup_requests(self) -> dict:
        return dict(self.state.get("outsource_topup_requests") or {})

    def get_outsource_topup_request(self, request_id) -> Optional[dict]:
        if not request_id:
            return None
        return (self.state.get("outsource_topup_requests") or {}).get(str(request_id))

    def find_pending_topup_by_amount(self, amount: float, tolerance: float = 0.00005):
        """Ищет pending top-up с точным совпадением unique_amount (учётом expires_at)."""
        try:
            amt = float(amount)
        except Exception:
            return None
        now = time.time()
        reqs = self.state.get("outsource_topup_requests") or {}
        for req in reqs.values():
            if req.get("status") != "pending":
                continue
            if req.get("expires_at", 0) < now:
                continue
            try:
                if abs(float(req.get("unique_amount") or 0) - amt) <= tolerance:
                    return req
            except Exception:
                continue
        return None

    async def credit_outsource_topup(
        self, request_id: str, txid: str = "", credited_block_ts: int = 0,
        manual_by: str = "",
    ) -> Optional[dict]:
        """Зачисляет pending top-up: помечает credited + увеличивает баланс юзера.
        Возвращает обновлённый request или None если ошибка.
        """
        async with _lock:
            reqs = self.state.get("outsource_topup_requests") or {}
            req = reqs.get(str(request_id))
            if not req:
                return None
            if req.get("status") != "pending":
                return None  # уже обработан
            username = (req.get("username") or "").lower()
            mgr = (self.state.get("outsource_managers") or {}).get(username)
            if not mgr:
                # Юзера нет? Странно — но не падаем, создадим минимальную запись
                mgrs = self.state.setdefault("outsource_managers", {})
                mgrs[username] = {
                    "tg_user_id": 0, "first_seen_ts": time.time(),
                    "wallet_balance_usdt": 0.0, "paid_total_usdt": 0.0,
                    "stats": {"drops_total": 0, "lks_total": 0, "lks_done": 0},
                }
                mgr = mgrs[username]
            base = float(req.get("base_amount") or 0)
            mgr["wallet_balance_usdt"] = float(mgr.get("wallet_balance_usdt") or 0) + base
            mgr["last_active_ts"] = time.time()
            req["status"] = "credited"
            req["txid"] = txid or req.get("txid") or ""
            req["credited_at"] = time.time()
            req["credited_block_ts"] = int(credited_block_ts or 0)
            if manual_by:
                req["credited_manual_by"] = manual_by
            await self._save_unlocked()
            return req

    async def reject_outsource_topup(self, request_id: str, manual_by: str = "") -> bool:
        async with _lock:
            req = (self.state.get("outsource_topup_requests") or {}).get(str(request_id))
            if not req:
                return False
            if req.get("status") != "pending":
                return False
            req["status"] = "rejected"
            req["rejected_at"] = time.time()
            if manual_by:
                req["rejected_by"] = manual_by
            await self._save_unlocked()
            return True

    # ════════════════════════════════════════════════════════════
    # PENDING CHAT FIXES — авто-починка прав CRM-бота в чатах
    # ════════════════════════════════════════════════════════════
    async def add_chat_fix_request(self, chat_id, reason: str = "") -> None:
        """CRM-бот вызывает это когда падает с CHAT_RESTRICTED / kicked.
        Userbot-воркер потом подберёт и попробует исправить.
        Идемпотентно — если запрос уже в очереди, обновляем reason."""
        if not chat_id:
            return
        async with _lock:
            q = self.state.setdefault("pending_chat_fixes", {})
            key = str(chat_id)
            existing = q.get(key) or {}
            q[key] = {
                "chat_id": int(chat_id) if str(chat_id).lstrip("-").isdigit() else chat_id,
                "reason": reason or existing.get("reason") or "",
                "added_at": existing.get("added_at") or time.time(),
                "attempts": int(existing.get("attempts") or 0),
                "last_error": existing.get("last_error") or "",
                "last_attempt_at": existing.get("last_attempt_at") or 0,
            }
            await self._save_unlocked()

    def list_chat_fix_requests(self) -> dict:
        return dict(self.state.get("pending_chat_fixes") or {})

    async def remove_chat_fix_request(self, chat_id) -> bool:
        async with _lock:
            q = self.state.get("pending_chat_fixes") or {}
            key = str(chat_id)
            if key in q:
                del q[key]
                await self._save_unlocked()
                return True
            return False

    async def bump_chat_fix_attempt(self, chat_id, last_error: str = "") -> None:
        async with _lock:
            q = self.state.get("pending_chat_fixes") or {}
            e = q.get(str(chat_id))
            if not e:
                return
            e["attempts"] = int(e.get("attempts") or 0) + 1
            e["last_attempt_at"] = time.time()
            if last_error:
                e["last_error"] = last_error[:200]
            await self._save_unlocked()

    # ════════════════════════════════════════════════════════════
    # ОТКУПЫ — RUB → USDT TRC20 обмен через ручных Откупщиков
    # ════════════════════════════════════════════════════════════
    def get_outkup_settings(self) -> dict:
        s = self.state.get("outkup_settings") or {}
        return {
            "rate_rub_per_usdt": float(s.get("rate_rub_per_usdt") or 100.0),
            "payments_chat_id": int(s.get("payments_chat_id") or 0),
            "outkup_team_chat_id": int(s.get("outkup_team_chat_id") or 0),
            "min_amount_rub": int(s.get("min_amount_rub") or 5000),
            "max_amount_rub": int(s.get("max_amount_rub") or 5000000),
            "enabled": bool(s.get("enabled", True)),
        }

    async def update_outkup_settings(self, **fields) -> dict:
        async with _lock:
            s = self.state.setdefault("outkup_settings", {})
            for k, v in fields.items():
                if k == "rate_rub_per_usdt":
                    s[k] = float(v or 0)
                elif k in ("payments_chat_id", "outkup_team_chat_id",
                           "min_amount_rub", "max_amount_rub"):
                    s[k] = int(v or 0)
                elif k == "enabled":
                    s[k] = bool(v)
                else:
                    s[k] = v
            await self._save_unlocked()
            return self.get_outkup_settings()

    def list_outkup_orders(self) -> dict:
        return dict(self.state.get("outkup_orders") or {})

    def get_outkup_order(self, order_id) -> Optional[dict]:
        if not order_id:
            return None
        return (self.state.get("outkup_orders") or {}).get(str(order_id))

    async def create_outkup_order(
        self, client_chat_id: int, client_user_id: int,
        client_username: str, client_msg_id: int,
        amount_rub: float, method: str,
    ) -> Optional[dict]:
        """Создаёт заявку в статусе pending_confirm. Bot уже посчитал USDT."""
        settings = self.get_outkup_settings()
        rate = settings["rate_rub_per_usdt"]
        if rate <= 0:
            return None
        try:
            amount = float(amount_rub or 0)
        except Exception:
            return None
        if amount < settings["min_amount_rub"] or amount > settings["max_amount_rub"]:
            return None
        usdt = round(amount / rate, 2)
        async with _lock:
            self.state["outkup_orders_seq"] = int(self.state.get("outkup_orders_seq") or 0) + 1
            oid = f"outk{self.state['outkup_orders_seq']}"
            now = time.time()
            order = {
                "id": oid,
                "client_chat_id": int(client_chat_id or 0),
                "client_user_id": int(client_user_id or 0),
                "client_username": (client_username or "").lstrip("@").lower(),
                "client_msg_id": int(client_msg_id or 0),
                "amount_rub": amount,
                "method": (method or "sbp").lower(),
                "calculated_usdt": usdt,
                "rate": rate,
                "status": "pending_confirm",
                "created_at": now,
                "confirmed_at": 0,
                "assigned_to": "",
                "assigned_at": 0,
                "paid_at": 0,
                "completed_at": 0,
                "cancelled_at": 0,
                "txid": "",
                "notes": "",
            }
            (self.state.setdefault("outkup_orders", {}))[oid] = order
            await self._save_unlocked()
            return order

    async def update_outkup_order(self, order_id, **fields) -> Optional[dict]:
        async with _lock:
            orders = self.state.get("outkup_orders") or {}
            o = orders.get(str(order_id))
            if not o:
                return None
            for k, v in fields.items():
                o[k] = v
            await self._save_unlocked()
            return o

    async def confirm_outkup_order(self, order_id) -> Optional[dict]:
        """Клиент подтвердил расчёт → меняем статус и форвардим в чат Откупщиков."""
        async with _lock:
            o = (self.state.get("outkup_orders") or {}).get(str(order_id))
            if not o or o.get("status") != "pending_confirm":
                return None
            o["status"] = "awaiting_payment"
            o["confirmed_at"] = time.time()
            await self._save_unlocked()
            return o

    async def take_outkup_order(self, order_id, username: str) -> Optional[dict]:
        """Откупщик берёт заявку в работу."""
        username = (username or "").lstrip("@").lower()
        async with _lock:
            o = (self.state.get("outkup_orders") or {}).get(str(order_id))
            if not o or o.get("status") not in ("awaiting_payment", "pending_confirm"):
                return None
            o["assigned_to"] = username
            o["assigned_at"] = time.time()
            if o["status"] == "pending_confirm":
                o["status"] = "awaiting_payment"
            await self._save_unlocked()
            return o

    async def mark_outkup_paid(self, order_id, by: str = "") -> Optional[dict]:
        """Откупщик подтвердил что клиент заплатил (получил рубли)."""
        async with _lock:
            o = (self.state.get("outkup_orders") or {}).get(str(order_id))
            if not o or o.get("status") not in ("awaiting_payment",):
                return None
            o["status"] = "paid"
            o["paid_at"] = time.time()
            if by:
                o["paid_by"] = (by or "").lstrip("@").lower()
            await self._save_unlocked()
            return o

    async def complete_outkup_order(
        self, order_id, txid: str = "", by: str = "",
    ) -> Optional[dict]:
        """Откупщик отправил USDT → завершено."""
        async with _lock:
            o = (self.state.get("outkup_orders") or {}).get(str(order_id))
            if not o or o.get("status") not in ("paid", "awaiting_payment"):
                return None
            o["status"] = "completed"
            o["completed_at"] = time.time()
            o["txid"] = txid or o.get("txid") or ""
            if by:
                o["completed_by"] = (by or "").lstrip("@").lower()
            await self._save_unlocked()
            return o

    async def cancel_outkup_order(
        self, order_id, reason: str = "", by: str = "",
    ) -> Optional[dict]:
        async with _lock:
            o = (self.state.get("outkup_orders") or {}).get(str(order_id))
            if not o:
                return None
            if o.get("status") in ("completed", "cancelled"):
                return None
            o["status"] = "cancelled"
            o["cancelled_at"] = time.time()
            o["cancel_reason"] = reason or ""
            if by:
                o["cancelled_by"] = (by or "").lstrip("@").lower()
            await self._save_unlocked()
            return o

    # ════════════════════════════════════════════════════════════
    # OUTSOURCE BOT TEXTS — редактируемые тексты бота из JARVIS
    # ════════════════════════════════════════════════════════════
    def get_outsource_text(self, key: str, default: str = "") -> str:
        """Возвращает override или дефолт из outsource_bot_texts.DEFAULT_TEXTS."""
        overrides = self.state.get("outsource_bot_texts") or {}
        if key in overrides and overrides[key] != "":
            return overrides[key]
        if default:
            return default
        try:
            from outsource_bot_texts import DEFAULT_TEXTS
            return DEFAULT_TEXTS.get(key, "")
        except Exception:
            return ""

    def list_outsource_texts(self) -> dict:
        """Возвращает {key: {default, current, is_overridden}} для UI."""
        try:
            from outsource_bot_texts import DEFAULT_TEXTS
        except Exception:
            DEFAULT_TEXTS = {}
        overrides = self.state.get("outsource_bot_texts") or {}
        result = {}
        all_keys = set(DEFAULT_TEXTS.keys()) | set(overrides.keys())
        for k in all_keys:
            default = DEFAULT_TEXTS.get(k, "")
            override = overrides.get(k, "")
            current = override if override != "" else default
            result[k] = {
                "default": default,
                "current": current,
                "is_overridden": bool(override and override != default),
            }
        return result

    async def set_outsource_text(self, key: str, value: str) -> bool:
        if not key:
            return False
        async with _lock:
            texts = self.state.setdefault("outsource_bot_texts", {})
            # Пустая строка = удалить override → вернуть к дефолту
            if not value:
                texts.pop(key, None)
            else:
                texts[key] = value
            await self._save_unlocked()
            return True

    async def reset_outsource_text(self, key: str) -> bool:
        async with _lock:
            texts = self.state.setdefault("outsource_bot_texts", {})
            if key in texts:
                texts.pop(key)
                await self._save_unlocked()
            return True

    async def expire_old_outsource_topups(self) -> int:
        """Помечает все pending с истёкшим expires_at как 'expired'. Возвращает кол-во."""
        async with _lock:
            now = time.time()
            reqs = self.state.get("outsource_topup_requests") or {}
            n = 0
            for req in reqs.values():
                if req.get("status") == "pending" and req.get("expires_at", 0) < now:
                    req["status"] = "expired"
                    n += 1
            if n:
                await self._save_unlocked()
            return n

    async def buy_outsource_bundle(
        self, bundle_id, username: str,
    ) -> Optional[dict]:
        """Атомарно покупает связку: списывает баланс, помечает bundle и все ЛК.
        Возвращает обновлённый bundle dict или None если ошибка.
        Не проверяет баланс — это делает caller заранее (для UX alert).
        """
        username = (username or "").lstrip("@").lower()
        if not bundle_id or not username:
            return None
        async with _lock:
            bundles = self.state.get("outsource_bundles") or {}
            bundle = bundles.get(str(bundle_id))
            if not bundle:
                return None
            if not bundle.get("in_pool"):
                return None  # уже куплен
            price = float(bundle.get("list_price_usdt") or 0)
            mgr = (self.state.get("outsource_managers") or {}).get(username)
            if not mgr:
                return None
            balance = float(mgr.get("wallet_balance_usdt") or 0)
            if balance < price:
                return None
            # Списываем + помечаем
            mgr["wallet_balance_usdt"] = balance - price
            mgr["paid_total_usdt"] = float(mgr.get("paid_total_usdt") or 0) + price
            mgr["last_active_ts"] = time.time()
            bundle["in_pool"] = False
            bundle["manager_username"] = username
            bundle["bought_at"] = time.time()
            lks = self.state.get("outsource_drop_lks") or {}
            now = time.time()
            for lkid in bundle.get("lk_ids", []):
                lk = lks.get(str(lkid))
                if lk:
                    lk["manager_username"] = username
                    lk["in_pool"] = False
                    lk["bought_at"] = now
                    lk["bought_by"] = username
                    lk["bought_as_bundle"] = str(bundle_id)
            await self._save_unlocked()
            return bundle

    # =====================================================================
    # OWNER PANEL — роли и разрешения
    # =====================================================================
    # Список всех 14 главных view'ов в JARVIS
    _ALL_VIEWS = [
        "office", "chat", "lk", "fin", "crm", "payouts", "discord",
        "support", "system", "accounting", "operational", "outkup",
        "settings", "owner",
    ]
    # Подвкладки внутри views — для гранулярного доступа.
    # Если роли не выдан конкретный subview, эта вкладка не показывается.
    # Если для view нет записи в `subviews` perms — доступны ВСЕ подвкладки (default).
    _ALL_SUBVIEWS = {
        "system": [
            "access", "password",
            "credit_access", "credit_password",
            "outsource_access", "outsource_password",
            "installed",
        ],
        "settings": [
            "profile", "workers", "chats", "ai", "pricing",
            "invite", "payment", "appearance", "system",
        ],
        "lk": ["all", "to_top_up", "to_pay", "to_release"],
        "support": ["managers", "system", "accounting", "all"],
    }
    # Полный список edit-actions (~70). Группируется по разделу для UI.
    _ALL_EDIT_ACTIONS = [
        # === ЛК / Operations ===
        "lk_status_change", "lk_update", "lk_delete",
        "lk_fill", "lk_sms_action",
        "move_lk_to_credit", "move_lk_to_outsource", "move_lk_to_supplier",
        # === System / KUC / Bundles ===
        "request_kuc", "decide_kuc",
        "bundle_create", "bundle_dissolve",
        # === Outsource bot management ===
        "topup_credit", "topup_reject",
        "bot_text_set", "bot_text_reset",
        # === Payouts ===
        "payout_usdt_paid", "payout_released",
        "payout_deal_funded", "payout_set_deal_id",
        # === Accounting ===
        "accounting_entry_add", "accounting_entry_delete",
        "accounting_payout_note",
        # === Support ===
        "support_take", "support_reply",
        "support_transfer", "support_close",
        "tg_session_connect", "tg_session_verify", "tg_session_disconnect",
        # === CRM Партнёры ===
        "crm_ban", "crm_unban", "crm_warn",
        # === Discord / Hub ===
        "discord_channel_create", "discord_channel_delete",
        "discord_message_send", "discord_message_delete",
        "discord_reaction_add", "discord_reaction_remove",
        "discord_pin", "discord_unpin",
        "call_create", "call_end", "call_join",
        # === Operational ===
        "exchange_request",
        # === Settings ===
        "settings_tg_chats", "settings_ai", "settings_invite",
        "settings_pricing_set", "settings_pricing_delete",
        "settings_payment_wallet", "admin_worker_role",
        # === Owner ===
        "owner_role_create_update", "owner_role_delete", "owner_user_role",
        # === Shared / контроль ===
        "ai_toggle", "sync_lk_request",
        # === Outreach ===
        "outreach_bot_auth_start", "outreach_bot_auth_confirm", "outreach_bot_delete",
        "outreach_campaign_create", "outreach_campaign_update", "outreach_campaign_delete",
        "outreach_campaign_start", "outreach_campaign_pause", "outreach_campaign_stop",
        "outreach_response_handle",
        # === LEO ===
        "leo_ask", "leo_voice_command", "leo_realtime_session",
        "leo_note_create", "leo_note_delete", "leo_note_move",
        "leo_notes_archive",
        # === CRM-бот (через Telegram, не API) ===
        "credit_capture_chat",
        # === ОТКУПЫ (RUB → USDT обмен) ===
        "outkup_take", "outkup_mark_paid", "outkup_complete",
        "outkup_cancel", "outkup_settings_update",
    ]
    # Дефолтные роли + их доступ. Каждая роль может иметь:
    # - views[]              — список доступных топ-views (или ["*"] = все)
    # - view_readonly[]      — views в режиме «только просмотр» (все mutations блокируются)
    # - subviews{view: []}   — фильтр доступных подвкладок (по умолчанию все)
    # - subview_readonly{view: {sub: True}} — конкретные subviews только просмотр
    # - edit_actions[]       — разрешённые мутации (или ["*"] = все)
    _DEFAULT_ROLE_PERMS = {
        "owner": {
            "label": "👑 Owner",
            "views": ["*"], "edit_actions": ["*"],
        },
        "manager": {
            "label": "👤 Manager",
            "views": ["office", "chat", "lk", "crm", "support", "system", "operational"],
            "edit_actions": [
                "request_kuc", "decide_kuc", "lk_sms_action", "lk_fill",
                "move_lk_to_credit", "move_lk_to_outsource", "move_lk_to_supplier",
                "lk_status_change",
                "support_take", "support_reply", "support_transfer", "support_close",
                "credit_capture_chat",
            ],
        },
        "system_dept": {
            "label": "⚙️ System",
            "views": ["office", "system", "operational"],
            "edit_actions": [
                "request_kuc", "decide_kuc", "lk_fill", "lk_sms_action",
            ],
        },
        "accounting": {
            "label": "💰 Accounting",
            "views": ["office", "fin", "payouts", "accounting"],
            "edit_actions": [
                "exchange_request",
                "payout_usdt_paid", "payout_released",
                "payout_deal_funded", "payout_set_deal_id",
                "accounting_entry_add", "accounting_entry_delete",
                "accounting_payout_note",
            ],
        },
        "operationist": {
            "label": "🛠 Operationist",
            "views": ["office", "operational", "lk"],
            "edit_actions": [
                "exchange_request", "lk_status_change",
            ],
        },
        # === Роль: ОТКУПЫ (RUB → USDT обмен) ===
        "outkup_specialist": {
            "label": "💱 Откупщик",
            "views": ["office", "outkup"],
            "view_readonly": ["office"],
            "edit_actions": [
                "outkup_take", "outkup_mark_paid", "outkup_complete",
                "outkup_cancel",
            ],
        },
        # === Роль 1: Менеджер чатов Доступы (от SIMBA) ===
        "chat_access_manager": {
            "label": "👤 Менеджер чатов Доступы",
            "views": [
                "office",      # просмотр + LEO голос
                "crm",         # просмотр
                "discord",     # full
                "support",     # full
                "system",      # только Доступы вкладки
                "settings",    # ограниченные подразделы
            ],
            "view_readonly": ["office", "crm"],  # только просмотр
            "subviews": {
                "system": ["access", "credit_access", "outsource_access"],  # без password / installed
                "settings": ["profile", "workers", "pricing", "appearance"],
            },
            "subview_readonly": {
                "settings": {"workers": True},  # работники — только просмотр
            },
            "edit_actions": [
                # 🟣 ХАБ — полный доступ
                "discord_channel_create", "discord_channel_delete",
                "discord_message_send", "discord_message_delete",
                "discord_reaction_add", "discord_reaction_remove",
                "discord_pin", "discord_unpin",
                "call_create", "call_end", "call_join",
                # 🛟 Поддержка — полный
                "support_take", "support_reply", "support_transfer", "support_close",
                "tg_session_connect", "tg_session_verify", "tg_session_disconnect",
                # ⚙ SYSTEM Доступы — full mutation для вкладок Доступы
                "request_kuc", "decide_kuc",
                "lk_sms_action", "lk_fill",
                "move_lk_to_credit", "move_lk_to_outsource", "move_lk_to_supplier",
                "bundle_create", "bundle_dissolve",
                # ⚙️ Настройки — Прайс ЛК полный
                "settings_pricing_set", "settings_pricing_delete",
                # 🧠 LEO голос (whitelist вопросов на стороне prompt-фильтра)
                "leo_ask", "leo_voice_command", "leo_realtime_session",
                "leo_note_create",
            ],
        },
    }

    def _ensure_default_role_permissions(self):
        """Заполняет role_permissions дефолтами если пусто."""
        rp = self.state.setdefault("role_permissions", {})
        for role_name, perms in self._DEFAULT_ROLE_PERMS.items():
            if role_name not in rp:
                rp[role_name] = dict(perms)

    def list_role_permissions(self) -> dict:
        self._ensure_default_role_permissions()
        return dict(self.state.get("role_permissions") or {})

    def get_role_permission(self, role: str) -> dict:
        self._ensure_default_role_permissions()
        return (self.state.get("role_permissions") or {}).get(role) or {}

    async def set_role_permission(
        self, role: str, label: str = "",
        views: Optional[list] = None,
        edit_actions: Optional[list] = None,
        view_readonly: Optional[list] = None,
        subviews: Optional[dict] = None,
        subview_readonly: Optional[dict] = None,
    ) -> dict:
        """Создаёт/обновляет роль. Поддерживает subviews + readonly."""
        async with _lock:
            rp = self.state.setdefault("role_permissions", {})
            existing = rp.get(role) or {}
            rp[role] = {
                "label": label or existing.get("label") or role,
                "views": list(views) if views is not None else (existing.get("views") or []),
                "edit_actions": list(edit_actions) if edit_actions is not None else (existing.get("edit_actions") or []),
                "view_readonly": list(view_readonly) if view_readonly is not None else (existing.get("view_readonly") or []),
                "subviews": dict(subviews) if subviews is not None else (existing.get("subviews") or {}),
                "subview_readonly": dict(subview_readonly) if subview_readonly is not None else (existing.get("subview_readonly") or {}),
            }
            if role not in self._DEFAULT_ROLE_PERMS:
                custom = self.state.setdefault("custom_roles", [])
                if role not in custom:
                    custom.append(role)
            await self._save_unlocked()
            return rp[role]

    async def delete_role_permission(self, role: str) -> bool:
        """Удалить кастомную роль. Дефолтные нельзя удалить."""
        if role in self._DEFAULT_ROLE_PERMS:
            return False
        async with _lock:
            rp = self.state.get("role_permissions") or {}
            if role in rp:
                del rp[role]
            custom = self.state.get("custom_roles") or []
            if role in custom:
                custom.remove(role)
            await self._save_unlocked()
            return True

    def role_can_view(self, role: str, view_id: str) -> bool:
        perms = self.get_role_permission(role)
        views = perms.get("views") or []
        return ("*" in views) or (view_id in views)

    def role_can_edit(self, role: str, action: str) -> bool:
        perms = self.get_role_permission(role)
        acts = perms.get("edit_actions") or []
        return ("*" in acts) or (action in acts)

    def role_can_subview(self, role: str, view_id: str, subview_id: str) -> bool:
        """Можно ли роли видеть конкретную подвкладку.

        Если в perms.subviews нет записи для view_id — доступны ВСЕ подвкладки (default open).
        Если есть запись со списком — доступны только перечисленные.
        Если "*" в списке — все.
        """
        perms = self.get_role_permission(role)
        # Owner-wildcard: если views=['*'] — всё доступно
        if "*" in (perms.get("views") or []):
            return True
        subs = perms.get("subviews") or {}
        if view_id not in subs:
            return True  # нет ограничения = всё открыто
        allowed = subs[view_id] or []
        return ("*" in allowed) or (subview_id in allowed)

    def role_view_readonly(self, role: str, view_id: str) -> bool:
        """View в режиме только-просмотр (мутации блокируются)."""
        perms = self.get_role_permission(role)
        return view_id in (perms.get("view_readonly") or [])

    def role_subview_readonly(self, role: str, view_id: str, subview_id: str) -> bool:
        perms = self.get_role_permission(role)
        sro = perms.get("subview_readonly") or {}
        view_map = sro.get(view_id) or {}
        return bool(view_map.get(subview_id))

    def effective_permissions(self, role: str) -> dict:
        """Возвращает полный снимок прав роли для frontend.

        Используется в /api/me — UI скрывает недоступные кнопки/вкладки.
        """
        perms = self.get_role_permission(role)
        return {
            "role": role,
            "label": perms.get("label") or role,
            "views": list(perms.get("views") or []),
            "view_readonly": list(perms.get("view_readonly") or []),
            "subviews": dict(perms.get("subviews") or {}),
            "subview_readonly": dict(perms.get("subview_readonly") or {}),
            "edit_actions": list(perms.get("edit_actions") or []),
        }

    def list_all_known_views(self) -> list:
        return list(self._ALL_VIEWS)

    def list_all_known_actions(self) -> list:
        return list(self._ALL_EDIT_ACTIONS)

    def list_all_known_subviews(self) -> dict:
        return dict(self._ALL_SUBVIEWS)

    # =====================================================================
    # GUEST CALLS — звонки по одноразовой ссылке (Яндекс.Телемост-стиль)
    # =====================================================================
    def list_guest_calls(self, only_active: bool = True) -> dict:
        gc = self.state.get("guest_calls") or {}
        if not only_active:
            return dict(gc)
        return {k: v for k, v in gc.items() if not v.get("ended_at")}

    def get_guest_call(self, room_id: str) -> Optional[dict]:
        return (self.state.get("guest_calls") or {}).get(str(room_id))

    async def create_guest_call(self, created_by: str, name: str = "",
                                password: str = "", max_participants: int = 10) -> dict:
        """Создаёт комнату для гостевого звонка. Возвращает {room_id, password, ...}."""
        import uuid as _uuid
        import secrets as _secrets
        async with _lock:
            room_id = _uuid.uuid4().hex[:16]
            pwd = password or _secrets.token_urlsafe(6)  # короткий читаемый пароль
            gcs = self.state.setdefault("guest_calls", {})
            entry = {
                "room_id": room_id,
                "password": pwd,
                "name": name or "Звонок",
                "created_by": (created_by or "").lstrip("@").lower(),
                "created_at": time.time(),
                "ended_at": 0,
                "max_participants": int(max_participants or 10),
                "active_participants": [],
            }
            gcs[room_id] = entry
            await self._save_unlocked()
            return entry

    async def end_guest_call(self, room_id: str, ended_by: str = "") -> bool:
        async with _lock:
            gc = (self.state.get("guest_calls") or {}).get(str(room_id))
            if not gc:
                return False
            gc["ended_at"] = time.time()
            gc["ended_by"] = ended_by or ""
            await self._save_unlocked()
            return True

    async def add_guest_participant(self, room_id: str, participant_id: str, name: str = "") -> bool:
        async with _lock:
            gc = (self.state.get("guest_calls") or {}).get(str(room_id))
            if not gc:
                return False
            parts = gc.setdefault("active_participants", [])
            if not any(p.get("participant_id") == participant_id for p in parts):
                parts.append({"participant_id": participant_id, "name": name or "Гость",
                              "joined_at": time.time()})
                await self._save_unlocked()
            return True

    async def remove_guest_participant(self, room_id: str, participant_id: str) -> bool:
        async with _lock:
            gc = (self.state.get("guest_calls") or {}).get(str(room_id))
            if not gc:
                return False
            parts = gc.get("active_participants") or []
            gc["active_participants"] = [p for p in parts if p.get("participant_id") != participant_id]
            await self._save_unlocked()
            return True

    def get_work_chat_for_droplk(self, droplk_id: str) -> Optional[int]:
        """Возвращает work_chat_id (для отправки сообщений клиенту) для любого ЛК.
        Работает и для crm_drop_lks, и для credit_drop_lks."""
        lk = self.get_drop_lk_any(droplk_id)
        if not lk:
            return None
        drop_id = lk.get("drop_id") or lk.get("credit_drop_id")
        if not drop_id:
            return None
        drop = self.get_drop_any(drop_id)
        if not drop:
            return None
        cid = drop.get("work_chat_id") or drop.get("chat_id")
        try:
            return int(cid) if cid else None
        except (ValueError, TypeError):
            return None

    # =====================================================================
    # CREDIT (Кредитование) — параллельная инфраструктура, зеркало crm_*
    # =====================================================================
    # --- Менеджеры (юристы) ---
    def list_credit_managers(self) -> dict:
        return self.state.get("credit_managers") or {}

    def get_credit_manager(self, username: str) -> Optional[dict]:
        if not username:
            return None
        u = username.lstrip("@").lower()
        return (self.state.get("credit_managers") or {}).get(u)

    async def register_credit_manager(
        self, username: str, tg_user_id: Optional[int] = None,
    ) -> dict:
        """Регистрирует/обновляет менеджера. Возвращает запись."""
        if not username:
            return {}
        u = username.lstrip("@").lower()
        async with _lock:
            mgrs = self.state.setdefault("credit_managers", {})
            now = time.time()
            if u not in mgrs:
                mgrs[u] = {
                    "username": u,
                    "tg_user_id": tg_user_id or 0,
                    "first_seen_ts": now,
                    "last_active_ts": now,
                    "stats": {
                        "drops_total": 0, "drops_done": 0,
                        "lks_total": 0, "lks_done": 0,
                    },
                }
            else:
                mgrs[u]["last_active_ts"] = now
                if tg_user_id and not mgrs[u].get("tg_user_id"):
                    mgrs[u]["tg_user_id"] = tg_user_id
            await self._save_unlocked()
            return mgrs[u]

    async def _bump_credit_manager_stat_unlocked(self, username: str, key: str, delta: int = 1):
        """Логика без захвата _lock — для вызова из-под уже захваченного _lock.
        Не делает _save_unlocked (caller сам сохранит когда вся транзакция готова)."""
        if not username:
            return
        u = username.lstrip("@").lower()
        mgrs = self.state.setdefault("credit_managers", {})
        if u not in mgrs:
            mgrs[u] = {
                "username": u, "tg_user_id": 0,
                "first_seen_ts": time.time(), "last_active_ts": time.time(),
                "stats": {"drops_total": 0, "drops_done": 0, "lks_total": 0, "lks_done": 0},
            }
        stats = mgrs[u].setdefault("stats", {})
        stats[key] = (stats.get(key) or 0) + delta

    async def bump_credit_manager_stat(self, username: str, key: str, delta: int = 1):
        """Публичная версия: берёт _lock и сохраняет state. Для внешних вызовов."""
        if not username:
            return
        async with _lock:
            await self._bump_credit_manager_stat_unlocked(username, key, delta)
            await self._save_unlocked()

    # --- Чаты кредитования ---
    def list_credit_chats(self) -> dict:
        return self.state.get("credit_chats") or {}

    def get_credit_chat(self, chat_id) -> Optional[dict]:
        c = _norm_chat_id(chat_id)
        return (self.state.get("credit_chats") or {}).get(c)

    async def register_credit_chat(
        self, chat_id, manager_username: str,
        is_access: bool = True, is_password: bool = False,
        registered_by_owner_id: Optional[int] = None,
    ) -> dict:
        async with _lock:
            chats = self.state.setdefault("credit_chats", {})
            c = _norm_chat_id(chat_id)
            entry = chats.get(c) or {}
            entry.update({
                "manager_username": (manager_username or "").lstrip("@").lower(),
                "is_access": bool(is_access),
                "is_password": bool(is_password),
                "registered_at": entry.get("registered_at") or time.time(),
                "registered_by_owner_id": registered_by_owner_id or entry.get("registered_by_owner_id"),
            })
            chats[c] = entry
            await self._save_unlocked()
            return entry

    async def unregister_credit_chat(self, chat_id) -> bool:
        async with _lock:
            chats = self.state.get("credit_chats") or {}
            c = _norm_chat_id(chat_id)
            if c in chats:
                del chats[c]
                await self._save_unlocked()
                return True
            return False

    def is_credit_chat(self, chat_id, kind: str = "any") -> bool:
        """kind: 'any' | 'access' | 'password'. True если chat относится к кредитованию.
        Источник истины (по приоритету):
          1) config.CREDIT_ACCESS_CHAT_ID / CREDIT_PASSWORD_CHAT_ID (хардкод/env)
          2) state credit_access_chat_id / credit_password_chat_id
          3) credit_chats (зарегистрированные доп. чаты)
        """
        cid_int = None
        try: cid_int = int(chat_id)
        except Exception: pass
        # 1) config
        access_main = 0
        password_main = 0
        try:
            import config as _cfg
            access_main = int(getattr(_cfg, "CREDIT_ACCESS_CHAT_ID", 0) or 0)
            password_main = int(getattr(_cfg, "CREDIT_PASSWORD_CHAT_ID", 0) or 0)
        except Exception:
            pass
        # 2) state (fallback)
        if not access_main:
            access_main = self.state.get("credit_access_chat_id") or 0
        if not password_main:
            password_main = self.state.get("credit_password_chat_id") or 0
        if kind in ("any", "access") and cid_int and cid_int == access_main:
            return True
        if kind in ("any", "password") and cid_int and cid_int == password_main:
            return True
        # 3) credit_chats (зарегистрированные через "Ассистент возьми...")
        chat_entry = self.get_credit_chat(chat_id)
        if not chat_entry:
            return False
        if kind == "any":
            return bool(chat_entry.get("is_access") or chat_entry.get("is_password"))
        if kind == "access":
            return bool(chat_entry.get("is_access"))
        if kind == "password":
            return bool(chat_entry.get("is_password"))
        return False

    # --- Анкеты кредитования ---
    def list_credit_drops(self, manager_username: Optional[str] = None) -> dict:
        drops = self.state.get("credit_drops") or {}
        if manager_username:
            u = manager_username.lstrip("@").lower()
            return {k: v for k, v in drops.items() if (v.get("manager_username") or "") == u}
        return drops

    def get_credit_drop(self, drop_id) -> Optional[dict]:
        return (self.state.get("credit_drops") or {}).get(str(drop_id))

    async def add_credit_drop(
        self, chat_id, manager_username: str, fio: str = "",
        about: str = "", scan_file_ids: Optional[list] = None,
    ) -> str:
        import logging as _lg
        _log = _lg.getLogger("storage")
        _log.info("[add_credit_drop] start chat=%s mgr=%s fio=%r", chat_id, manager_username, fio[:50])
        _log.info("[add_credit_drop] acquiring _lock")
        async with _lock:
            _log.info("[add_credit_drop] lock acquired")
            seq = (self.state.get("credit_drops_seq") or 0) + 1
            self.state["credit_drops_seq"] = seq
            drop_id = f"cdrp{seq:05d}"
            drops = self.state.setdefault("credit_drops", {})
            drops[drop_id] = {
                "drop_id": drop_id,
                "chat_id": _norm_chat_id(chat_id),
                "manager_username": (manager_username or "").lstrip("@").lower(),
                "fio": fio or "",
                "about": about or "",
                "scan_file_ids": list(scan_file_ids or []),
                "status": "draft",
                "created_at": time.time(),
                "lk_card_ids": [],
            }
            _log.info("[add_credit_drop] bumping manager stat (unlocked)")
            await self._bump_credit_manager_stat_unlocked(manager_username, "drops_total", 1)
            _log.info("[add_credit_drop] saving state.json (size_estimate=%d entries)", len(self.state.get("credit_drops") or {}))
            await self._save_unlocked()
            _log.info("[add_credit_drop] DONE drop_id=%s", drop_id)
            return drop_id

    async def update_credit_drop(self, drop_id: str, **fields) -> bool:
        async with _lock:
            drop = (self.state.get("credit_drops") or {}).get(str(drop_id))
            if not drop:
                return False
            for k, v in fields.items():
                drop[k] = v
            drop["updated_at"] = time.time()
            await self._save_unlocked()
            return True

    # --- ЛК банков под анкетами кредитования ---
    def list_credit_drop_lks(self, credit_drop_id: Optional[str] = None) -> dict:
        lks = self.state.get("credit_drop_lks") or {}
        if credit_drop_id:
            return {k: v for k, v in lks.items() if v.get("credit_drop_id") == credit_drop_id}
        return lks

    def get_credit_drop_lk(self, droplk_id) -> Optional[dict]:
        return (self.state.get("credit_drop_lks") or {}).get(str(droplk_id))

    async def add_credit_drop_lk(
        self, credit_drop_id: str, manager_username: str,
        bank: str = "", value: str = "", deal: str = "",
    ) -> str:
        async with _lock:
            seq = (self.state.get("credit_drop_lks_seq") or 0) + 1
            self.state["credit_drop_lks_seq"] = seq
            droplk_id = f"clk{seq:05d}"
            lks = self.state.setdefault("credit_drop_lks", {})
            lks[droplk_id] = {
                "droplk_id": droplk_id,
                "credit_drop_id": credit_drop_id,
                "manager_username": (manager_username or "").lstrip("@").lower(),
                "bank": bank or "",
                "value": value or "",
                "deal": deal or "",
                "sms_history": [],
                "sms_stage": "",
                "new_login": "", "new_password": "",
                "new_mail": "", "new_number": "", "code_word": "",
                "ded_ip": "", "ded_login": "Administrator",
                "ded_pass": "", "ded_location": "",
                "msgid_pass": 0, "sms_tracker_msg_id": 0,
                "created_at": time.time(), "updated_at": time.time(),
            }
            drop = (self.state.get("credit_drops") or {}).get(credit_drop_id)
            if drop:
                drop.setdefault("lk_card_ids", []).append(droplk_id)
            await self._bump_credit_manager_stat_unlocked(manager_username, "lks_total", 1)
            await self._save_unlocked()
            return droplk_id

    async def update_credit_drop_lk(self, droplk_id, **fields) -> bool:
        async with _lock:
            lk = (self.state.get("credit_drop_lks") or {}).get(str(droplk_id))
            if not lk:
                return False
            for k, v in fields.items():
                lk[k] = v
            lk["updated_at"] = time.time()
            await self._save_unlocked()
            return True

    # --- FSM для credit-чатов ---
    def get_credit_fsm(self, tg_user_id):
        return (self.state.get("credit_fsm") or {}).get(str(tg_user_id)) or {}

    async def set_credit_fsm(self, tg_user_id, action=None, data=None, msg_id=None, chat_id=None):
        async with _lock:
            fsm = self.state.setdefault("credit_fsm", {})
            if action is None:
                fsm.pop(str(tg_user_id), None)
            else:
                fsm[str(tg_user_id)] = {
                    "action": action, "data": dict(data or {}),
                    "msg_id": msg_id, "chat_id": chat_id,
                    "updated_at": time.time(),
                    "expires_at": time.time() + 1800,
                }
            await self._save_unlocked()

    async def clear_credit_fsm(self, tg_user_id):
        await self.set_credit_fsm(tg_user_id, action=None)

    async def delete_credit_drop_lk(self, droplk_id) -> bool:
        async with _lock:
            lks = self.state.get("credit_drop_lks") or {}
            if str(droplk_id) in lks:
                lk = lks[str(droplk_id)]
                # Отвязываем от анкеты
                cdrop_id = lk.get("credit_drop_id")
                if cdrop_id:
                    drop = (self.state.get("credit_drops") or {}).get(cdrop_id)
                    if drop and droplk_id in (drop.get("lk_card_ids") or []):
                        try: drop["lk_card_ids"].remove(droplk_id)
                        except ValueError: pass
                del lks[str(droplk_id)]
                await self._save_unlocked()
                return True
            return False

    async def append_credit_sms(self, droplk_id, code, time_str=""):
        async with _lock:
            lk = (self.state.get("credit_drop_lks") or {}).get(str(droplk_id))
            if not lk:
                return False
            sms = lk.setdefault("sms_history", [])
            sms.append({"code": code, "time": time_str or time.strftime("%d.%m.%Y %H:%M")})
            await self._save_unlocked()
            return True

    # =====================================================================
    # AUTO-ROUTING (любой track по префиксу ID — для совместимости handler'ов)
    # =====================================================================
    # Префиксы:
    #   crm_drops:     'd' + 4 цифры       (например 'd0042')
    #   credit_drops:  'cdrp' + 5 цифр     (например 'cdrp00001')
    #   crm_drop_lks:  'lk' + цифры         (например 'lk0123')
    #   credit_drop_lks: 'clk' + 5 цифр    (например 'clk00001')
    # auto-detect по префиксу: cdrp/clk → credit, остальное → crm.

    def get_drop_any(self, drop_id) -> Optional[dict]:
        s = str(drop_id or "")
        if s.startswith("odrp"): return self.get_outsource_drop(s)
        if s.startswith("cdrp"): return self.get_credit_drop(s)
        return self.get_crm_drop(s)

    def get_drop_lk_any(self, droplk_id) -> Optional[dict]:
        s = str(droplk_id or "")
        if s.startswith("olk"): return self.get_outsource_drop_lk(s)
        if s.startswith("clk"): return self.get_credit_drop_lk(s)
        return self.get_crm_drop_lk(s)

    def list_drop_lks_any(self, drop_id=None) -> dict:
        if drop_id is None:
            out = {}
            out.update(self.state.get("crm_drop_lks") or {})
            out.update(self.state.get("credit_drop_lks") or {})
            out.update(self.state.get("outsource_drop_lks") or {})
            return out
        s = str(drop_id)
        if s.startswith("odrp"): return self.list_outsource_drop_lks(outsource_drop_id=s)
        if s.startswith("cdrp"): return self.list_credit_drop_lks(credit_drop_id=s)
        return self.list_crm_drop_lks(drop_id=s)

    async def update_drop_any(self, drop_id, **fields) -> bool:
        s = str(drop_id or "")
        if s.startswith("odrp"): return await self.update_outsource_drop(s, **fields)
        if s.startswith("cdrp"): return await self.update_credit_drop(s, **fields)
        return await self.update_crm_drop(s, **fields)

    async def update_drop_lk_any(self, droplk_id, **fields) -> bool:
        s = str(droplk_id or "")
        if s.startswith("olk"): return await self.update_outsource_drop_lk(s, **fields)
        if s.startswith("clk"): return await self.update_credit_drop_lk(s, **fields)
        return await self.update_crm_drop_lk(s, **fields)

    async def delete_drop_lk_any(self, droplk_id) -> bool:
        s = str(droplk_id or "")
        if s.startswith("olk"): return await self.delete_outsource_drop_lk(s)
        if s.startswith("clk"): return await self.delete_credit_drop_lk(s)
        return await self.delete_crm_drop_lk(s)

    async def append_drop_sms_any(self, droplk_id, code, time_str=""):
        s = str(droplk_id or "")
        if s.startswith("olk"): return await self.append_outsource_sms(s, code, time_str)
        if s.startswith("clk"): return await self.append_credit_sms(s, code, time_str)
        return await self.append_crm_sms(s, code, time_str)

    def get_drop_lks_for_drop_any(self, drop_id) -> dict:
        """Алиас list_drop_lks_any — для семантической ясности в handler'ах."""
        return self.list_drop_lks_any(drop_id)

    # === Write-routing (для create операций) ===
    async def add_drop_for_chat(
        self, chat_id, fio: str = "",
        owner_id: Optional[str] = None,
        manager_username: Optional[str] = None,
        work_chat_id: Optional[int] = None,
        about: str = "", scan_file_ids: Optional[list] = None,
    ):
        """Создаёт дроп. Routing по chat_id:
          - is_credit_chat(chat_id) → add_credit_drop (manager_username из credit_chats если не передан)
          - иначе → add_crm_drop (owner_id обязателен)
        Возвращает drop_id (формат `d000X` для crm или `cdrpXXXXX` для credit).
        """
        if self.is_credit_chat(chat_id):
            mgr = manager_username
            if not mgr:
                entry = self.get_credit_chat(chat_id) or {}
                mgr = entry.get("manager_username") or ""
            if not mgr:
                # Централизованная группа без явного менеджера — fallback "system"
                mgr = "system"
            return await self.add_credit_drop(
                chat_id=chat_id, manager_username=mgr, fio=fio,
                about=about, scan_file_ids=scan_file_ids,
            )
        # CRM (поставщики)
        if not owner_id:
            raise ValueError("owner_id required for CRM drop (non-credit chat)")
        return await self.add_crm_drop(
            owner_id=owner_id, fio=fio, work_chat_id=work_chat_id,
        )

    async def add_drop_lk_for_drop(
        self, drop_id: str, bank: str = "", value: str = "", deal: str = "",
        owner_id: Optional[str] = None,
    ):
        """Создаёт ЛК банка под анкетой. Routing по префиксу drop_id.
        Для CRM подтягивает owner_id из дропа (если не передан).
        Для credit подтягивает manager_username из credit_drop."""
        s = str(drop_id or "")
        if s.startswith("cdrp"):
            drop = self.get_credit_drop(s) or {}
            mgr = drop.get("manager_username") or ""
            return await self.add_credit_drop_lk(
                credit_drop_id=s, manager_username=mgr,
                bank=bank, value=value, deal=deal,
            )
        # CRM: add_crm_drop_lk(drop_id, owner_id, bank, value)
        if not owner_id:
            drop = self.get_crm_drop(s) or {}
            owner_id = drop.get("owner_id") or ""
        return await self.add_crm_drop_lk(
            drop_id=s, owner_id=owner_id, bank=bank, value=value,
        )

    # =====================================================================
    # OUTSOURCE (Аутсорс — маркетплейс ЛК для управляющих)
    # =====================================================================
    def list_outsource_managers(self) -> dict:
        return self.state.get("outsource_managers") or {}

    def get_outsource_manager(self, username: str) -> Optional[dict]:
        if not username: return None
        return (self.state.get("outsource_managers") or {}).get(username.lstrip("@").lower())

    async def register_outsource_manager(self, username: str, tg_user_id: Optional[int] = None) -> dict:
        if not username: return {}
        u = username.lstrip("@").lower()
        async with _lock:
            mgrs = self.state.setdefault("outsource_managers", {})
            now = time.time()
            if u not in mgrs:
                mgrs[u] = {
                    "username": u, "tg_user_id": tg_user_id or 0,
                    "first_seen_ts": now, "last_active_ts": now,
                    "wallet_balance_usdt": 0.0, "paid_total_usdt": 0.0,
                    "stats": {"drops_total": 0, "drops_done": 0, "lks_total": 0, "lks_done": 0},
                }
            else:
                mgrs[u]["last_active_ts"] = now
                if tg_user_id and not mgrs[u].get("tg_user_id"):
                    mgrs[u]["tg_user_id"] = tg_user_id
            await self._save_unlocked()
            return mgrs[u]

    async def _bump_outsource_manager_stat_unlocked(self, username: str, key: str, delta: int = 1):
        """Логика без _lock — для вызова из-под уже захваченного _lock."""
        if not username: return
        u = username.lstrip("@").lower()
        mgrs = self.state.setdefault("outsource_managers", {})
        if u not in mgrs:
            mgrs[u] = {"username": u, "tg_user_id": 0, "first_seen_ts": time.time(),
                       "last_active_ts": time.time(), "wallet_balance_usdt": 0.0,
                       "paid_total_usdt": 0.0, "stats": {"drops_total": 0, "drops_done": 0,
                                                          "lks_total": 0, "lks_done": 0}}
        stats = mgrs[u].setdefault("stats", {})
        stats[key] = (stats.get(key) or 0) + delta

    async def bump_outsource_manager_stat(self, username: str, key: str, delta: int = 1):
        """Публичная: берёт _lock + save. Для внешних вызовов."""
        if not username: return
        async with _lock:
            await self._bump_outsource_manager_stat_unlocked(username, key, delta)
            await self._save_unlocked()

    def list_outsource_chats(self) -> dict:
        return self.state.get("outsource_chats") or {}

    def get_outsource_chat(self, chat_id) -> Optional[dict]:
        return (self.state.get("outsource_chats") or {}).get(_norm_chat_id(chat_id))

    async def register_outsource_chat(self, chat_id, manager_username: str,
                                     is_access: bool = True, is_password: bool = False,
                                     registered_by_owner_id: Optional[int] = None) -> dict:
        async with _lock:
            chats = self.state.setdefault("outsource_chats", {})
            c = _norm_chat_id(chat_id)
            entry = chats.get(c) or {}
            entry.update({
                "manager_username": (manager_username or "").lstrip("@").lower(),
                "is_access": bool(is_access), "is_password": bool(is_password),
                "registered_at": entry.get("registered_at") or time.time(),
                "registered_by_owner_id": registered_by_owner_id or entry.get("registered_by_owner_id"),
            })
            chats[c] = entry
            await self._save_unlocked()
            return entry

    def is_outsource_chat(self, chat_id, kind: str = "any") -> bool:
        cid_int = None
        try: cid_int = int(chat_id)
        except Exception: pass
        # Из config (если будут хардкоженные ID)
        try:
            import config as _cfg
            access_main = int(getattr(_cfg, "OUTSOURCE_ACCESS_CHAT_ID", 0) or 0)
            password_main = int(getattr(_cfg, "OUTSOURCE_PASSWORD_CHAT_ID", 0) or 0)
        except Exception:
            access_main = password_main = 0
        if kind in ("any", "access") and cid_int and cid_int == access_main: return True
        if kind in ("any", "password") and cid_int and cid_int == password_main: return True
        entry = self.get_outsource_chat(chat_id)
        if not entry: return False
        if kind == "any": return bool(entry.get("is_access") or entry.get("is_password"))
        if kind == "access": return bool(entry.get("is_access"))
        if kind == "password": return bool(entry.get("is_password"))
        return False

    def list_outsource_drops(self, manager_username: Optional[str] = None) -> dict:
        drops = self.state.get("outsource_drops") or {}
        if manager_username:
            u = manager_username.lstrip("@").lower()
            return {k: v for k, v in drops.items() if (v.get("manager_username") or "") == u}
        return drops

    def get_outsource_drop(self, drop_id) -> Optional[dict]:
        return (self.state.get("outsource_drops") or {}).get(str(drop_id))

    async def add_outsource_drop(self, chat_id, manager_username: str, fio: str = "",
                                 about: str = "", scan_file_ids: Optional[list] = None) -> str:
        async with _lock:
            seq = (self.state.get("outsource_drops_seq") or 0) + 1
            self.state["outsource_drops_seq"] = seq
            drop_id = f"odrp{seq:05d}"
            self.state.setdefault("outsource_drops", {})[drop_id] = {
                "drop_id": drop_id, "chat_id": _norm_chat_id(chat_id),
                "manager_username": (manager_username or "").lstrip("@").lower(),
                "fio": fio or "", "about": about or "",
                "scan_file_ids": list(scan_file_ids or []),
                "status": "draft", "created_at": time.time(), "lk_card_ids": [],
            }
            await self._bump_outsource_manager_stat_unlocked(manager_username, "drops_total", 1)
            await self._save_unlocked()
            return drop_id

    async def update_outsource_drop(self, drop_id: str, **fields) -> bool:
        async with _lock:
            drop = (self.state.get("outsource_drops") or {}).get(str(drop_id))
            if not drop: return False
            for k, v in fields.items(): drop[k] = v
            drop["updated_at"] = time.time()
            await self._save_unlocked()
            return True

    def list_outsource_drop_lks(self, outsource_drop_id: Optional[str] = None) -> dict:
        lks = self.state.get("outsource_drop_lks") or {}
        if outsource_drop_id:
            return {k: v for k, v in lks.items() if v.get("outsource_drop_id") == outsource_drop_id}
        return lks

    def get_outsource_drop_lk(self, droplk_id) -> Optional[dict]:
        return (self.state.get("outsource_drop_lks") or {}).get(str(droplk_id))

    async def add_outsource_drop_lk(self, outsource_drop_id: str, manager_username: str,
                                     bank: str = "", value: str = "", deal: str = "") -> str:
        async with _lock:
            seq = (self.state.get("outsource_drop_lks_seq") or 0) + 1
            self.state["outsource_drop_lks_seq"] = seq
            droplk_id = f"olk{seq:05d}"
            self.state.setdefault("outsource_drop_lks", {})[droplk_id] = {
                "droplk_id": droplk_id, "outsource_drop_id": outsource_drop_id,
                "manager_username": (manager_username or "").lstrip("@").lower(),
                "bank": bank or "", "value": value or "", "deal": deal or "",
                "sms_history": [], "sms_stage": "",
                "new_login": "", "new_password": "", "new_mail": "",
                "new_number": "", "code_word": "",
                "ded_ip": "", "ded_login": "Administrator", "ded_pass": "", "ded_location": "",
                "msgid_pass": 0, "sms_tracker_msg_id": 0,
                "created_at": time.time(), "updated_at": time.time(),
            }
            drop = (self.state.get("outsource_drops") or {}).get(outsource_drop_id)
            if drop:
                drop.setdefault("lk_card_ids", []).append(droplk_id)
            await self._bump_outsource_manager_stat_unlocked(manager_username, "lks_total", 1)
            await self._save_unlocked()
            return droplk_id

    async def update_outsource_drop_lk(self, droplk_id, **fields) -> bool:
        async with _lock:
            lk = (self.state.get("outsource_drop_lks") or {}).get(str(droplk_id))
            if not lk: return False
            for k, v in fields.items(): lk[k] = v
            lk["updated_at"] = time.time()
            await self._save_unlocked()
            return True

    async def delete_outsource_drop_lk(self, droplk_id) -> bool:
        async with _lock:
            lks = self.state.get("outsource_drop_lks") or {}
            if str(droplk_id) in lks:
                lk = lks[str(droplk_id)]
                odrop_id = lk.get("outsource_drop_id")
                if odrop_id:
                    drop = (self.state.get("outsource_drops") or {}).get(odrop_id)
                    if drop and droplk_id in (drop.get("lk_card_ids") or []):
                        try: drop["lk_card_ids"].remove(droplk_id)
                        except ValueError: pass
                del lks[str(droplk_id)]
                await self._save_unlocked()
                return True
            return False

    async def append_outsource_sms(self, droplk_id, code, time_str=""):
        async with _lock:
            lk = (self.state.get("outsource_drop_lks") or {}).get(str(droplk_id))
            if not lk: return False
            lk.setdefault("sms_history", []).append({
                "code": code, "time": time_str or time.strftime("%d.%m.%Y %H:%M")
            })
            await self._save_unlocked()
            return True

    # --- Перенос ЛК в Аутсорс из любого track ---
    async def move_any_lk_to_outsource(self, droplk_id: str, manager_username: str) -> Optional[str]:
        """Универсальный перенос: любой ЛК (lk* / clk*) → outsource (olk*)."""
        s = str(droplk_id or "")
        lk = self.get_drop_lk_any(s)
        if not lk: return None
        # Определяем источник
        is_credit = s.startswith("clk")
        async with _lock:
            ou_drops = self.state.setdefault("outsource_drops", {})
            ou_lks = self.state.setdefault("outsource_drop_lks", {})
            mgr = (manager_username or "").lstrip("@").lower()
            # Получаем исходный drop для FIO
            if is_credit:
                src_drop_id = lk.get("credit_drop_id")
                src_drops = self.state.get("credit_drops") or {}
            else:
                src_drop_id = lk.get("drop_id")
                src_drops = self.state.get("crm_drops") or {}
            src_drop = src_drops.get(src_drop_id, {}) if src_drop_id else {}
            fio = (src_drop.get("fio") or "").strip()
            # Ищем или создаём outsource_drop по manager+fio
            existing_odrop_id = None
            for oid, odrop in ou_drops.items():
                if (odrop.get("manager_username") or "") == mgr and \
                   (odrop.get("fio") or "").strip() == fio:
                    existing_odrop_id = oid; break
            if existing_odrop_id:
                odrop_id = existing_odrop_id
            else:
                seq = (self.state.get("outsource_drops_seq") or 0) + 1
                self.state["outsource_drops_seq"] = seq
                odrop_id = f"odrp{seq:05d}"
                ou_drops[odrop_id] = {
                    "drop_id": odrop_id, "chat_id": src_drop.get("chat_id") or src_drop.get("work_chat_id") or "",
                    "manager_username": mgr, "fio": fio,
                    "about": src_drop.get("about") or "",
                    "scan_file_ids": list(src_drop.get("scan_file_ids") or []),
                    "status": src_drop.get("status") or "draft",
                    "created_at": time.time(), "lk_card_ids": [],
                    "_moved_from": ("credit" if is_credit else "crm") + ":" + (src_drop_id or ""),
                }
                await self._bump_outsource_manager_stat_unlocked(manager_username, "drops_total", 1)
            # Создаём outsource_drop_lk
            seq_lk = (self.state.get("outsource_drop_lks_seq") or 0) + 1
            self.state["outsource_drop_lks_seq"] = seq_lk
            new_olk_id = f"olk{seq_lk:05d}"
            # Берём все поля кроме drop-связей и owner/manager
            exclude_keys = {"drop_id", "credit_drop_id", "owner_id", "manager_username"}
            new_lk = {k: v for k, v in lk.items() if k not in exclude_keys}
            new_lk.update({
                "droplk_id": new_olk_id, "outsource_drop_id": odrop_id,
                "manager_username": mgr,
                "_moved_from": ("credit_lk:" if is_credit else "crm_lk:") + s,
                "_moved_at": time.time(),
                "updated_at": time.time(),
            })
            ou_lks[new_olk_id] = new_lk
            ou_drops[odrop_id].setdefault("lk_card_ids", []).append(new_olk_id)
            await self._bump_outsource_manager_stat_unlocked(manager_username, "lks_total", 1)
            # Удаляем из источника
            if is_credit:
                src_lks = self.state.get("credit_drop_lks") or {}
                if s in src_lks:
                    del src_lks[s]
                    if src_drop and s in (src_drop.get("lk_card_ids") or []):
                        try: src_drop["lk_card_ids"].remove(s)
                        except ValueError: pass
                    if src_drop and not src_drop.get("lk_card_ids"):
                        src_drop["status"] = "moved_to_outsource"
            else:
                src_lks = self.state.get("crm_drop_lks") or {}
                if s in src_lks:
                    del src_lks[s]
                    if src_drop and s in (src_drop.get("lk_card_ids") or []):
                        try: src_drop["lk_card_ids"].remove(s)
                        except ValueError: pass
                    if src_drop and not src_drop.get("lk_card_ids"):
                        src_drop["status"] = "moved_to_outsource"
            await self._save_unlocked()
            return new_olk_id

    async def move_outsource_lk_to_crm(self, outsource_droplk_id: str, owner_id: Optional[str] = None) -> Optional[str]:
        """Обратно из Аутсорса в Поставщики."""
        async with _lock:
            ou_lks = self.state.get("outsource_drop_lks") or {}
            lk = ou_lks.get(str(outsource_droplk_id))
            if not lk: return None
            ou_drops = self.state.get("outsource_drops") or {}
            old_odrop_id = lk.get("outsource_drop_id")
            old_odrop = ou_drops.get(old_odrop_id, {}) if old_odrop_id else {}
            crm_drops = self.state.setdefault("crm_drops", {})
            crm_lks = self.state.setdefault("crm_drop_lks", {})
            fio = (old_odrop.get("fio") or "").strip()
            target_drop_id = None
            for did, drop in crm_drops.items():
                if owner_id and drop.get("owner_id") != owner_id: continue
                if (drop.get("fio") or "").strip() == fio:
                    target_drop_id = did; break
            if not target_drop_id:
                seq = (self.state.get("crm_drops_seq") or 0) + 1
                self.state["crm_drops_seq"] = seq
                target_drop_id = f"d{seq:04d}"
                crm_drops[target_drop_id] = {
                    "drop_id": target_drop_id, "owner_id": owner_id or "",
                    "chat_id": old_odrop.get("chat_id") or "", "fio": fio,
                    "about": old_odrop.get("about") or "",
                    "scan_file_ids": list(old_odrop.get("scan_file_ids") or []),
                    "status": "draft", "created_at": time.time(), "lk_card_ids": [],
                    "_moved_from_outsource_drop": old_odrop_id or "",
                }
            seq_lk = (self.state.get("crm_drop_lks_seq") or 0) + 1
            self.state["crm_drop_lks_seq"] = seq_lk
            new_lk_id = f"lk{seq_lk:05d}"
            new_lk = {k: v for k, v in lk.items() if k not in ("outsource_drop_id", "manager_username")}
            new_lk.update({
                "droplk_id": new_lk_id, "drop_id": target_drop_id, "owner_id": owner_id or "",
                "_moved_from_outsource_lk": str(outsource_droplk_id),
                "_moved_at": time.time(), "updated_at": time.time(),
            })
            crm_lks[new_lk_id] = new_lk
            crm_drops[target_drop_id].setdefault("lk_card_ids", []).append(new_lk_id)
            del ou_lks[str(outsource_droplk_id)]
            if old_odrop and outsource_droplk_id in (old_odrop.get("lk_card_ids") or []):
                try: old_odrop["lk_card_ids"].remove(outsource_droplk_id)
                except ValueError: pass
            if old_odrop and not old_odrop.get("lk_card_ids"):
                old_odrop["status"] = "moved_to_supplier"
            await self._save_unlocked()
            return new_lk_id

    # =====================================================================
    # MOVE LK BETWEEN TRACKS (Поставщики ↔ Кредитование)
    # =====================================================================
    async def move_crm_lk_to_credit(
        self, droplk_id: str, manager_username: str,
    ) -> Optional[str]:
        """Переносит ЛК из crm_drop_lks → credit_drop_lks.
        Если у анкеты остаются ещё ЛК — она НЕ удаляется.
        Если это последний ЛК анкеты — анкета тоже переезжает.
        Возвращает новый credit_droplk_id или None.
        """
        async with _lock:
            crm_lks = self.state.get("crm_drop_lks") or {}
            crm_drops = self.state.get("crm_drops") or {}
            lk = crm_lks.get(str(droplk_id))
            if not lk:
                return None
            old_drop_id = lk.get("drop_id")
            old_drop = crm_drops.get(old_drop_id, {}) if old_drop_id else {}

            # 1) Создаём (или находим) credit_drop под этого менеджера + этого FIO
            credit_drops = self.state.setdefault("credit_drops", {})
            credit_lks = self.state.setdefault("credit_drop_lks", {})
            mgr_lower = (manager_username or "").lstrip("@").lower()
            fio = (old_drop.get("fio") or "").strip()
            existing_cdrop_id = None
            for cdid, cdrop in credit_drops.items():
                if (cdrop.get("manager_username") or "") == mgr_lower and (
                    (cdrop.get("fio") or "").strip() == fio
                ):
                    existing_cdrop_id = cdid
                    break
            if existing_cdrop_id:
                cdrop_id = existing_cdrop_id
            else:
                seq = (self.state.get("credit_drops_seq") or 0) + 1
                self.state["credit_drops_seq"] = seq
                cdrop_id = f"cdrp{seq:05d}"
                credit_drops[cdrop_id] = {
                    "drop_id": cdrop_id,
                    "chat_id": old_drop.get("chat_id") or "",
                    "manager_username": mgr_lower,
                    "fio": fio,
                    "about": old_drop.get("about") or "",
                    "scan_file_ids": list(old_drop.get("scan_file_ids") or []),
                    "status": old_drop.get("status") or "draft",
                    "created_at": time.time(),
                    "lk_card_ids": [],
                    "_moved_from_crm_drop": old_drop_id or "",
                }
                await self._bump_credit_manager_stat_unlocked(manager_username, "drops_total", 1)

            # 2) Создаём credit_droplk с теми же данными
            seq_lk = (self.state.get("credit_drop_lks_seq") or 0) + 1
            self.state["credit_drop_lks_seq"] = seq_lk
            new_clk_id = f"clk{seq_lk:05d}"
            credit_lks[new_clk_id] = {
                **{k: v for k, v in lk.items() if k != "drop_id"},
                "droplk_id": new_clk_id,
                "credit_drop_id": cdrop_id,
                "manager_username": mgr_lower,
                "_moved_from_crm_lk": str(droplk_id),
                "_moved_at": time.time(),
                "updated_at": time.time(),
            }
            credit_drops[cdrop_id].setdefault("lk_card_ids", []).append(new_clk_id)
            await self._bump_credit_manager_stat_unlocked(manager_username, "lks_total", 1)

            # 3) Удаляем ЛК из crm_drop_lks
            del crm_lks[str(droplk_id)]
            # Убираем из drop.lk_card_ids
            if old_drop and old_drop.get("lk_card_ids"):
                try:
                    old_drop["lk_card_ids"].remove(str(droplk_id))
                except ValueError:
                    pass
            # Если у crm_drop не осталось ЛК — пометим как пустой (не удаляем для истории)
            if old_drop and not old_drop.get("lk_card_ids"):
                old_drop["status"] = "moved_to_credit"

            await self._save_unlocked()
            return new_clk_id

    async def move_credit_lk_to_crm(
        self, credit_droplk_id: str, owner_id: Optional[str] = None,
    ) -> Optional[str]:
        """Переносит ЛК из credit_drop_lks → crm_drop_lks. Зеркало move_crm_lk_to_credit.
        owner_id — куда переносить (если None и есть crm_drop с тем же FIO — туда; иначе создаст draft).
        """
        async with _lock:
            credit_lks = self.state.get("credit_drop_lks") or {}
            credit_drops = self.state.get("credit_drops") or {}
            lk = credit_lks.get(str(credit_droplk_id))
            if not lk:
                return None
            old_cdrop_id = lk.get("credit_drop_id")
            old_cdrop = credit_drops.get(old_cdrop_id, {}) if old_cdrop_id else {}

            crm_drops = self.state.setdefault("crm_drops", {})
            crm_lks = self.state.setdefault("crm_drop_lks", {})
            fio = (old_cdrop.get("fio") or "").strip()
            # Ищем подходящий crm_drop по owner_id + FIO
            target_drop_id = None
            for did, drop in crm_drops.items():
                if owner_id and drop.get("owner_id") != owner_id:
                    continue
                if (drop.get("fio") or "").strip() == fio:
                    target_drop_id = did
                    break
            if not target_drop_id:
                # Создаём новый draft crm_drop
                seq = (self.state.get("crm_drops_seq") or 0) + 1
                self.state["crm_drops_seq"] = seq
                target_drop_id = f"drp{seq:05d}"
                crm_drops[target_drop_id] = {
                    "drop_id": target_drop_id,
                    "owner_id": owner_id or "",
                    "chat_id": old_cdrop.get("chat_id") or "",
                    "fio": fio, "about": old_cdrop.get("about") or "",
                    "scan_file_ids": list(old_cdrop.get("scan_file_ids") or []),
                    "status": old_cdrop.get("status") or "draft",
                    "created_at": time.time(), "lk_card_ids": [],
                    "_moved_from_credit_drop": old_cdrop_id or "",
                }

            # Создаём новый crm_drop_lk
            seq_lk = (self.state.get("crm_drop_lks_seq") or 0) + 1
            self.state["crm_drop_lks_seq"] = seq_lk
            new_lk_id = f"lk{seq_lk:05d}"
            crm_lks[new_lk_id] = {
                **{k: v for k, v in lk.items() if k != "credit_drop_id" and k != "manager_username"},
                "droplk_id": new_lk_id,
                "drop_id": target_drop_id,
                "owner_id": owner_id or "",
                "_moved_from_credit_lk": str(credit_droplk_id),
                "_moved_at": time.time(),
                "updated_at": time.time(),
            }
            crm_drops[target_drop_id].setdefault("lk_card_ids", []).append(new_lk_id)

            # Удаляем credit_drop_lk
            del credit_lks[str(credit_droplk_id)]
            if old_cdrop and old_cdrop.get("lk_card_ids"):
                try:
                    old_cdrop["lk_card_ids"].remove(str(credit_droplk_id))
                except ValueError:
                    pass
            if old_cdrop and not old_cdrop.get("lk_card_ids"):
                old_cdrop["status"] = "moved_to_supplier"

            await self._save_unlocked()
            return new_lk_id

    # =====================================================================
    # OPERATIONAL / EXCHANGE REQUESTS (фикс заявок на обмен)
    # =====================================================================
    def list_exchange_requests(self, status: Optional[str] = None) -> dict:
        reqs = self.state.get("exchange_requests") or {}
        if status:
            return {k: v for k, v in reqs.items() if (v.get("status") or "") == status}
        return reqs

    def get_exchange_request(self, req_id: str) -> Optional[dict]:
        return (self.state.get("exchange_requests") or {}).get(req_id)

    async def add_exchange_request(self, **fields) -> str:
        """Создаёт новую фикс заявку.
        Поля: bank_in, fio_in, lk_card_id_in, amount_in,
              outs: [ { bank, fio, lk_card_id, jur_jur_receivers: [ {bank, fio} ], amount_out } ],
              partner_pct, exchange_rate, commission_usdt,
              margin_usdt (calculated), status, created_by.
        """
        async with _lock:
            seq = int(self.state.get("exchange_requests_seq", 0)) + 1
            req_id = f"ex{seq:04d}"
            self.state["exchange_requests_seq"] = seq
            reqs = self.state.setdefault("exchange_requests", {})
            base = {
                "req_id": req_id,
                "bank_in": (fields.get("bank_in") or "").upper(),
                "fio_in": fields.get("fio_in") or "",
                "lk_card_id_in": fields.get("lk_card_id_in") or "",
                "amount_in": float(fields.get("amount_in") or 0),
                "outs": list(fields.get("outs") or []),
                "partner_pct": float(fields.get("partner_pct") or 0),
                "exchange_rate": float(fields.get("exchange_rate") or 0),
                "commission_usdt": float(fields.get("commission_usdt") or 0),
                "margin_usdt": float(fields.get("margin_usdt") or 0),
                "total_in_rub": float(fields.get("total_in_rub") or 0),
                "total_out_rub": float(fields.get("total_out_rub") or 0),
                "status": fields.get("status") or "ЗАФИКСИРОВАНА",
                "created_by": fields.get("created_by") or "operationist",
                "created_at": time.time(),
                "involved_lk_cards": list(fields.get("involved_lk_cards") or []),
            }
            reqs[req_id] = base
            await self._save_unlocked()
            return req_id

    async def update_exchange_request(self, req_id: str, **fields) -> bool:
        async with _lock:
            req = (self.state.setdefault("exchange_requests", {})).get(req_id)
            if not req:
                return False
            for k, v in fields.items():
                req[k] = v
            req["updated_at"] = time.time()
            await self._save_unlocked()
            return True

    def list_lk_in_work(self) -> list:
        """ЛК, переданные в Операционную (status В_РАБОТЕ + перевязано).
        Каждая запись содержит lk_card_id, bank, fio, supplier, perevyaz_ts,
        is_combo (флаг связки = 2+ ЛК с одной анкеты)."""
        cards = self.state.get("lk_cards") or {}
        result = []
        # Группировка для определения combo (одна анкета = одинаковый fio+supplier)
        groups = {}
        for cid, c in cards.items():
            if not c:
                continue
            if (c.get("status") or "В_РАБОТЕ") != "В_РАБОТЕ":
                continue
            key = (
                (c.get("fio") or "").strip().lower(),
                (c.get("supplier") or "").lstrip("@").lower(),
            )
            groups.setdefault(key, []).append((cid, c))
        for key, lst in groups.items():
            is_combo = len(lst) > 1
            for cid, c in lst:
                bank_upper = (c.get("bank") or "").upper()
                result.append({
                    "card_id": cid,
                    "bank": bank_upper,
                    "fio": c.get("fio") or "",
                    "supplier": (c.get("supplier") or "").lstrip("@"),
                    "perevyaz_ts": c.get("created_at") or 0,
                    "deal_id": c.get("deal_id") or "",
                    "payment_method": c.get("payment_method") or "",
                    # Если у ЛК price_usdt пустой/0 — берём дефолт по банку
                    # (storage.pricing → DEFAULT_LK_PRICES → 0)
                    "price_usdt": self.resolve_lk_price(bank_upper, c.get("price_usdt")),
                    "is_combo": is_combo,
                    "combo_size": len(lst),
                })
        return result

    # =====================================================================
    # ACCOUNTING ENTRIES (Бухгалтерия v2: касса/зарплаты/реклама/симки/поставщики)
    # =====================================================================
    def list_accounting_entries(
        self,
        category: Optional[str] = None,
        date_from: Optional[float] = None,
        date_to: Optional[float] = None,
    ) -> list:
        """Возвращает список бухгалтерских записей.

        Категории: 'kassa', 'suppliers', 'salaries', 'ads', 'sims'.
        Маржа считается отдельно (kassa - sum(suppliers,salaries,ads,sims))."""
        entries = self.state.get("accounting_entries") or []
        out = []
        for e in entries:
            if category and (e.get("category") or "") != category:
                continue
            ts = float(e.get("ts") or 0)
            if date_from is not None and ts < date_from:
                continue
            if date_to is not None and ts > date_to:
                continue
            out.append(e)
        out.sort(key=lambda x: x.get("ts") or 0, reverse=True)
        return out

    async def add_accounting_entry(
        self,
        category: str,
        amount_usdt: float,
        amount_rub: float = 0.0,
        note: str = "",
        created_by: str = "",
        ref_id: str = "",
    ) -> str:
        """Добавляет запись в Бухгалтерию.
        category: 'kassa' | 'suppliers' | 'salaries' | 'ads' | 'sims'.
        Возвращает entry_id."""
        async with _lock:
            seq = int(self.state.get("accounting_entries_seq", 0)) + 1
            entry_id = f"ac{seq:05d}"
            self.state["accounting_entries_seq"] = seq
            entries = self.state.setdefault("accounting_entries", [])
            entry = {
                "entry_id": entry_id,
                "category": category,
                "amount_usdt": float(amount_usdt or 0),
                "amount_rub": float(amount_rub or 0),
                "note": note or "",
                "created_by": created_by or "",
                "ref_id": ref_id or "",
                "ts": time.time(),
            }
            entries.append(entry)
            await self._save_unlocked()
            return entry_id

    async def delete_accounting_entry(self, entry_id: str) -> bool:
        async with _lock:
            entries = self.state.get("accounting_entries") or []
            n0 = len(entries)
            entries[:] = [e for e in entries if (e.get("entry_id") or "") != entry_id]
            if len(entries) != n0:
                self.state["accounting_entries"] = entries
                await self._save_unlocked()
                return True
            return False

    def accounting_summary(
        self,
        date_from: Optional[float] = None,
        date_to: Optional[float] = None,
    ) -> dict:
        """Сводка по категориям + общая маржа за период.
        Маржа = касса − (suppliers + salaries + ads + sims)."""
        cats = {
            "kassa": 0.0,
            "suppliers": 0.0,
            "salaries": 0.0,
            "ads": 0.0,
            "sims": 0.0,
        }
        cats_rub = dict(cats)
        for e in (self.state.get("accounting_entries") or []):
            ts = float(e.get("ts") or 0)
            if date_from is not None and ts < date_from:
                continue
            if date_to is not None and ts > date_to:
                continue
            cat = e.get("category") or ""
            if cat in cats:
                cats[cat] += float(e.get("amount_usdt") or 0)
                cats_rub[cat] += float(e.get("amount_rub") or 0)
        margin_usdt = (
            cats["kassa"]
            - cats["suppliers"]
            - cats["salaries"]
            - cats["ads"]
            - cats["sims"]
        )
        return {
            "categories_usdt": cats,
            "categories_rub": cats_rub,
            "margin_usdt": margin_usdt,
            "period": {"from": date_from, "to": date_to},
        }


# ============================================================
# AES-256-CBC шифрование StringSession для worker_sessions.
# Module-level helpers (используются из api.py и userbot.py).
# ============================================================

def _session_crypto_key() -> bytes:
    """32-байтный ключ AES из env SESSION_SECRET."""
    import hashlib, os
    secret = os.getenv("SESSION_SECRET", "") or "pride-default-fallback-please-change"
    return hashlib.sha256(secret.encode()).digest()


def encrypt_session(plaintext: str) -> str:
    """Шифрует StringSession через AES-256-CBC + base64. Префикс v1: / v0: fallback."""
    if not plaintext:
        return ""
    try:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        from cryptography.hazmat.primitives import padding
        import os as _os, base64 as _b64
        key = _session_crypto_key()
        iv = _os.urandom(16)
        padder = padding.PKCS7(128).padder()
        padded = padder.update(plaintext.encode("utf-8")) + padder.finalize()
        cipher = Cipher(algorithms.AES(key), modes.CBC(iv))
        enc = cipher.encryptor()
        ct = enc.update(padded) + enc.finalize()
        return "v1:" + _b64.b64encode(iv + ct).decode("ascii")
    except Exception:
        import base64 as _b64
        return "v0:" + _b64.b64encode(plaintext.encode("utf-8")).decode("ascii")


def decrypt_session(encoded: str) -> str:
    """Расшифровывает StringSession. v0: — простой base64, v1: — AES."""
    if not encoded:
        return ""
    try:
        if encoded.startswith("v0:"):
            import base64 as _b64
            return _b64.b64decode(encoded[3:].encode("ascii")).decode("utf-8")
        if encoded.startswith("v1:"):
            from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
            from cryptography.hazmat.primitives import padding
            import base64 as _b64
            blob = _b64.b64decode(encoded[3:].encode("ascii"))
            iv, ct = blob[:16], blob[16:]
            cipher = Cipher(algorithms.AES(_session_crypto_key()), modes.CBC(iv))
            dec = cipher.decryptor()
            padded = dec.update(ct) + dec.finalize()
            unpadder = padding.PKCS7(128).unpadder()
            return (unpadder.update(padded) + unpadder.finalize()).decode("utf-8")
        return encoded
    except Exception:
        return ""


storage = Storage(config.STORAGE_PATH)
