"""Userbot service: создаёт супергруппы, приглашает работников, шлёт welcome при входе клиента.

Welcome delivery has two channels:
1. Realtime: events.ChatAction handler (fires when Telegram pushes us the join event)
2. Fallback: a per-chat polling task that polls participants every 3s for up to 10 min,
   sends welcome as soon as the expected client_id appears.

Race condition protection: each chat has its own asyncio.Lock (_welcome_locks).
Only the first coroutine to acquire the lock will actually send the message;
the second will see welcome_sent=True and exit immediately.
"""
import logging
import asyncio
import random
import re
import time
from typing import Optional, Tuple

from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.tl.functions.channels import (
    CreateChannelRequest,
    InviteToChannelRequest,
    EditAdminRequest,
    GetParticipantRequest,
)
from telethon.tl.functions.messages import ExportChatInviteRequest
from telethon.tl.types import (
    ChatAdminRights, PeerUser,
    MessageEntityBold, MessageEntityItalic, MessageEntityUnderline,
    MessageEntityStrike, MessageEntityCode, MessageEntityPre,
    MessageEntityBlockquote, MessageEntityTextUrl, MessageEntityCustomEmoji,
)
from telethon.errors import (
    UserPrivacyRestrictedError,
    UserNotMutualContactError,
    FloodWaitError,
    PeerFloodError,
    UsernameNotOccupiedError,
    UserNotParticipantError,
)
try:
    from telethon.errors import UserAlreadyParticipantError  # type: ignore
except ImportError:  # старые версии
    class UserAlreadyParticipantError(Exception):
        pass

import config
from storage import storage
import brain
import memory
import accounting2
import learn

# Event bus для дашборда. Безопасный no-op fallback если модуль не доступен.
try:
    from event_bus import emit_event as _emit_event
    def _e(t, payload=None, character="", severity="info"):
        try:
            _emit_event(t, payload, character, severity)
        except Exception:
            pass
except Exception:
    def _e(t, payload=None, character="", severity="info"):
        pass

logger = logging.getLogger(__name__)


# Шаблоны «AI вслух заявляет что молчит» — ловим и не отправляем.
# AI должен МОЛЧАТЬ а не АНОНСИРОВАТЬ молчание. Каждое такое сообщение —
# выкинутые кредиты + мусор в клиентском чате.
_SILENCE_ANNOUNCEMENT_PATTERNS = [
    r"\bвнутренн\w*\s+диалог\b",
    r"\bвнутренн\w*\s+обмен\b",
    r"\bобмен\s+данными\s+между\s+командой\b",
    r"\bпродолжаю\s+молчать\b",
    r"\bмолчу[,.\s]",
    r"\bпока\s+молчу\b",
    r"\bне\s+вмешива\w+\b",
    r"\bв\s+диалог\s+не\s+вступаю\b",
    r"\bне\s+вступаю\s+в\s+диалог\b",
    r"\bждуу?\s+пока\s+клиент\b",
    r"\bждуу?\s+когда\s+клиент\b",
    r"\bжду\s+действий\s+клиента\b",
    r"\bжду\s+результат\b.*\bперевязк\w*",
    r"\bпропускаю\s+ответ\b",
    r"\bsilent\s+mode\b",
    r"\b\[\s*молч\w*\s*\]",
    r"^\s*\(\s*молч\w*\s*\)\s*$",
]
_SILENCE_RX = [re.compile(p, re.IGNORECASE | re.MULTILINE) for p in _SILENCE_ANNOUNCEMENT_PATTERNS]


def _is_silence_announcement(text: str) -> bool:
    """True если ответ AI — это анонс молчания вместо реального молчания."""
    if not text:
        return True  # пустота = тоже молчание, не шлём
    stripped = text.strip()
    # Очень короткие «..»/«ок»/«…» — пропускаем (это норм короткий ответ)
    if len(stripped) < 6:
        return False
    # Если хоть один из «silence-announcement» паттернов матчится — это шум
    for rx in _SILENCE_RX:
        if rx.search(stripped):
            return True
    return False


# Запрещённые темы в ответах клиенту (блок / отказ / самообвинение / спекуляции
# про команду). Если хоть один паттерн матчится — ответ не отправляется,
# AI принудительно молчит. См. knowledge/policy.md секция «ЗАПРЕЩЁННЫЕ ТЕМЫ».
_FORBIDDEN_CLIENT_PATTERNS = [
    # Самообвинение / брак на нашей стороне
    r"\bбрак\s+на\s+нашей\s+сторон\w*",
    r"\bнаша\s+ошибк\w*",
    r"\bнаш\s+косяк\b",
    r"\bмы\s+виноват\w*",
    r"\bу\s+нас\s+(?:произош\w+|сбой|проблем\w*)",
    r"\bпроизошёл\s+(?:критическ\w+\s+)?сбой\s+в\s+систем\w*",
    r"\bошибк\w*\s+при\s+обработк\w*",
    # Невозможные обещания (пересдача отклонённых счетов и т.п.)
    r"\bпересда(?:ть|ча|чу|чи|дим|дите|м|йте)\b",
    r"\bпересда\w*\s+счет\w*",
    r"\bповторит\w*\s+перевязк\w*",
    r"\bвзять\s+(?:их\s+)?снова\b",
    # Технические спекуляции про команду
    r"\bоперационист\w+\s+(?:не\s+)?(?:взял\w*|должн\w*|смог\w*)",
    r"\bтехническ\w+\s+перевязан\w*\s+но\s+платеж\w*",
    r"\b(?:тимон|@?timon\w*)\s+(?:точно\s+)?в\s+курсе\??",
    r"\b(?:тимон|@?timon\w*)\s+должен\s+был",
    r"\bчто\s+говорит\s+(?:тимон|@?timon\w*)",
    r"\bпочему\s+(?:тимон|@?timon\w*)\s+не\s+(?:заметил|написал|сообщил)",
    # Обещания компенсаций
    r"\b(?:возврат|компенсаци\w+|переоценк\w*\s+потер\w+)\s+(?:если|и\s+|или)",
    # Извинения за выдуманные сбои
    r"\bвозможно\s+операционист\w+",
    r"\bможет\s+быть\s+(?:блокировк\w*|сбой|баг)",
    # 🔴 Запрет: говорить партнёру пригласить дропа в чат / дать ссылку.
    # Партнёр ведёт ВСЕХ дропов сам через /clients — дропу в наш чат не надо.
    r"\bдобавьте\s+(?:его|её|их|дропа)?\s*в\s+(?:этот\s+)?чат",
    r"\bпригласите\s+(?:его|её|их|дропа)\s*(?:сюда|в\s+чат)?",
    r"\bдайте\s+(?:ему|ей|им|дропу)\s+ссылку\s+на\s+(?:этот\s+)?чат",
    r"\bпригласите\s+(?:его|её|их|дропа)?\s*в\s+(?:этот\s+)?чат",
    r"\bпопросите\s+(?:его|её|их|дропа)\s+(?:написать|зайти)\s+в\s+(?:этот\s+)?чат",
    r"\bон\s+пропиш(?:ет|и)\s+/clients\s+(?:прямо\s+)?здесь",
    r"\bпусть\s+он(?:а)?\s+(?:напишет|пропишет)",
    # 🔴 Запрет: говорить КЛИЕНТУ что ОН пополняет сделку. Сделку пополняем МЫ.
    # Клиент только СОЗДАЁТ сделку и присылает номер. Деньги вносим мы.
    # ⚠️ Фразы narrow — матчим только когда они адресованы клиенту прямо
    # («вам нужно пополнить», «как вы пополните»). Слова про «пополнение»
    # сами по себе допустимы (например «мы пополним», «после пополнения отпустим»).
    r"\bпополните\s+(?:обе\s+)?сделк(?:у|и)\b",
    r"\bвнесите\s+(?:средства|деньг\w+|сумм\w+)\s+(?:в\s+)?гарант",
    r"\bвам\s+нужно\s+пополнить\s+сделк",
    r"\bпосле\s+(?:вашего\s+)?пополнения\s+вами\b",
    r"\bкак\s+(?:только\s+)?вы\s+пополните\s+сделк",
    r"\bкогда\s+вы\s+пополните\s+сделк",
    r"\bждём\s+(?:вашего\s+)?подтверждения\s+пополнения\s+(?:от\s+вас|с\s+вашей)",
    r"\bждём\s+пополнения\s+(?:от\s+вас|с\s+вашей\s+стороны)",
    # 🔴 Отказ клиенту требующему гарант ДО отработки — недопустим.
    # Если клиент сказал «хочу гарант сейчас / до отработки» — соглашаемся,
    # а не отговариваем «1-3 дня после отработки».
    r"\b(?:обычно\s+)?(?:это\s+)?занима(?:ет|ёт)\s+1\W{1,3}3\s+дн",
    r"\bожидайте\s+(?:результата\s+)?отработк",
    r"\bпополнение\s+(?:происходит|идёт|идет|будет)\s+только\s+после",
    r"\bкогда\s+отработаем\W{1,3}пополним",
    r"\bсч[её]т\s+(?:ещ[её]\s+)?в\s+работе\s+у\s+операционист",
    # 🔴🔴🔴 КРИТИЧНЫЙ ЗАПРЕТ: AI принимает вину PRIDE / соглашается что клиент прав
    # в споре / меняет решение под давлением / просит прощения от лица команды.
    # Это уничтожает доверие к компании и провоцирует ещё больший скандал.
    r"\b(?:это|такое)\s+пиздец\w*",  # ругательства в ответах клиенту
    r"\bмы\s+(?:тебя\s+)?(?:просрали|подставили|кинули|обма?ну\w*)",
    r"\bты\s+(?:абсолютно|совершенно|полностью|просто)\s+прав\w*",
    r"\b(?:ты|вы)\s+(?:прав|правы)\W{0,4}он\s+(?:честно|прав)",
    r"\b(?:это|такое)\s+недопустимо",
    r"\bклиентск\w+\s+сервис\s+по\s+пол",
    r"\bмы\s+в\s+этом\s+виноват\w*",
    r"\b(?:это|тут)\s+уже\s+не\s+ошибк[ау]",
    r"\bизвин(?:и|ите)\s+что\s+(?:допустили|такое\s+случилось)",
    r"\bон\s+больше\s+ждать\s+не\s+(?:может|должен)",
    r"\bвытя(?:гивают|нем)\s+(?:прямо\s+)?сейчас",
    r"\bнаш\s+косяк\b",
    # AI не должен ТЕГАТЬ менеджеров в ответе клиенту (escalate_to_team — да,
    # но в самом сообщении клиенту тегов быть не должно).
    r"@TimonSkupCL\b", r"@SIMBA_PRIDE_ADM\b", r"@pride_sys0\d+\b",
    # Обещания выплаты при БЛОК-статусе ЛК — недопустимо.
    r"\b(?:сейчас|сегодня|немедленно)\s+(?:выплатим|переведём|перечислим)",
    # Принятие обвинений в спам-режиме
    r"\bты\s+(?:абсолютно|совершенно)\s+прав\W{0,5}мы",
    # 🔴 Запрет: предлагать клиенту цену ВЫШЕ чем он назвал.
    # AI не должен «исправлять» клиента типа «вы наверное имели в виду 400»,
    # «по нашему прайсу X», «а может за Y», «с учётом долга у вас выйдет Z».
    # Если клиент назвал 170 — AI соглашается на 170. Точка.
    r"\bпо\s+(?:нашему\s+)?прайсу\s+\d+\W{0,4}\$",
    r"\bвы\s+наверн(?:о|ое)\s+имели\s+в\s+виду",
    r"\bвы\s+(?:точно|правда)\s+имели\s+в\s+виду\s+\d+",
    r"\bу\s+нас\s+прайс\s+вы(?:ше|сше)",
    r"\bнаша\s+цена\s+\d+\W{0,4}\$",
    r"\bу\s+нас\s+цена\s+вы(?:ше|сше)",
    r"\bа\s+может\s+за\s+\d+",
    r"\bс\s+учёт(?:ом|ом)\s+долг\w*\s+(?:у\s+)?вас\s+выйдет\s+\d+",
    r"\bвыплата\s+составит\s+\d+\W{0,4}\$",
    # 🔴 Запрет: AI НЕ СПРАШИВАЕТ цену у клиента — мы её НАЗЫВАЕМ из storage.lk_prices.
    # Цена назначаем МЫ, не клиент решает «сколько вы хотите».
    r"\bкакая\s+(?:сумма|цена)\s+выплат\w*",
    r"\bна\s+какую\s+сумму\s+(?:рассчитыв|надеетес|претенду)",
    r"\bкакую\s+сумму\s+(?:хотите|ждёте|ожидаете|расс?читыва|вы\s+хотите)",
    r"\bкакую\s+сумму\s+вы\s+",
    r"\bкакая\s+(?:у\s+(?:вас|тебя)\s+)?цена\b",
    r"\bкакая\s+цена\s+у\s+(?:вас|тебя)",
    r"\bсколько\s+(?:хотите|ждёте|ожидаете)\s+(?:за|получить)",
    r"\bкакая\s+у\s+(?:вас|тебя)\s+цена",
    r"\bкакую\s+(?:цену|сумму)\s+(?:хотите|ждёте|вы\s+ждёте)",
    r"\bсколько\s+вы\s+(?:хотите|ждёте)\s+за\b",
    # 🔴 ЗАПРЕТ: внутренние термины НЕ употребляем при общении с клиентом.
    # «дроп», «дроп-счёт», «дропа» — это наш сленг, клиент не должен слышать.
    # Только: «ваш счёт», «личный счёт», «ваш ЛК».
    r"\bдроп\b",
    r"\bдроп[- ]?счёт\w*",
    r"\bдроп[- ]?счет\w*",
    r"\bдроп\w*\s+(?:счёт|счет|акк|аккаунт)",
    r"\bваш\s+дроп\b",
    r"\bличный\s+дроп\b",
    # 🔴 Не спрашиваем у клиента «это твой личный X» — мы не выясняем, мы продаём.
    r"\bэто\s+(?:тво[йя]|ваш(?:а|е)?)\s+личн\w+\s+(?:втб|альфа|сбер|точк|озон|псб)",
]
_FORBIDDEN_RX = [
    re.compile(p, re.IGNORECASE | re.MULTILINE)
    for p in _FORBIDDEN_CLIENT_PATTERNS
]


# Прямые триггеры — если в сообщении есть, AI отвечает СРАЗУ
# (минуя классификатор релевантности).
_DIRECT_TRIGGER_PATTERNS = [
    # Явный вызов ассистента
    r"\bассистент\w*\b",
    r"@ассистент\w*\b",
    r"@assistant\b",
    # Любой вопрос
    r"\?",
    # Банки и счета
    r"\bальфа\b", r"\bтбанк\b", r"\bт[\s-]?банк\b", r"\bтинькоф\w*\b",
    r"\bозон\b", r"\bточк\w*\b", r"\bвтб\b", r"\bуралсиб\b",
    r"\bраиф\w*\b", r"\bрайф\w*\b", r"\bлоко\b", r"\bбкс\b",
    r"\bдело\b", r"\bубрир\b", r"\bсбер\w*\b",
    # Деньги / выплаты / сделки
    r"\bусдт\b", r"\busdt\b", r"\btrc[-_ ]?20\b", r"\btrx\b",
    r"\bгарант\w*\b", r"\bсделк\w*\b", r"\bвыплат\w*\b", r"\bоплат\w*\b",
    r"\bденьг\w+\b", r"\bкомпенсаци\w+\b", r"\bвозврат\w*\b",
    r"\bдеп(?:озит)?\w*\b",
    # Процессы
    r"\bотработ\w+\b", r"\bперевяз\w+\b", r"\bхолд\w*\b",
    r"\bблок\w*\b", r"\bбрак\w*\b", r"\bотказ\w*\b",
    r"\bстатус\w*\b", r"\bкогда\b", r"\bсколько\b",
    # Счета / ИП
    r"\bип\b", r"\bр/?с\b", r"\bсч[её]т\w*\b", r"\bдоговор\w*\b",
    r"\bпасспорт\w*\b", r"\bинн\b", r"\bокпо\b",
    # Команды
    r"^/clients\b", r"\b\+\s*партн[её]р\b",
    # Номер сделки (5-6 цифр с # или без) — клиент прислал, AI обязан ответить
    r"^#?\d{5,7}\s*$", r"\bсделка\s*#?\d{5,7}\b",
    # Слова-уточнения по deal_id
    r"\bзамена\b", r"\bактуальн\w*\s+номер", r"\bновый\s+номер",
    # «что дальше?» во всех формах
    r"\bчто\s+(?:дальше|делать|теперь)", r"\bдальше\s+что",
    # Сильные эмоциональные триггеры (жалоба/претензия)
    r"\bкид(?:ало|ок|нул)\b", r"\bжалоб\w*\b", r"\bпретенз\w*\b",
    r"\bобман\w*\b", r"\bсуд\w*\b", r"\bполиц\w*\b",
]
_DIRECT_TRIGGER_RX = [
    re.compile(p, re.IGNORECASE | re.MULTILINE)
    for p in _DIRECT_TRIGGER_PATTERNS
]


def _should_force_respond(text: str) -> bool:
    """True если в тексте есть прямой триггер — отвечаем без классификатора."""
    if not text:
        return False
    for rx in _DIRECT_TRIGGER_RX:
        if rx.search(text):
            return True
    return False


def _has_forbidden_topic(text: str) -> tuple:
    """Возвращает (True, matched_pattern) если в тексте есть запрещённая
    тема для клиентского чата. Иначе (False, "")."""
    if not text:
        return (False, "")
    stripped = text.strip()
    if len(stripped) < 6:
        return (False, "")
    for rx in _FORBIDDEN_RX:
        m = rx.search(stripped)
        if m:
            return (True, rx.pattern)
    return (False, "")


def _split_text(text: str, limit: int = 3900) -> list:
    """Split long text into chunks <= limit. Tries to break on newlines/spaces."""
    if len(text) <= limit:
        return [text]
    parts = []
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = text.rfind(" ", 0, limit)
        if cut < limit // 2:
            cut = limit
        parts.append(text[:cut])
        text = text[cut:].lstrip()
    if text:
        parts.append(text)
    return parts


def _entities_to_telethon(items: list) -> list:
    """Convert aiogram-style entity dicts -> Telethon entities. Unknown types skipped."""
    out = []
    for e in items or []:
        try:
            t = e.get("type"); off = int(e["offset"]); ln = int(e["length"])
        except Exception:
            continue
        if t == "custom_emoji":
            cid = e.get("custom_emoji_id") or e.get("customEmojiId")
            if cid:
                out.append(MessageEntityCustomEmoji(off, ln, int(cid)))
        elif t == "bold":
            out.append(MessageEntityBold(off, ln))
        elif t == "italic":
            out.append(MessageEntityItalic(off, ln))
        elif t == "underline":
            out.append(MessageEntityUnderline(off, ln))
        elif t == "strikethrough":
            out.append(MessageEntityStrike(off, ln))
        elif t == "code":
            out.append(MessageEntityCode(off, ln))
        elif t == "pre":
            lang = e.get("language") or ""
            out.append(MessageEntityPre(off, ln, lang))
        elif t in ("blockquote", "expandable_blockquote"):
            out.append(MessageEntityBlockquote(off, ln))
        elif t == "text_link":
            url = e.get("url") or ""
            if url:
                out.append(MessageEntityTextUrl(off, ln, url))
    return out


def _fmt_username(uname: Optional[str], fallback: str = "не указан") -> str:
    """Единый формат @username для постов/уведомлений.
    Гарантирует ровно один префикс @, обрезает пробелы.
    Если username пустой — возвращает fallback."""
    if not uname:
        return fallback
    clean = str(uname).strip().lstrip("@")
    if not clean:
        return fallback
    return f"@{clean}"


class UserbotService:
    def __init__(self):
        if config.STRING_SESSION:
            session = StringSession(config.STRING_SESSION)
        else:
            session = "userbot_session"
        self.client = TelegramClient(session, config.API_ID, config.API_HASH)
        self._me = None
        self._welcome_locks: dict[int, asyncio.Lock] = {}
        self._ai_locks: dict[int, asyncio.Lock] = {}
        self._last_worker_ts: dict[str, float] = {}
        # Время последнего сообщения от клиента в managed-чате (нормализованный
        # chat_id -> unix time). Используется в _handle_ai_message чтобы
        # понимать «отвечал ли worker на ПРЕДЫДУЩЕЕ сообщение клиента».
        self._last_client_msg_ts: dict[str, float] = {}
        self._chat_entity_cache: dict[int, object] = {}
        # Накладной режим редактирования заявки V2: chat_id -> (date_str, app_id).
        # Когда оператор пишет "редактировать заявку N" в Группе 2, мы запоминаем
        # пару, и при следующей валидной заявке от него — удаляем старую и применяем новую.
        self._editing_app: dict[int, tuple] = {}
        # SILENT MODE: chat_key -> unix_time_until.
        # После add_partner_to_crm AI замолкает на 30 минут — клиент общается
        # с @PrideCONTROLE_bot, AI не должен комментировать каждое нажатие
        # кнопки и каждый шаг анкеты. Снимается:
        #   - явным запросом помощи от клиента (?/помог/не получ/сколько/когда)
        #   - перевязкой ЛК ('🔗 Перевяз ЛК выполнен')
        #   - истечением TTL.
        self._ai_silent_until: dict[str, float] = {}
        # Pending запрос на удаление всех ЛК (команда «Ассистент удалить все ЛК»).
        # Структура: {chat_key: {msg_id, requested_by, approved_by: set, expires_at}}.
        # Удаление срабатывает когда approved_by содержит ID Тимона И ID любого админа.
        self._pending_delete_all_lk: dict[str, dict] = {}
        # Pending подтверждение карточки ЛК от клиента после /checkchatforLKCARD.
        # Структура: {chat_key: {bank, fio, price_usdt, payment_method, deal_id,
        #                       usdt_address, requested_at, expires_at}}.
        self._pending_lk_card_confirm: dict[str, dict] = {}

    async def create_work_chat(self, client_name: str, client_id: int = 0) -> dict:
        """Создаёт супергруппу-беседу под клиента, инвайтит работников,
        делает userbot админом, регистрирует чат в managed_chats и возвращает
        invite-ссылку.

        bot.py зовёт это после капчи в @PRIDE_INVITE_bot. Без этой функции
        invite-flow ломается (создание не доходит до ссылки).

        Returns: {chat_id, title, invite_link, statuses}
          где statuses — словарь {worker_username: "добавлен"/"не существует"/...}
        """
        title = config.CHAT_TITLE_TEMPLATE.format(client_name=client_name)
        about = config.CHAT_DESCRIPTION_TEMPLATE.format(client_name=client_name)

        # 1) Создаём супергруппу
        result = await self.client(CreateChannelRequest(
            title=title, about=about, megagroup=True,
        ))
        channel = result.chats[0]
        logger.info("Created group '%s' (id=%s) for client=%s", title, channel.id, client_id)

        # 2) Резолвим работников. Берём из storage.get_workers() (актуальный список,
        # настраиваемый через админку), с fallback на config.DEFAULT_WORKERS.
        statuses: dict[str, str] = {}
        users_to_invite = []
        try:
            workers_list = storage.get_workers() or []
        except Exception:
            workers_list = []
        if not workers_list:
            workers_list = list(getattr(config, "DEFAULT_WORKERS", []) or [])
        for username in workers_list:
            uname = (username or "").lstrip("@").strip()
            if not uname:
                continue
            try:
                entity = await self.client.get_entity(uname)
                users_to_invite.append((uname, entity))
                statuses[uname] = "найден"
            except UsernameNotOccupiedError:
                statuses[uname] = "не существует"
            except Exception as e:
                statuses[uname] = f"ошибка резолва: {e}"

        # 3) Добавляем по одному
        for uname, user in users_to_invite:
            try:
                await self.client(InviteToChannelRequest(channel, [user]))
                statuses[uname] = "добавлен"
            except UserPrivacyRestrictedError:
                statuses[uname] = "запрещены приглашения (Privacy)"
            except UserNotMutualContactError:
                statuses[uname] = "нет в контактах"
            except PeerFloodError:
                statuses[uname] = "флуд-лимит Telegram"
            except FloodWaitError as e:
                statuses[uname] = f"flood wait {e.seconds}s"
            except UserAlreadyParticipantError:
                statuses[uname] = "уже в чате"
            except Exception as e:
                statuses[uname] = f"ошибка: {e}"

        # 4) Делаем userbot админом (нужно для welcome / kick / pin / call)
        if getattr(config, "USERBOT_AS_ADMIN", True) and self._me:
            try:
                rights = ChatAdminRights(
                    change_info=True, post_messages=True, edit_messages=True,
                    delete_messages=True, ban_users=True, invite_users=True,
                    pin_messages=True, add_admins=False, anonymous=False,
                    manage_call=True,
                )
                await self.client(EditAdminRequest(
                    channel=channel, user_id=self._me,
                    admin_rights=rights, rank="Owner",
                ))
            except Exception as e:
                logger.warning("Could not grant admin rights to userbot: %s", e)

        # 5) Выдаём админку с rank работникам у которых worker_roles[uname].is_admin=True.
        # Также обогащаем statuses[uname] суффиксом «+ админка (Роль)»,
        # чтобы admin-нотификация в bot.py показывала роль рядом с никнеймом.
        try:
            for uname, user in users_to_invite:
                # Только если уже добавили в чат
                current_status = statuses.get(uname, "")
                if current_status not in ("добавлен", "уже в чате"):
                    continue
                # Берём роль из storage.worker_roles[uname.lower()] → {role, is_admin}
                role_data = {}
                try:
                    role_data = storage.get_worker_role(uname) or {}
                except Exception as e:
                    logger.warning("get_worker_role @%s failed: %s", uname, e)
                # Поддержим оба формата на всякий случай
                if isinstance(role_data, str):
                    role_str = role_data.strip()
                    is_admin = bool(role_str)
                else:
                    role_str = (role_data.get("role")
                                or role_data.get("rank") or "").strip()
                    is_admin = bool(role_data.get("is_admin"))
                if not is_admin:
                    # Работник без флага админки — оставляем как обычный участник.
                    if role_str:
                        statuses[uname] = f"{current_status} ({role_str}, без админки)"
                    continue
                try:
                    rights = ChatAdminRights(
                        change_info=False, post_messages=True, edit_messages=True,
                        delete_messages=True, ban_users=False, invite_users=True,
                        pin_messages=True, add_admins=False, anonymous=False,
                        manage_call=True,
                    )
                    await self.client(EditAdminRequest(
                        channel=channel, user_id=user,
                        admin_rights=rights, rank=role_str or "",
                    ))
                    role_suffix = f" ({role_str})" if role_str else ""
                    statuses[uname] = f"{current_status} + админка{role_suffix}"
                except Exception as e:
                    logger.warning("grant admin to @%s failed: %s", uname, e)
                    statuses[uname] = f"{current_status} (админка не выдана: {e})"
        except Exception as e:
            logger.warning("workers admin grant pass failed: %s", e)

        # 6) Invite-ссылка
        invite = await self.client(ExportChatInviteRequest(channel))
        invite_link = invite.link

        # 7) Регистрируем чат в managed_chats (нужно для welcome polling и AI)
        try:
            await storage.register_chat(
                chat_id=int(channel.id),
                client_id=int(client_id or 0),
                client_name=client_name,
            )
        except Exception as e:
            logger.warning("register_chat failed: %s", e)

        # 8) Эмитим событие на дашборд
        try:
            _e("chat-created", {
                "chat_id": int(channel.id),
                "client_id": int(client_id or 0),
                "client_name": client_name,
                "title": title,
                "workers_statuses": statuses,
            }, character="chat", severity="success")
        except Exception:
            pass

        return {
            "chat_id": int(channel.id),
            "title": title,
            "invite_link": invite_link,
            "statuses": statuses,
        }

    def _get_welcome_lock(self, chat_id) -> asyncio.Lock:
        """Lock на отправку welcome для конкретного чата.

        КРИТИЧНО: ключ нормализуем через _norm_chat_id, потому что Telethon
        отдаёт chat_id в разных форматах:
          - _handle_chat_action: event.chat_id — signed (-100xxx)
          - _watch_for_client_join: channel.id — unsigned (xxx)
        Без нормализации создаются ДВА разных lock'а и оба источника
        параллельно проходят первую проверку welcome_sent → дубль welcome.
        """
        from storage import _norm_chat_id
        key = _norm_chat_id(chat_id)
        if key not in self._welcome_locks:
            self._welcome_locks[key] = asyncio.Lock()
        return self._welcome_locks[key]

    async def start(self):
        await self.client.start(phone=config.USERBOT_PHONE)
        self._me = await self.client.get_me()
        logger.info(
            "Userbot started: %s (@%s, id=%s)",
            self._me.first_name, self._me.username, self._me.id,
        )
        # Запускаем воркер для команд из дашборда (опрос storage каждые 5 сек)
        try:
            asyncio.create_task(self._dashboard_command_worker())
            logger.info("dashboard_command_worker started")
        except Exception as e:
            logger.warning("dashboard_command_worker start failed: %s", e)
        # Подключаем outreach-юзерботы (если есть в storage)
        try:
            import outreach
            asyncio.create_task(outreach.manager.connect_all())
            logger.info("outreach manager connect_all scheduled")
        except Exception as e:
            logger.warning("outreach connect_all start failed: %s", e)
        for label, cid in (
            ("brain_chat", storage.get_brain_chat_id()),
            ("coord_chat", storage.get_coordination_chat_id()),
        ):
            if not cid:
                continue
            try:
                ent = await self.client.get_entity(cid)
                self._chat_entity_cache[int(cid)] = ent
                logger.info("%s entity primed: id=%s type=%s", label, cid, type(ent).__name__)
            except Exception as e:
                logger.warning(
                    "%s entity prime FAILED for id=%s: %s — sends в этот чат могут падать с InvalidPeer",
                    label, cid, e,
                )

        try:
            count = 0
            async for dialog in self.client.iter_dialogs(limit=30):
                ent = dialog.entity
                etype = type(ent).__name__
                title = getattr(ent, "title", None) or getattr(ent, "first_name", "") or "?"
                show_id = dialog.id
                logger.info(
                    "DIALOG[%d]: chat_id=%s title=%r type=%s",
                    count, show_id, title[:60], etype,
                )
                count += 1
            logger.info("DIALOG: listed %d chats", count)
        except Exception as e:
            logger.warning("dialog listing failed: %s", e)

        @self.client.on(events.ChatAction)
        async def _on_chat_action(event):
            try:
                await self._handle_chat_action(event)
            except Exception as e:
                logger.warning("ChatAction handler error: %s", e)

        @self.client.on(events.NewMessage(outgoing=True))
        async def _on_outgoing_for_support_cache(event):
            """Outgoing от PRIDE ASSISTANT — кэшируем для дашборда поддержки.
            Это нужно потому что _on_new_message слушает только incoming."""
            try:
                if not event or not event.message or not event.message.text:
                    return
                chat_id = event.chat_id
                chat_info = storage.get_chat_info(chat_id)
                if not chat_info:
                    return
                from storage import _norm_chat_id as _nrm
                cache = storage.state.setdefault("support_msg_cache", {})
                key = str(_nrm(chat_id))
                arr = cache.setdefault(key, [])
                msg_entry = {
                    "id": event.message.id,
                    "ts": time.time(),
                    "role": "assistant",
                    "author": "PRIDE ASSISTANT",
                    "sender_id": event.message.sender_id,
                    "text": event.message.text[:4000],
                }
                # Дедуп: если уже есть с таким id — пропускаем
                if not any(m.get("id") == msg_entry["id"] for m in arr[-10:]):
                    arr.append(msg_entry)
                    if len(arr) > 200:
                        del arr[: len(arr) - 200]
                    try:
                        from storage import _norm_chat_id as _nrm
                        _e("support-message", {
                            "chat_id": str(_nrm(chat_id)),
                            "raw_chat_id": chat_id,
                            "msg": msg_entry,
                        }, character="chat", severity="info")
                    except Exception:
                        pass
            except Exception as e:
                logger.warning("outgoing-support-cache fail: %s", e)

        @self.client.on(events.NewMessage(incoming=True))
        async def _on_new_message(event):
            try:
                # Ideas-чат — отдельная обработка (сохраняем сообщения как идеи)
                # ВАЖНО: chat_id у Telethon vs aiogram может отличаться по формату
                # (-100xxx vs xxx), поэтому нормализуем оба через storage._norm_chat_id.
                from storage import _norm_chat_id as _norm
                ideas_chat = storage.get_ideas_chat_id()
                if ideas_chat:
                    if _norm(event.chat_id) == _norm(ideas_chat):
                        await self._handle_ideas_message(event)
                        return
                await self._handle_ai_message(event)
            except Exception as e:
                logger.exception("AI message handler error: %s", e)

    async def stop(self):
        """Аккуратный shutdown — отключаем Telethon клиент.
        Вызывается из bot.py в finally при остановке бота."""
        try:
            if self.client and self.client.is_connected():
                await self.client.disconnect()
                logger.info("Userbot stopped")
        except Exception as e:
            logger.warning("userbot stop error: %s", e)

    async def _handle_ideas_message(self, event):
        """Сохраняет сообщения из ideas-чата в storage.ideas_inbox.
        ПРАВИЛА:
        • Сообщения от ботов (включая CRM, ассистент) — игнорируем
        • Команды (/...) — пропускаем
        • Записываем только если текст НАЧИНАЕТСЯ с «идея», «idea», «баг», «bug»
        Без префикса — игнорируем (это обычная переписка, не идея)."""
        try:
            text = (event.message.text or event.message.message or "").strip()
            if not text or text.startswith("/"):
                return
            # Skip botов (CRM-бот, ассистент, любые системные)
            try:
                sender = await event.get_sender()
                if getattr(sender, "bot", False):
                    return
                uname = getattr(sender, "username", None) or ""
                fname = getattr(sender, "first_name", "") or ""
                author = (f"@{uname}" if uname else fname) or "?"
            except Exception:
                author = "?"
            # Триггер по началу строки: «идея …» / «idea …» / «баг …» / «bug …»
            low = text.lower().lstrip()
            kind = None
            payload = None
            triggers_idea = ("идея ", "idea ", "идея:", "idea:", "идеа ")
            triggers_bug = ("баг ", "bug ", "баг:", "bug:")
            for t in triggers_idea:
                if low.startswith(t):
                    kind = "idea"
                    payload = text.lstrip()[len(t):].strip()
                    break
            if kind is None:
                for t in triggers_bug:
                    if low.startswith(t):
                        kind = "bug"
                        payload = text.lstrip()[len(t):].strip()
                        break
            # Особые случаи: просто «идея» или «баг» одним словом — без текста (пропускаем)
            if kind is None:
                stripped = low.rstrip(":.! ")
                if stripped in ("идея", "idea", "баг", "bug"):
                    return  # требуется текст после префикса
            if kind is None or not payload:
                return  # не идея — игнорируем переписку
            idea_id = await storage.add_idea(
                text=payload, author=author,
                chat_id=int(event.chat_id),
                msg_id=int(event.message.id),
                kind=kind,
            )
            logger.info(
                "idea saved id=%s kind=%s by=%s chat=%s len=%d",
                idea_id, kind, author, event.chat_id, len(payload),
            )
            try:
                emoji = "📝" if kind == "idea" else "🐛"
                await event.message.react(emoji)
            except Exception:
                try:
                    await event.message.reply(
                        f"{'🐛' if kind == 'bug' else '💡'} <b>Сохранено #{idea_id}</b>",
                        parse_mode="html",
                    )
                except Exception:
                    pass
        except Exception as e:
            logger.warning("ideas handler failed: %s", e)

    async def _handle_chat_action(self, event):
        try:
            uid = getattr(event, "user_id", None)
            logger.info(
                "ChatAction received: chat_id=%s user_id=%s joined=%s added=%s",
                event.chat_id, uid, event.user_joined, event.user_added,
            )
        except Exception:
            pass

        if not (event.user_joined or event.user_added):
            return
        info = storage.get_chat_info(event.chat_id)
        if not info:
            logger.info("ChatAction: chat=%s not in managed_chats — skip", event.chat_id)
            return
        if info.get("welcome_sent"):
            logger.info("ChatAction: chat=%s welcome already sent — skip", event.chat_id)
            return
        expected = info.get("client_id")
        if not expected:
            return

        joining_ids = set()
        if getattr(event, "user_id", None):
            joining_ids.add(event.user_id)
        try:
            users = await event.get_users()
            for u in users or []:
                joining_ids.add(getattr(u, "id", None))
        except Exception:
            pass

        if expected not in joining_ids:
            logger.info(
                "ChatAction: chat=%s expected=%s joining=%s — skip",
                event.chat_id, expected, joining_ids,
            )
            return

        await self._send_welcome(event.chat_id, expected, source="event")

    async def _send_welcome(self, chat_id, expected_client_id: int, source: str = "?"):
        lock = self._get_welcome_lock(chat_id)
        async with lock:
            info = storage.get_chat_info(chat_id)
            if not info or info.get("welcome_sent"):
                return False
            await asyncio.sleep(1)
            info = storage.get_chat_info(chat_id)
            if not info or info.get("welcome_sent"):
                return False

            welcome = storage.get_welcome()
            entities_raw = storage.get_welcome_entities()
            try:
                if entities_raw:
                    ents = _entities_to_telethon(entities_raw)
                    await self.client.send_message(chat_id, welcome, formatting_entities=ents)
                else:
                    for chunk in _split_text(welcome, 3900):
                        await self.client.send_message(chat_id, chunk)
                        await asyncio.sleep(0.3)
                await storage.mark_welcome_sent(chat_id)
                logger.info(
                    "Welcome sent (source=%s, entities=%d, len=%d) to chat=%s for client=%s",
                    source, len(entities_raw), len(welcome), chat_id, expected_client_id,
                )
                return True
            except FloodWaitError as e:
                logger.warning(
                    "Welcome send flood wait %ds (source=%s, chat=%s) — retrying after wait",
                    e.seconds, source, chat_id,
                )
                await asyncio.sleep(e.seconds + 1)
                try:
                    await self.client.send_message(chat_id, welcome[:3900])
                    await storage.mark_welcome_sent(chat_id)
                    logger.info("Welcome sent after flood wait (chat=%s)", chat_id)
                    return True
                except Exception as retry_e:
                    logger.warning("Welcome retry failed (chat=%s): %s", chat_id, retry_e)
                    return False
            except Exception as e:
                logger.warning(
                    "Welcome send failed (source=%s, chat=%s): %s",
                    source, chat_id, e,
                )
                return False

    async def _watch_for_client_join(self, channel, client_id: int, timeout_sec: int = 600):
        deadline = asyncio.get_event_loop().time() + timeout_sec
        try:
            while asyncio.get_event_loop().time() < deadline:
                info = storage.get_chat_info(channel.id)
                if info and info.get("welcome_sent"):
                    logger.info("watch chat=%s: welcome already sent, exiting watcher", channel.id)
                    return
                try:
                    await self.client(GetParticipantRequest(
                        channel=channel,
                        participant=PeerUser(client_id),
                    ))
                    logger.info("watch chat=%s: client %s joined, sending welcome", channel.id, client_id)
                    await self._send_welcome(channel.id, client_id, source="poll")
                    # Помечаем пользователя как «вошёл в work-чат» для воронки
                    try:
                        await storage.mark_user_entered_work_chat(client_id)
                        await storage.bump_funnel("chats_active")
                    except Exception:
                        pass
                    return
                except UserNotParticipantError:
                    pass
                except FloodWaitError as e:
                    logger.warning("watch chat=%s flood wait %ss", channel.id, e.seconds)
                    await asyncio.sleep(e.seconds + 1)
                    continue
                except Exception as e:
                    logger.warning("watch chat=%s poll error: %s", channel.id, e)
                await asyncio.sleep(3)
            logger.info("watch chat=%s: timeout (%ss), client never joined", channel.id, timeout_sec)
        except Exception as e:
            logger.warning("watch chat=%s: unexpected error: %s", channel.id, e)

    async def _resolve_chat_target(self, chat_id):
        try:
            cid_int = int(chat_id)
        except Exception:
            return chat_id
        if cid_int in self._chat_entity_cache:
            return self._chat_entity_cache[cid_int]
        # Если ID положительный — это либо user, либо нормализованный channel/megagroup
        # без -100 префикса (что хранится в storage после _norm_chat_id).
        # Сначала пробуем как PeerChannel (для группового чата) — это покрывает
        # 99% наших чатов: рабочие беседы, ЛК-группа, бухгалтерия, сделки.
        # Если падает — fallback на обычный get_entity (вдруг это user).
        candidates = []
        if cid_int > 0:
            try:
                from telethon.tl.types import PeerChannel
                candidates.append(PeerChannel(cid_int))
            except Exception:
                pass
        candidates.append(cid_int)
        last_err = None
        for cand in candidates:
            try:
                ent = await self.client.get_entity(cand)
                self._chat_entity_cache[cid_int] = ent
                return ent
            except Exception as e:
                last_err = e
                continue
        logger.warning("resolve_chat_target failed for %s: %s", cid_int, last_err)
        return cid_int

    # === AI brain handlers ===

    async def _handle_ai_message(self, event):
        chat_id = event.chat_id
        bid = storage.get_brain_chat_id()

        from storage import _norm_chat_id

        try:
            sender_id_dbg = event.sender_id
        except Exception:
            sender_id_dbg = "?"
        logger.info(
            "userbot event: chat_id=%s norm=%s brain_id=%s norm_brain=%s sender=%s",
            chat_id, _norm_chat_id(chat_id), bid,
            (_norm_chat_id(bid) if bid else "—"), sender_id_dbg,
        )

        if bid and _norm_chat_id(chat_id) == _norm_chat_id(bid):
            await self._handle_brain_chat_writeback(event)
            return

        # Группа 1 «Личные кабинеты» — анкеты ЛК + БРАК/БЛОК
        lk_id = storage.get_lk_group_id()
        if lk_id and _norm_chat_id(chat_id) == _norm_chat_id(lk_id):
            await self._handle_lk_group_message(event)
            return

        # Группа 2 «Бухгалтерия» — заявки v2
        accounting_id = storage.get_accounting_group_id()
        if accounting_id and _norm_chat_id(chat_id) == _norm_chat_id(accounting_id):
            await self._handle_accounting_v2_message(event)
            return
        chat_info = storage.get_chat_info(chat_id)

        # 🔴 WELCOME FALLBACK: если managed_chat есть, клиент пишет,
        # но welcome_sent=False — значит ChatAction event пропустили
        # (бот был в downtime / race). Шлём welcome ОДИН раз сейчас.
        try:
            if (chat_info and not chat_info.get("welcome_sent")
                    and event.message and event.message.sender_id == chat_info.get("client_id")):
                client_id_w = chat_info.get("client_id") or 0
                if client_id_w:
                    asyncio.create_task(
                        self._send_welcome(chat_id, client_id_w, source="firstmsg-fallback")
                    )
        except Exception as e:
            logger.warning("welcome-fallback err: %s", e)

        # Bump last-message timestamp в managed_chats (для сортировки inbox)
        try:
            if chat_info and event.message:
                await storage.bump_last_message_ts(chat_id)
        except Exception:
            pass

        # Кешируем сообщение в support_msg_cache для дашборда (helpdesk).
        # Храним последние 200 сообщений на чат.
        try:
            if not chat_info:
                logger.debug(
                    "[support_cache] SKIP: chat_info is None for chat_id=%s "
                    "(не managed_chat)", chat_id,
                )
            elif not (event.message and event.message.text):
                logger.debug(
                    "[support_cache] SKIP: no text in event for chat_id=%s",
                    chat_id,
                )
            if chat_info and event.message and event.message.text:
                from storage import _norm_chat_id as _nrm
                cache = storage.state.setdefault("support_msg_cache", {})
                key = str(_nrm(chat_id))
                arr = cache.setdefault(key, [])
                sender_id_x = event.message.sender_id
                client_id_x = chat_info.get("client_id") or 0
                logger.info(
                    "[support_cache] WRITE chat=%s key=%s sender=%s client=%s text=%r",
                    chat_id, key, sender_id_x, client_id_x,
                    (event.message.text or "")[:60],
                )
                if self._me and sender_id_x == self._me.id:
                    author_role = "assistant"
                    author_name = "PRIDE ASSISTANT"
                elif sender_id_x == client_id_x:
                    author_role = "client"
                    author_name = chat_info.get("client_name") or "Клиент"
                else:
                    author_role = "worker"
                    try:
                        s = await event.get_sender()
                        author_name = (
                            (getattr(s, "first_name", None) or "")
                            + (" " + getattr(s, "last_name", "") if getattr(s, "last_name", None) else "")
                        ).strip() or "Сотрудник"
                    except Exception:
                        author_name = "Сотрудник"
                msg_entry = {
                    "id": event.message.id,
                    "ts": time.time(),
                    "role": author_role,
                    "author": author_name,
                    "sender_id": sender_id_x,
                    "text": event.message.text[:4000],
                }
                arr.append(msg_entry)
                if len(arr) > 200:
                    del arr[: len(arr) - 200]
                # SSE event — UI открытого чата подхватит без polling.
                # chat_id передаём СТРОКОЙ нормализованной — иначе SSE даёт raw
                # -1003998507288, а inbox даёт "3998507288" → === не совпадёт.
                try:
                    norm_cid = str(_nrm(chat_id))
                    logger.info(
                        "[support_sse] EMIT support-message norm_cid=%s raw=%s "
                        "msg_id=%s role=%s text=%r",
                        norm_cid, chat_id, msg_entry.get("id"),
                        msg_entry.get("role"),
                        (msg_entry.get("text") or "")[:60],
                    )
                    _e("support-message", {
                        "chat_id": norm_cid,
                        "raw_chat_id": chat_id,
                        "msg": msg_entry,
                    }, character="chat", severity="info")
                    _e("support-inbox-bump", {
                        "chat_id": norm_cid,
                        "client_id": client_id_x,
                        "role": author_role,
                    }, character="chat", severity="info")
                except Exception as e_sse:
                    logger.warning("[support_sse] emit failed: %s", e_sse)
        except Exception as e:
            logger.warning("support_msg_cache update fail: %s", e)

        # 📞 HELPDESK TRIGGER: клиент пишет про оператора / менеджера / человека —
        # переводим чат в inbox менеджера + замолкаем AI.
        try:
            if (chat_info and event.message and event.message.text
                    and event.message.sender_id == chat_info.get("client_id")):
                low_text = event.message.text.lower().strip()
                # Гибкий regex: слово «оператор/менеджер/человек/админ/owner»
                # в любой словоформе. Лимит 200 символов чтобы не триггерить
                # из длинных рассуждений клиента.
                operator_re = re.compile(
                    r"\b(оператор\w*|менеджер\w*|"
                    r"саппорт\w*|support|"
                    r"жив\w+\s+человек\w*|реальн\w+\s+человек\w*|"
                    r"с\s+человеком|к\s+человеку|"
                    r"позови\s+\w+|позвать\s+\w+|"
                    r"ассистент[,.\s]+позови|ассистент[,.\s]+позвать)\b",
                    re.IGNORECASE,
                )
                triggered = (
                    len(low_text) <= 200
                    and bool(operator_re.search(low_text))
                )
                if triggered:
                    sup = chat_info.get("support") or {}
                    if sup.get("status") not in (
                        "operator_requested", "in_progress", "awaiting_department",
                    ):
                        # Ставим status=awaiting_department — ждём выбор клиента
                        await storage.set_support_state(
                            chat_id, status="awaiting_department",
                            department="", opened_at=time.time(),
                            assigned_to=0,
                            trigger_text=(event.message.text or "")[:160],
                        )
                        # Уведомление клиенту с выбором подразделения
                        try:
                            target = await self._resolve_chat_target(chat_id)
                            sent_notice = await self.client.send_message(
                                target,
                                "📞 <b>На какое подразделение вас перевести?</b>\n\n"
                                "Ответьте цифрой или словом:\n\n"
                                "<b>1</b> — 👤 <b>Менеджер</b>\n"
                                "<i>общие вопросы, цены, условия сделки</i>\n\n"
                                "<b>2</b> — ⚙️ <b>System</b>\n"
                                "<i>перевязка и установка ЛК на железо</i>\n\n"
                                "<b>3</b> — 💰 <b>Бухгалтерия</b>\n"
                                "<i>выплаты, предоплаты, финансовые вопросы</i>",
                                parse_mode="html",
                            )
                            # Кэшируем outgoing-уведомление чтобы появилось в дашборде
                            try:
                                from storage import _norm_chat_id as _nrm
                                cache_dict = storage.state.setdefault("support_msg_cache", {})
                                arr_n = cache_dict.setdefault(str(_nrm(chat_id)), [])
                                arr_n.append({
                                    "id": getattr(sent_notice, "id", int(time.time()*1000)),
                                    "ts": time.time(),
                                    "role": "assistant",
                                    "author": "PRIDE ASSISTANT",
                                    "sender_id": (self._me.id if self._me else 0),
                                    "text": "📞 Подключаю оператора, ожидайте.",
                                })
                                if len(arr_n) > 200:
                                    del arr_n[: len(arr_n) - 200]
                                await storage._save_unlocked()
                                _e("support-message", {
                                    "chat_id": str(_nrm(chat_id)),
                                    "raw_chat_id": chat_id,
                                    "msg": arr_n[-1],
                                }, character="chat", severity="info")
                            except Exception as ec:
                                logger.warning("cache trigger notice fail: %s", ec)
                        except Exception as e:
                            logger.warning("trigger notice send fail: %s", e)
                        _e("support-operator-requested", {
                            "chat_id": chat_id,
                            "client_username": chat_info.get("client_username") or "",
                            "client_name": chat_info.get("client_name") or "",
                            "text": event.message.text[:120],
                        }, character="chat", severity="warning")
                        logger.info(
                            "[helpdesk] operator requested in chat=%s by client=%s text=%r",
                            chat_id, chat_info.get("client_id"),
                            event.message.text[:80],
                        )
                    return
        except Exception as e:
            logger.warning("helpdesk trigger handler error: %s", e)


        # 🔴 AUTO-TRIGGER: после перевязки CRM-бот пишет «✅ Перевязка ЛК ... успешно
        # выполнена» / «ЛК ... перевязан и в работе» + «Метод оплаты: уточняется
        # у клиента». Userbot ловит это сообщение и сам отправляет клиенту
        # запрос на метод оплаты (раньше «Ассистент уточнит» — но AI не
        # триггерился потому что сообщение от бота, не от клиента).
        try:
            if chat_info and event.message and event.message.text:
                txt_low = event.message.text.lower()
                # Расширенный матч: любое из этих сочетаний триггерит auto-ask
                perevyaz_match = (
                    ("перевязка лк" in txt_low and "успешно выполнена" in txt_low)
                    or ("перевязан и в работе" in txt_low and "уточняется у клиента" in txt_low)
                    or ("перевязка" in txt_low and "успешно" in txt_low)
                    or ("карточка" in txt_low and "перевязан" in txt_low)
                )
                if perevyaz_match:
                    logger.info(
                        "AI: detected CRM perevyaz-success in chat=%s text=%r — "
                        "auto-asking payment method",
                        chat_id, txt_low[:100],
                    )
                    await self._auto_ask_payment_method_after_perevyaz(event, chat_id, chat_info)
        except Exception as e:
            logger.warning("auto-ask payment method handler error: %s", e)

        # Команды takeover/forget работают в ЛЮБОЙ группе, даже если её
        # ещё нет в managed_chats — это и есть смысл takeover'а.
        if not event.message:
            return
        # Если в сообщении нет ни текста ни фото — игнорируем (стикеры, документы
        # пока не поддерживаются полноценно; фото идёт в AI через Claude Vision).
        has_photo = bool(getattr(event.message, "photo", None))
        if not (event.message.text or "").strip() and not has_photo:
            return
        try:
            if await self._maybe_handle_takeover_command(event, chat_id, chat_info):
                return
        except Exception as e:
            logger.warning("takeover handler error: %s", e)
        if not chat_info:
            return

        # Снимаем silent mode если пришёл сигнал готовности от CRM-бота:
        # «Отдать в работу» / «отправлено на обработку» / «принят в работу».
        # Это значит клиент завершил заполнение анкеты — AI снова может писать.
        try:
            await self._maybe_release_silent_on_crm_ready(event, chat_id)
        except Exception as e:
            logger.warning("silent release check failed: %s", e)

        if await self._maybe_handle_perevyaz(event, chat_info):
            return

        sender_id = event.sender_id
        if self._me and sender_id == self._me.id:
            return

        try:
            sender = await event.get_sender()
        except Exception:
            sender = None
        sender_username = (getattr(sender, "username", "") or "").lower()

        # Параллельный триггер: @pride_sys01/@pride_sys02 пишет в work-чат
        # «Иванов — Альфа — перевяз успешен» → создаём карточку как от CRM-бота.
        try:
            if await self._maybe_handle_perevyaz_by_worker(
                event, chat_info, sender_username,
            ):
                return
        except Exception as e:
            logger.warning("perevyaz-by-worker dispatch failed: %s", e)
        workers_lc = {w.lower() for w in storage.get_workers()}
        is_worker = (
            (sender_username and sender_username in workers_lc)
            or (sender_id in storage.get_admins())
        )

        from storage import _norm_chat_id  # noqa: F811
        chat_key = _norm_chat_id(chat_id)

        if is_worker:
            self._last_worker_ts[chat_key] = time.time()
            # Менеджер-стата: сообщение работника в work-чате
            try:
                if sender_username:
                    await storage.bump_manager(sender_username, "messages")
            except Exception:
                pass
            # Сбрасываем cooldown тега — менеджер пришёл и ответил
            try:
                if sender_username:
                    await storage.record_specialist_reply(chat_id, sender_username)
            except Exception as e:
                logger.debug("record_specialist_reply failed: %s", e)
            # Команды админу: 'Ассистент добавь @x' / 'Ассистент выдай админку @x'.
            # Работают только в managed_chats (рабочие беседы клиентов).
            try:
                msg_text = ((event.message and event.message.text) or "")
                if msg_text and await self._maybe_handle_ai_admin_command(
                    event, msg_text, chat_id,
                ):
                    return
            except Exception as e:
                logger.warning("ai admin cmd handler error: %s", e)
            logger.info("AI: worker activity in chat=%s by @%s", chat_id, sender_username)
            return

        if not storage.is_ai_enabled():
            return
        if not config.ANTHROPIC_API_KEY:
            return

        client_id = chat_info.get("client_id")
        if client_id and sender_id != client_id:
            return

        # AUTO-DETECT метода оплаты из сообщения клиента: страховка на случай
        # если AI забыл вызвать set_payment_method. Срабатывает если метод
        # ещё не задан ИЛИ клиент явно требует переключение на GUARANTOR_BEFORE.
        try:
            msg_text_lc = ((event.message and event.message.text) or "").lower()
            # Маркеры явного требования гаранта ДО отработки — могут переопределить
            # уже установленный метод (клиент имеет право изменить решение).
            demand_before = any(m in msg_text_lc for m in (
                "хочу гарант сейчас", "хочу гарант прям", "пополните прям",
                "пополните сейчас", "до отработ", "иначе ресн",
                "сразу гарант", "гарант сразу", "гарант до",
            ))
            if (not chat_info.get("payment_method") or demand_before) and msg_text_lc:
                if await self._maybe_autodetect_payment_method(
                    event, chat_id, msg_text_lc,
                ):
                    # Если override триггер сработал — синхронизируем метод
                    # на активных карточках ЛК этого work_chat (если они есть).
                    if demand_before:
                        try:
                            from storage import _norm_chat_id as _norm
                            wc_norm = _norm(chat_id)
                            for cid, c in (storage.list_lk_cards() or {}).items():
                                if _norm(c.get("work_chat_id") or 0) != wc_norm:
                                    continue
                                if (c.get("status") or "").upper() in ("ЗАВЕРШЁН", "ЗАВЕРШЕН", "БРАК", "БЛОК"):
                                    continue
                                if (c.get("payment_method") or "").upper() == "GUARANTOR_BEFORE":
                                    continue
                                await storage.update_lk_card(
                                    cid, payment_method="GUARANTOR_BEFORE",
                                    _allow_payment_method_change=True,
                                )
                                logger.info(
                                    "AUTO override payment_method → GUARANTOR_BEFORE for card=%s "
                                    "(client demand)", cid,
                                )
                        except Exception as e:
                            logger.warning("override payment_method on demand: %s", e)
        except Exception as e:
            logger.warning("auto-detect payment method failed: %s", e)

        # PENDING LK CONFIRM: если ждём подтверждения от клиента после
        # /checkchatforLKCARD — обрабатываем «да»/«нет» здесь, минуя AI.
        try:
            from storage import _norm_chat_id as _norm_x
            chat_key_x = _norm_x(chat_id)
            pending_card = self._pending_lk_card_confirm.get(chat_key_x)
            if pending_card and pending_card.get("expires_at", 0) > time.time():
                msg_text_pc = ((event.message and event.message.text) or "").lower().strip()
                if msg_text_pc:
                    affirm = any(w in msg_text_pc for w in (
                        "да", "верно", "подтверждаю", "правильно", "ага", "ок ",
                        "ок.", "ок,", "ок!", "ok", "yes", "👍", "✅",
                    )) and len(msg_text_pc) < 60
                    if affirm:
                        # Создаём карточку
                        pc = self._pending_lk_card_confirm.pop(chat_key_x)
                        try:
                            card_id = await storage.add_lk_card(
                                bank=pc.get("bank") or "",
                                fio=pc.get("fio") or "",
                                price_usdt=float(pc.get("price_usdt") or 0),
                                payment_method=pc.get("payment_method") or "",
                                deal_id=pc.get("deal_id") or "",
                                usdt_address=pc.get("usdt_address") or "",
                                status="В_РАБОТЕ",
                                supplier=pc.get("client_username") or "",
                                client_id=int(pc.get("client_id") or 0),
                                client_username=pc.get("client_username") or "",
                                work_chat_id=int(chat_id or 0),
                                created_by="checkchatforlkcard_confirmed",
                            )
                            try:
                                await self._refresh_lk_card_post(card_id)
                            except Exception:
                                pass
                            target = await self._resolve_chat_target(chat_id)
                            await self.client.send_message(
                                target,
                                f"✅ Спасибо! Карточка <code>#{card_id}</code> создана. "
                                f"Передаём операционистам — выплата по факту отработки "
                                f"(или сразу — если выбран гарант до).",
                                parse_mode="html",
                            )
                            _e("lk-card-confirmed-by-client", {
                                "card_id": card_id, "chat_id": chat_id,
                            }, character="lk", severity="success")
                        except Exception as e:
                            logger.warning("confirmed card create fail: %s", e)
                            try:
                                target = await self._resolve_chat_target(chat_id)
                                await self.client.send_message(
                                    target,
                                    f"⚠️ Подтвердили, но создание карточки упало: {e}. "
                                    f"Передал админам.",
                                )
                            except Exception:
                                pass
                        return
                    # «нет» / «исправь ...» — НЕ создаём, оставляем pending,
                    # дальше AI обработает текст и предложит правильные значения
        except Exception as e:
            logger.warning("pending lk confirm handler error: %s", e)

        # AUTO-DETECT номера сделки от клиента для GUARANTOR_AFTER_WORK карточки.
        # Страховка на случай если AI забудет вызвать record_deal. Срабатывает
        # когда клиент шлёт чистое число 5-7 цифр (с # или без) и в этом
        # work_chat есть ОТРАБОТАН-карточка с методом GUARANTOR_AFTER_WORK.
        try:
            msg_text_raw = ((event.message and event.message.text) or "").strip()
            if msg_text_raw and await self._maybe_autodetect_deal_id(
                event, chat_id, msg_text_raw,
            ):
                # deal_id применён, карточка → ПОПОЛНИТЬ_И_ОТПУСТИТЬ, в fund_release,
                # клиенту отправлено подтверждение. AI больше отвечать не нужно.
                return
        except Exception as e:
            logger.warning("auto-detect deal_id failed: %s", e)

        # 📞 AWAITING DEPARTMENT: клиент должен выбрать 1/2/3 подразделение
        try:
            sup_aw = (chat_info or {}).get("support") or {}
            if sup_aw.get("status") == "awaiting_department":
                low_a = (event.message.text or "").lower().strip()
                if event.message.sender_id == (chat_info or {}).get("client_id"):
                    chosen_dept = None
                    chosen_label = None
                    if low_a in ("1", "1️⃣", "один") or re.search(r"\bменеджер", low_a):
                        chosen_dept = "managers"
                        chosen_label = "👤 Менеджеры"
                    elif (low_a in ("2", "2️⃣", "два")
                          or re.search(r"\b(system|систем|перевяз|установ|желез|sus|сус)", low_a)):
                        chosen_dept = "system"
                        chosen_label = "⚙️ System"
                    elif (low_a in ("3", "3️⃣", "три")
                          or re.search(r"\b(бухгалт|выплат|предоплат|финанс|деньг)", low_a)):
                        chosen_dept = "accounting"
                        chosen_label = "💰 Бухгалтерия"
                    if chosen_dept:
                        await storage.set_support_state(
                            chat_id,
                            status="operator_requested",
                            department=chosen_dept,
                            assigned_to=0,
                        )
                        # Подтверждение клиенту
                        try:
                            target = await self._resolve_chat_target(chat_id)
                            confirm_text = (
                                f"✅ Запрос отправлен в <b>{chosen_label}</b>.\n"
                                f"Оператор подключится в ближайшее время."
                            )
                            sent_c = await self.client.send_message(
                                target, confirm_text, parse_mode="html",
                            )
                            from storage import _norm_chat_id as _nrm
                            cache_dict = storage.state.setdefault("support_msg_cache", {})
                            arr_c = cache_dict.setdefault(str(_nrm(chat_id)), [])
                            mc = {
                                "id": getattr(sent_c, "id", int(time.time()*1000)),
                                "ts": time.time(),
                                "role": "assistant",
                                "author": "PRIDE ASSISTANT",
                                "sender_id": (self._me.id if self._me else 0),
                                "text": f"✅ Запрос отправлен в {chosen_label}.",
                            }
                            arr_c.append(mc)
                            if len(arr_c) > 200:
                                del arr_c[: len(arr_c) - 200]
                            await storage._save_unlocked()
                            _e("support-message", {
                                "chat_id": str(_nrm(chat_id)),
                                "raw_chat_id": chat_id,
                                "msg": mc,
                            }, character="chat", severity="info")
                            _e("support-operator-requested", {
                                "chat_id": chat_id,
                                "department": chosen_dept,
                                "client_username": (chat_info or {}).get("client_username") or "",
                                "client_name": (chat_info or {}).get("client_name") or "",
                            }, character="chat", severity="warning")
                        except Exception as ec:
                            logger.warning("dept confirm send fail: %s", ec)
                        logger.info(
                            "[helpdesk] dept chosen=%s by client in chat=%s",
                            chosen_dept, chat_id,
                        )
                        return
                    # Если клиент написал что-то невалидное — повторим prompt одно сообщение
                    try:
                        target = await self._resolve_chat_target(chat_id)
                        await self.client.send_message(
                            target,
                            "⚠️ Не понял выбор. Напишите <b>1</b>, <b>2</b> или <b>3</b>:\n"
                            "1 — Менеджер · 2 — System · 3 — Бухгалтерия",
                            parse_mode="html",
                        )
                    except Exception:
                        pass
                    return
        except Exception as e:
            logger.warning("dept-choice handler error: %s", e)

        # 📞 HARD SILENCE: если чат в support-режиме — AI молчит, НО с TTL:
        #   awaiting_department: 15 мин (если клиент не выбрал 1/2/3)
        #   operator_requested:  30 мин (если менеджер не нажал 'Взять')
        #   in_progress:         4 ч  (если без активности)
        # После TTL: auto-close + AI снова отвечает.
        try:
            sup_state = (chat_info or {}).get("support") or {}
            sup_status = sup_state.get("status") or ""
            if sup_status in ("operator_requested", "in_progress", "awaiting_department"):
                opened_at = float(sup_state.get("opened_at") or 0)
                age_min = (time.time() - opened_at) / 60 if opened_at else 9999
                ttl_min = {
                    "awaiting_department": 15,
                    "operator_requested": 30,
                    "in_progress": 240,
                }.get(sup_status, 60)
                if age_min > ttl_min:
                    logger.warning(
                        "AI: support TTL expired chat=%s status=%s age=%.0fmin (TTL=%dmin) — auto-resetting",
                        chat_id, sup_status, age_min, ttl_min,
                    )
                    try:
                        await storage.set_support_state(
                            chat_id, status="closed",
                            closed_at=time.time(),
                            auto_closed_reason=f"ttl_expired_{sup_status}",
                        )
                    except Exception:
                        pass
                    # НЕ молчим — пускаем дальше к AI
                else:
                    logger.info(
                        "AI: HARD SILENCE chat=%s status=%s age=%.0fmin/ttl=%dmin",
                        chat_id, sup_status, age_min, ttl_min,
                    )
                    self._last_client_msg_ts[chat_key] = time.time()
                    return
        except Exception as e:
            logger.warning("HARD SILENCE TTL check err chat=%s: %s", chat_id, e)

        # SILENT MODE: после add_partner_to_crm AI молчит 30 минут пока
        # клиент заполняет анкету в @PrideCONTROLE_bot. Снимается явным
        # запросом помощи или истечением TTL.
        silent_until = self._ai_silent_until.get(chat_key, 0)
        if silent_until and time.time() < silent_until:
            text_lc = ((event.message.text or "") if event.message else "").lower()
            HELP_MARKERS = (
                "?", "помог", "помощ", "не получ", "не работ", "не пойм",
                "не понимаю", "сколько", "когда", "куда", "что дальше",
                "застр", "ошибк", "не приходит", "не вижу",
                "привет", "здравств", "есть кто",
                # Короткие подтверждения / возражения — клиент продолжает диалог
                "да", "нет", "ок", "хорошо", "норм", "согласен", "согласна",
                "подходит", "идет", "идёт", "договорились", "понятно",
                "понял", "ясно", "good", "ok", "yes", "no",
                # Возражения по цене / условиям — AI обязательно ОТВЕЧАЕТ
                "цена", "цене", "дорого", "дешев", "торг", "скид",
                "метод", "оплат", "выплат", "перевод", "карт", "юсдт", "usdt",
                "гарант", "континентал", "continental",
                # Любая короткая реплика < 4 слов — снимаем silence
            )
            # Дополнительный матч: короткие сообщения (1-3 слова) считаем как
            # "клиент пытается продолжить разговор" → снимаем silence.
            word_count = len(text_lc.split())
            looks_like_help = (
                any(m in text_lc for m in HELP_MARKERS)
                or word_count <= 3  # короткое сообщение почти всегда требует ответа
            )
            if not looks_like_help:
                logger.info(
                    "AI: silent mode active for chat=%s (CRM-флоу), пропускаю клиентское сообщение len=%d",
                    chat_id, len(text_lc),
                )
                # Обновим штамп клиента — но не отвечаем
                self._last_client_msg_ts[chat_key] = time.time()
                return
            # Клиент просит помощь → снимаем silent
            logger.info(
                "AI: silent mode lifted for chat=%s — клиент задал вопрос/помощь",
                chat_id,
            )
            self._ai_silent_until.pop(chat_key, None)

        # Авто-фиксация @username клиента: если в managed_chats нет username
        # или он устарел — подтягиваем из event.sender и обновляем индекс.
        # Это «лечит» legacy-беседы, созданные до сохранения username.
        if sender_username:
            stored_uname = (chat_info.get("client_username") or "").lower().strip()
            if stored_uname != sender_username:
                try:
                    await storage.update_client_username(chat_id, sender_username)
                    logger.info(
                        "managed_chat=%s: client_username updated → @%s",
                        chat_id, sender_username,
                    )
                except Exception as e:
                    logger.warning("auto-update username failed for chat=%s: %s", chat_id, e)

        idle_min = max(0, storage.get_client_idle_minutes())
        idle_sec = idle_min * 60
        # Обновляем штамп последнего сообщения клиента.
        self._last_client_msg_ts[chat_key] = time.time()

        # Логика «дать сотруднику ответить первым»:
        # Если worker когда-то писал в чате и его последнее сообщение было
        # недавно (< idle_sec назад) — AI ждёт ОСТАТОК паузы. Если за это
        # время worker ответит клиенту — AI молчит. Если нет — AI отвечает.
        last_worker_ts = self._last_worker_ts.get(chat_key, 0)
        if idle_sec > 0 and last_worker_ts > 0:
            since_worker = time.time() - last_worker_ts
            if since_worker < idle_sec:
                delay = max(1, int(idle_sec - since_worker))
                logger.info(
                    "AI: chat=%s — worker недавно был активен (%ds назад), "
                    "ждём паузу %ds перед ответом",
                    chat_id, int(since_worker), delay,
                )
                # Берём lock сразу — чтобы другие сообщения клиента в эту
                # паузу не запустили параллельную обработку.
                lock = self._ai_locks.setdefault(chat_key, asyncio.Lock())
                if lock.locked():
                    logger.info("AI: chat=%s already processing — skip", chat_id)
                    return
                async with lock:
                    await asyncio.sleep(delay)
                    new_last_worker = self._last_worker_ts.get(chat_key, 0)
                    if new_last_worker > last_worker_ts:
                        await storage.bump_ai_stats(skipped_worker_active=1)
                        logger.info(
                            "AI: chat=%s — worker написал во время паузы, "
                            "AI не вмешивается",
                            chat_id,
                        )
                        return
                    await self._do_ai_reply(event, chat_info, idle_sec, chat_key)
                return

        # Worker не активен (никогда не писал, либо давно) — отвечаем сразу.
        lock = self._ai_locks.setdefault(chat_key, asyncio.Lock())
        if lock.locked():
            logger.info("AI: chat=%s already processing — skip", chat_id)
            return
        async with lock:
            await self._do_ai_reply(event, chat_info, idle_sec, chat_key)

    async def _handle_learn_command(self, event, text: str):
        """Bulk-обучение из истории чатов. /learn [chat_id] [limit=N]."""
        cmd = learn.parse_learn_command(text)
        chat_id = cmd["chat_id"]
        limit = cmd["limit"]

        if not config.ANTHROPIC_API_KEY:
            await event.reply("⚠️ ANTHROPIC_API_KEY не задан — обучение невозможно.")
            return
        if not config.GITHUB_TOKEN:
            await event.reply("⚠️ GITHUB_TOKEN не задан — нечего сохранять.")
            return

        if chat_id:
            await event.reply(
                f"📚 Обучение: chat_id={chat_id}, limit={limit}.\n"
                f"Это может занять несколько минут."
            )
            asyncio.create_task(self._learn_task(event, chat_id, limit))
        else:
            chats = storage.get_managed_chat_ids() or []
            await event.reply(
                f"📚 Обучение из {len(chats)} managed-чатов, "
                f"limit={limit} пар на чат.\nОтчёт по завершении."
            )
            asyncio.create_task(self._learn_all_task(event, limit))

    async def _learn_task(self, event, chat_id, limit):
        try:
            stats = await learn.learn_from_chat(self.client, chat_id, limit=limit)
            text = (
                f"✅ chat={chat_id} завершён.\n"
                f"Сообщений: {stats.get('messages', 0)}, "
                f"пар: {stats.get('pairs_count', 0)}\n"
                f"{learn.format_stats_short(stats)}"
            )
            await event.reply(text)
        except Exception as e:
            logger.exception("learn_task failed for chat=%s", chat_id)
            try:
                await event.reply(f"⚠️ Ошибка: {e}")
            except Exception:
                pass

    async def _learn_all_task(self, event, limit):
        try:
            overall = await learn.learn_from_all_chats(self.client, limit_per_chat=limit)
            text = (
                f"✅ Обучение завершено: {overall['chats_processed']}/"
                f"{overall['chats_total']} чатов.\n"
                f"Сообщений: {overall['messages']}, "
                f"пар: {overall['pairs_count']}, "
                f"обработано: {overall['processed']}\n"
                f"💎 Сохранено: <b>{overall['saved']}</b> | "
                f"пропущено: {overall['skipped']} | "
                f"ошибок: {overall['errors']}"
            )
            await event.reply(text, parse_mode="html")
        except Exception as e:
            logger.exception("learn_all_task failed")
            try:
                await event.reply(f"⚠️ Ошибка: {e}")
            except Exception:
                pass

    async def _handle_pricing_command(self, event, text: str) -> bool:
        """Управление прайсом ЛК — единый источник цен. Только в брейн-чате.

        Команды:
          • «прайс» / «прайс показать» — текущий список цен
          • «прайс БАНК ЦЕНА» — задать/обновить цену банка
          • «прайс удали БАНК» / «прайс delete БАНК» — снять цену
        Возвращает True если команда обработана."""
        clean = text.strip()

        # Удаление
        m_del = re.match(
            r"^\s*(?:прайс|price|цены|цена)\s+(?:удали(?:ть)?|delete|remove|"
            r"снять|снеси)\s+([\wа-яА-Я\-]+)\s*$",
            clean, re.I,
        )
        if m_del:
            bank = m_del.group(1)
            ok = await storage.remove_pricing(bank)
            if ok:
                await event.reply(
                    f"🗑 Цена банка <b>{bank.upper()}</b> снята.",
                    parse_mode="html",
                )
                await self._sync_pricing_to_knowledge()
            else:
                await event.reply(
                    f"ℹ️ Цена банка <b>{bank.upper()}</b> и так не задана.",
                    parse_mode="html",
                )
            return True

        # Показать
        if re.match(
            r"^\s*(?:прайс|price|цены|цена)\s*(?:показать|показ|list|"
            r"список|таблица)?\s*$",
            clean, re.I,
        ):
            prices = storage.list_pricing()
            if not prices:
                await event.reply(
                    "📋 <b>Прайс ЛК</b> пуст.\n\n"
                    "Задай цену командой:\n"
                    "<code>прайс АЛЬФА 400</code>\n"
                    "<code>прайс ОЗОН 300</code>",
                    parse_mode="html",
                )
                return True
            lines = ["📋 <b>Прайс ЛК</b> (USDT за один ЛК):", ""]
            for bank, price in sorted(prices.items()):
                lines.append(f"• <b>{bank}</b> — <code>{price:g}$</code>")
            lines.append("")
            lines.append(
                "<i>Изменить:</i> <code>прайс БАНК ЦЕНА</code>\n"
                "<i>Удалить:</i> <code>прайс удали БАНК</code>"
            )
            await event.reply(
                "\n".join(lines), parse_mode="html", link_preview=False,
            )
            return True

        # Установить цену
        m_set = re.match(
            r"^\s*(?:прайс|price|цены|цена)\s+([\wа-яА-Я\-]+)\s+"
            r"([\d.,]+)\s*\$?\s*$",
            clean, re.I,
        )
        if m_set:
            bank = m_set.group(1)
            try:
                price = float(m_set.group(2).replace(",", "."))
            except ValueError:
                await event.reply(
                    f"⚠️ Не понял цену: <code>{m_set.group(2)}</code>",
                    parse_mode="html",
                )
                return True
            ok = await storage.set_pricing(bank, price)
            if not ok:
                await event.reply("⚠️ Не смог сохранить.")
                return True
            await event.reply(
                f"✅ Прайс обновлён: <b>{bank.upper()}</b> = "
                f"<code>{price:g}$</code>",
                parse_mode="html",
            )
            await self._sync_pricing_to_knowledge()
            return True

        return False

    async def _sync_pricing_to_knowledge(self):
        """Переписывает knowledge/pricing.md шаблоном из storage — чтобы
        AI читал актуальные цены. Использует GitHub Contents API."""
        prices = storage.list_pricing()
        if not config.GITHUB_TOKEN:
            logger.info("pricing sync: GITHUB_TOKEN не задан — пропускаю")
            return
        lines = [
            "# Прайс ЛК",
            "",
            "> 🔴 **Единый источник цен.** Этот файл переписывается",
            "> юзерботом автоматически при команде «прайс БАНК ЦЕНА»",
            "> в брейн-чате. Любые другие упоминания цен в knowledge —",
            "> **устарели**. AI должен использовать ТОЛЬКО эти значения.",
            "",
            "Цена — сколько мы платим поставщику за один ЛК (в USDT).",
            "",
        ]
        if not prices:
            lines.append("_Прайс пуст — задайте цены через `прайс БАНК ЦЕНА`._")
        else:
            lines.append("| Банк | Цена ЛК |")
            lines.append("|---|---|")
            for bank, price in sorted(prices.items()):
                lines.append(f"| {bank} | {price:g}$ |")
        new_md = "\n".join(lines) + "\n"
        try:
            url = await memory.commit_to_knowledge(
                file="pricing.md",
                append_block=new_md,
                commit_msg="pricing: обновление через команду «прайс» в брейн-чате",
                overwrite=True,
            )
            if url:
                logger.info("pricing.md synced to knowledge: %s", url)
            else:
                logger.warning("pricing.md sync to knowledge failed (no url)")
        except Exception as e:
            logger.warning("pricing.md sync failed: %s", e)

    async def _handle_brain_chat_writeback(self, event):
        if not event.message:
            return
        text = (event.message.text or "").strip()
        if not text:
            return
        if self._me and event.sender_id == self._me.id:
            return
        if text.startswith("[AI-LOG]"):
            return
        # /learn — bulk-обучение из истории чатов
        if text.lower().startswith("/learn"):
            await self._handle_learn_command(event, text)
            return
        # Команды очистки (в брейн-чате — самые быстрые)
        low = text.lower().strip()
        cleanup_cmds = (
            "очистить маржу", "очисти маржу", "обнули маржу",
            "очистить бухгалтерию", "очисти бухгалтерию",
            "очистить ai", "очисти ai", "очистить статистику ai",
            "очистить всё", "очисти всё", "сбросить статистику",
            "clear margin", "clear accounting", "clear ai",
        )
        if any(low == c or low.startswith(c + " ") for c in cleanup_cmds):
            result = await self._execute_dashboard_command(text.strip())
            try:
                await event.reply(result, parse_mode="html", link_preview=False)
            except Exception:
                pass
            return

        # /checkchatforLKCARD <chat_id> — восстановление карточки ЛК из брейн-чата
        if low.startswith("/checkchatforlkcard") or low.startswith("/checklk"):
            await self._handle_checkchat_brain_command(event, text)
            return

        # /sync_lk — синхронизация карточек ЛК из истории Группы 1
        if (
            low.startswith("/sync_lk")
            or low.startswith("/sync_cards")
            or low.startswith("синхронизация карточек")
            or low.startswith("синхронизация лк")
        ):
            m_lim = re.search(r"\b(\d+)\b", text)
            limit = int(m_lim.group(1)) if m_lim else 500
            limit = max(50, min(limit, 3000))
            await self._apply_sync_lk_cards(event, limit=limit)
            return
        # Команда «прайс» — управление прайсом ЛК (единый источник цен).
        if re.match(r"^\s*(?:прайс|price|цены|цена)\b", text, re.I):
            handled = await self._handle_pricing_command(event, text)
            if handled:
                return
        if text.startswith("/"):
            return
        if not storage.is_writeback_enabled():
            return
        if not config.ANTHROPIC_API_KEY:
            return

        logger.info("brain writeback: processing %d chars from chat=%s", len(text), event.chat_id)
        try:
            result = await memory.process_brain_chat_message(text)
        except Exception as e:
            logger.exception("brain writeback unexpected error: %s", e)
            await storage.bump_writeback_stats(errors=1)
            return

        status = result.get("status")
        if status == "ok":
            await storage.bump_writeback_stats(commits=1)
            url = result.get("url") or ""
            file = result.get("file")
            preview = result.get("preview", "")
            reply = (
                f"✅ Сохранено в `knowledge/{file}`\n"
                f"Commit: {url}\n\n"
                f"```\n{preview}\n```"
            )
            try:
                await event.reply(reply, link_preview=False)
            except Exception as e:
                logger.warning("brain writeback ack failed: %s", e)
        elif status == "skipped":
            await storage.bump_writeback_stats(skipped=1)
            try:
                await event.reply("📝 Принял к сведению, но это не похоже на факт для сохранения.")
            except Exception:
                pass
        elif status == "commit_fail":
            await storage.bump_writeback_stats(errors=1)
            file = result.get("file") or "?"
            try:
                await event.reply(
                    f"⚠️ Не смог закоммитить в `knowledge/{file}` "
                    f"(GitHub API). Проверь GITHUB_TOKEN и логи."
                )
            except Exception:
                pass
        elif status == "no_token":
            try:
                await event.reply("⚠️ GITHUB_TOKEN не задан в env — writeback в граф невозможен.")
            except Exception:
                pass
        elif status == "classify_fail":
            await storage.bump_writeback_stats(errors=1)
            try:
                await event.reply("⚠️ Claude не смог разобрать сообщение в JSON.")
            except Exception:
                pass

    # Короткие ack-сообщения — не дёргаем Claude вообще (экономия ~30% запросов)
    _ACK_RE = re.compile(
        r"^[\s+\-•·]*(ок|ok|оk|окей|okay|понял|поняла|понятно|"
        r"спасибо|спс|thanks|thx|давай|давайте|"
        r"хорошо|хор|good|готов|готова|"
        r"да|нет|yes|no|ага|угу|ясно|ясн|"
        r"\+|плюс|сек|секунду|минут\w*|"
        r"подождите|подожди|жду|"
        r"принял|принято|приму|"
        r"👍|👌|✅|🙏|❤|♥|😊|🤝)[\s.!?)+\-]*$",
        re.I,
    )

    def _is_ack_message(self, text: str) -> bool:
        """True если это просто подтверждение/благодарность без вопроса."""
        if not text:
            return False
        t = text.strip()
        # Если длиннее 30 символов — точно не ack
        if len(t) > 30:
            return False
        # Если есть знак вопроса — нужен ответ
        if "?" in t:
            return False
        return bool(self._ACK_RE.match(t))

    async def _do_ai_reply(self, event, chat_info: dict, idle_sec: int, chat_key: str):
        chat_id = event.chat_id
        # Извлекаем текст один раз — используется в ack-фильтре, классификаторе
        # релевантности и логах.
        text_now = ""
        try:
            text_now = ((event.message and event.message.text) or "").strip()
        except Exception:
            pass

        # === ECONOMY GUARD: пропуск ack-сообщений ===
        try:
            if self._is_ack_message(text_now):
                logger.info(
                    "AI: chat=%s — ack '%s', пропуск (экономия токенов)",
                    chat_id, text_now[:40],
                )
                await storage.bump_ai_stats(skipped_ack=1)
                _e("ai-skip-ack", {
                    "chat_id": chat_id, "text": text_now[:60],
                }, severity="info")
                return
        except Exception as e:
            logger.warning("ack check failed: %s", e)

        delay = random.uniform(config.AI_TYPING_DELAY_MIN, config.AI_TYPING_DELAY_MAX)
        try:
            async with self.client.action(chat_id, "typing"):
                await asyncio.sleep(delay)
        except Exception:
            await asyncio.sleep(delay)

        # Повторная проверка: если worker написал пока мы «печатали» — отступаем,
        # но только если его ответ свежий (last_worker_ts > last_client_msg_ts).
        last_worker_ts = self._last_worker_ts.get(chat_key, 0)
        last_client_ts = self._last_client_msg_ts.get(chat_key, 0)
        if (
            idle_sec > 0
            and last_worker_ts > last_client_ts
            and time.time() - last_worker_ts < idle_sec
        ):
            await storage.bump_ai_stats(skipped_worker_active=1)
            logger.info("AI: chat=%s — worker came in during typing delay, skip", chat_id)
            return

        client_id = chat_info.get("client_id") or 0
        try:
            history = await self._fetch_history_for_claude(chat_id, client_id)
        except Exception as e:
            logger.warning("AI: history fetch failed for chat=%s: %s", chat_id, e)
            history = []
        if not history or history[-1]["role"] != "user":
            logger.info("AI: chat=%s — empty/invalid history", chat_id)
            return

        # === ECONOMY GUARD #2: классификатор релевантности ===
        # Дёшевый Haiku-вызов решает «нужен ли ответ?». Отсекает шуточки/болтовню/
        # реакции до основного AI-вызова. ~$0.0001 за фильтрацию, экономия ~30-40%
        # вызовов основного брейна.
        # Direct-триггеры (Ассистент / банки / деньги / вопрос) минуют классификатор.
        try:
            force_respond = _should_force_respond(text_now)
            if (
                config.AI_RELEVANCE_CHECK_ENABLED
                and not force_respond
            ):
                # Берём только последние 4 сообщения чтобы классификатор был дёшев
                tail = history[-4:] if len(history) >= 1 else history
                action = await brain.classify_relevance(tail)
                if action == "skip":
                    logger.info(
                        "AI: chat=%s — relevance=SKIP (%.40s...) экономия токенов",
                        chat_id, text_now[:50],
                    )
                    try:
                        await storage.bump_ai_relevance_stats(skipped=1)
                    except Exception:
                        pass
                    _e("ai-relevance-skip", {
                        "chat_id": chat_id,
                        "text": text_now[:120],
                    }, severity="info")
                    # Один раз на чат — подсказка про «Ассистент»
                    if (
                        config.AI_ASSISTANT_HINT_ENABLED
                        and not storage.was_assistant_hint_sent(chat_id)
                    ):
                        try:
                            marked = await storage.mark_assistant_hint_sent(chat_id)
                            if marked:
                                await self.client.send_message(
                                    chat_id,
                                    config.AI_ASSISTANT_HINT_TEXT,
                                )
                                logger.info(
                                    "AI: chat=%s — отправлена подсказка про Ассистента",
                                    chat_id,
                                )
                                _e("ai-assistant-hint-sent", {
                                    "chat_id": chat_id,
                                }, severity="info")
                        except Exception as e:
                            logger.warning(
                                "send assistant hint failed: %s", e,
                            )
                    return
                else:
                    # relevance="respond" — продолжаем как обычно
                    try:
                        await storage.bump_ai_relevance_stats(responded=1)
                    except Exception:
                        pass
        except Exception as e:
            # Любая ошибка классификатора — fail-safe, продолжаем основной AI
            logger.warning("relevance classifier failed: %s — fallback respond", e)

        brain_notes = await self._fetch_brain_notes()

        client_username = None
        if client_id:
            try:
                client_entity = await self.client.get_entity(client_id)
                client_username = getattr(client_entity, "username", None)
            except Exception as e:
                logger.warning("client entity resolve failed: %s", e)
        # Память клиента: подгружаем прошлые предпочтения из client_preferences
        # (по @username, между разными work-чатами). AI увидит что у клиента
        # уже есть выбранный метод оплаты и не будет спрашивать заново.
        prev_prefs = {}
        try:
            uname_for_prefs = (
                (chat_info.get("client_username") or client_username or "")
                .lstrip("@").strip()
            )
            if uname_for_prefs:
                prev_prefs = storage.get_client_preferences(uname_for_prefs)
        except Exception as e:
            logger.warning("get_client_preferences failed: %s", e)

        client_context = {
            "id": client_id,
            "name": chat_info.get("client_name") or "",
            "username": client_username or "",
            "prev_preferences": prev_prefs,  # {payment_method, usdt_address, lk_count, ...}
        }

        last_msg_id = getattr(getattr(event, "message", None), "id", None)
        async def _executor(name, inp):
            return await self._execute_ai_tool(name, inp, chat_id=chat_id, last_msg_id=last_msg_id)

        async with self.client.action(chat_id, "typing"):
            reply, usage = await brain.generate_reply(
                history,
                brain_notes=brain_notes,
                tools_executor=_executor,
                client_context=client_context,
            )
        if reply is None:
            await storage.bump_ai_stats(errors=1)
            logger.warning("AI: chat=%s — claude returned None", chat_id)
            return

        # 🔴 ФИЛЬТР: AI иногда пишет «молчу, не вмешиваюсь» / «внутренний диалог
        # команды» вместо того чтобы реально молчать. Это пустой шум, тратит
        # кредиты, мусорит чат. Перехватываем и НЕ отправляем.
        if _is_silence_announcement(reply):
            logger.info(
                "AI: chat=%s — пропускаю «silence announcement» (%.40s...)",
                chat_id, reply.strip(),
            )
            _e("ai-silence-suppressed", {
                "chat_id": chat_id,
                "short": reply[:120],
            }, severity="warning")
            return

        # 🔴 GUARD: если AI говорит клиенту «/clients» НО CRM-бот ещё НЕ
        # добавлен в чат (нет crm_owner записи) — насильно дёргаем
        # add_partner_to_crm СНАЧАЛА, потом уже отдаём ответ. Иначе клиент
        # пишет /clients, а бот не отвечает — потому что его нет в чате.
        if reply and "/clients" in reply.lower():
            try:
                client_uname = (chat_info.get("client_username") or "").lstrip("@").strip()
                client_uid = int(chat_info.get("client_id") or 0)
                # Проверяем — есть ли уже owner для этого клиента
                owner = None
                try:
                    if client_uid:
                        owner = storage.find_crm_owner_by_tg(client_uid)
                    if not owner and client_uname:
                        owner = storage.find_crm_owner_by_username(client_uname)
                except Exception as e:
                    logger.warning("crm_owner lookup fail: %s", e)
                if not owner and client_uname:
                    logger.warning(
                        "AI: /clients в ответе БЕЗ предварительного "
                        "add_partner_to_crm — форсим вызов tool для @%s",
                        client_uname,
                    )
                    try:
                        res = await self._tool_add_partner_to_crm(
                            chat_id=chat_id, client_username=client_uname,
                        )
                        logger.info("forced add_partner_to_crm result: %s", res)
                        if res.get("status") != "ok":
                            # Если совсем плохо — НЕ отправляем reply (юзеру /clients
                            # без CRM в чате бесполезен), эскалируем работнику.
                            logger.error(
                                "Failed to add CRM bot for @%s — suppressing /clients reply",
                                client_uname,
                            )
                            _e("ai-crm-add-failed", {
                                "chat_id": chat_id, "client_username": client_uname,
                                "error": res.get("error"),
                            }, severity="error")
                            return
                        # Дать CRM-боту 1-2 сек чтобы welcome message пришёл
                        await asyncio.sleep(1.5)
                    except Exception as e:
                        logger.exception("forced add_partner_to_crm failed: %s", e)
                        return
                elif owner:
                    logger.info(
                        "AI: /clients в ответе — CRM-owner УЖЕ есть для @%s, форс не нужен",
                        client_uname,
                    )
            except Exception as e:
                logger.warning("/clients guard error: %s", e)

        # 🔴🔴🔴 ФИЛЬТР: запрещённые темы (брак на нашей стороне, пересдача
        # отклонённых счетов, спекуляции про сотрудников, обещания компенсаций).
        # Полный список — knowledge/policy.md «ЗАПРЕЩЁННЫЕ ТЕМЫ В ОТВЕТАХ КЛИЕНТУ».
        # Если матчится — НЕ отправляем (молчим) и фиксируем для метрики.
        has_forbidden, forbidden_pat = _has_forbidden_topic(reply)
        if has_forbidden:
            logger.warning(
                "AI: chat=%s — ЗАБЛОКИРОВАН ответ с запрещённой темой "
                "(pat=%r) %.80s...",
                chat_id, forbidden_pat, reply.strip(),
            )
            _e("ai-forbidden-topic-blocked", {
                "chat_id": chat_id,
                "pattern": forbidden_pat,
                "short": reply[:200],
            }, severity="error")
            return

        # 🔴 ПОСТ-ПРОЦЕССИНГ: добавляем hint про оператора + ev. prompt подразделения.
        # 1-й ответ AI клиенту → hint "если что напишите Ассистент"
        # 2-й ответ → prompt подразделения (с цифрами 1/2/3)
        # 3+ → стандартный hint "напишите Ассистент позови оператора"
        try:
            ai_count = int(chat_info.get("ai_reply_count") or 0) if chat_info else 0
        except Exception:
            ai_count = 0
        hint = ""
        # Не дублируем если AI уже сам написал слово "оператор"/"Ассистент позови"
        reply_low = (reply or "").lower()
        already_has_hint = (
            ("ассистент позови" in reply_low)
            or ("позови оператор" in reply_low)
            or ("позвать оператор" in reply_low)
            or ("на какое подразделение" in reply_low)
            or ("выберите подразделение" in reply_low)
        )
        if not already_has_hint:
            if ai_count == 0:
                # Первый ответ — учим клиента триггеру
                hint = (
                    "\n\n<i>💬 Если я вам понадоблюсь — просто напишите "
                    "«Ассистент» и дальше свой вопрос. "
                    "Если хотите живого оператора — «Ассистент позови оператора».</i>"
                )
            else:
                # Все последующие ответы — короткий hint про оператора
                hint = (
                    "\n\n<i>💬 Если нужен живой оператор — напишите "
                    "«Ассистент позови оператора».</i>"
                )
        # ВАЖНО: prompt подразделения здесь НЕ показываем.
        # Меню 1/2/3 появляется ТОЛЬКО при срабатывании триггера
        # ("оператор/менеджер/Ассистент позови") — обрабатывается выше.
        # Markdown → HTML: Claude иногда шлёт **bold** и *italic* — Telethon с
        # parse_mode=html их не парсит и они показываются как текст.
        # Конвертируем безопасно: ` → <code>, ** → <b>, * → <i>.
        reply_with_hint = reply + hint
        try:
            converted = reply_with_hint
            # Сначала ` `code` ` (чтобы не задеть * внутри)
            converted = re.sub(r"`([^`\n]+)`", r"<code>\1</code>", converted)
            # **bold** (двойные)
            converted = re.sub(r"\*\*([^\*\n]+?)\*\*", r"<b>\1</b>", converted)
            # *italic* (одиночные) — только если не часть HTML тега
            converted = re.sub(r"(?<![<>\w])\*([^\*\n]+?)\*(?![<>\w])", r"<i>\1</i>", converted)
            # Оставшиеся одиночные ** убираем (если есть)
            converted = converted.replace("**", "")
            reply_to_send = converted
            for chunk in _split_text(reply_to_send, 3900):
                await self.client.send_message(
                    chat_id, chunk, parse_mode="html", link_preview=False,
                )
                await asyncio.sleep(0.3)
            # Инкрементируем счётчик AI-ответов в чате
            try:
                await storage.bump_ai_reply_count(chat_id)
            except Exception:
                pass
        except Exception as e:
            logger.warning("AI: send failed chat=%s: %s", chat_id, e)
            await storage.bump_ai_stats(errors=1)
            return

        await storage.bump_ai_stats(
            replies=1,
            input_tokens=usage.get("input_tokens", 0),
            output_tokens=usage.get("output_tokens", 0),
            cache_read_tokens=usage.get("cache_read_tokens", 0),
            cache_write_tokens=usage.get("cache_creation_tokens", 0),
            model=usage.get("model") or config.DEFAULT_AI_MODEL,
        )
        logger.info(
            "AI: replied chat=%s in=%s out=%s",
            chat_id, usage.get("input_tokens"), usage.get("output_tokens"),
        )
        _e("ai-reply", {
            "chat_id": chat_id,
            "client_username": chat_info.get("client_username"),
            "client_name": chat_info.get("client_name"),
            "input_tokens": usage.get("input_tokens", 0),
            "output_tokens": usage.get("output_tokens", 0),
            "cache_read": usage.get("cache_read_tokens", 0),
            "short": (reply[:80] if reply else ""),
        }, character="chat", severity="info")

        client_text = (event.message.text or "").strip()
        await self._log_to_brain(
            chat_id=chat_id,
            chat_info=chat_info,
            client_text=client_text,
            ai_text=reply,
            usage=usage,
        )

    async def _fetch_history_for_claude(self, chat_id, client_id: int) -> list[dict]:
        msgs: list[dict] = []
        last_user_msg_id_with_photo = None  # для Vision на последнем сообщении
        try:
            async for m in self.client.iter_messages(chat_id, limit=config.AI_HISTORY_LIMIT):
                txt = (m.text or "").strip()
                has_photo = bool(getattr(m, "photo", None))
                # Сообщения без текста И без фото — пропускаем
                if not txt and not has_photo:
                    continue
                if self._me and m.sender_id == self._me.id:
                    if txt:
                        msgs.insert(0, {"role": "assistant", "content": txt})
                elif client_id and m.sender_id == client_id:
                    # Сохраняем id ПЕРВОГО (в итерации = последнего) фото клиента
                    if has_photo and last_user_msg_id_with_photo is None:
                        last_user_msg_id_with_photo = m.id
                    content_text = txt or "(прислал изображение без подписи)"
                    msgs.insert(0, {
                        "role": "user", "content": content_text,
                        "_msg_id": m.id, "_has_photo": has_photo,
                    })
                else:
                    if not txt:
                        continue
                    try:
                        s = await m.get_sender()
                    except Exception:
                        s = None
                    name = getattr(s, "first_name", None) or "Сотрудник"
                    msgs.insert(0, {"role": "user", "content": f"[{name}]: {txt}"})
        except Exception as e:
            logger.warning("history iter failed for chat=%s: %s", chat_id, e)
            return []

        while msgs and msgs[0]["role"] != "user":
            msgs.pop(0)
        while msgs and msgs[-1]["role"] != "user":
            msgs.pop()

        # === CLAUDE VISION: для ПОСЛЕДНЕГО клиентского сообщения с фото
        # подгружаем изображение и заменяем content на list-of-blocks.
        # Только последнее (не вся история — иначе раздувание токенов и денег).
        if last_user_msg_id_with_photo and msgs:
            try:
                # Найти последнее user-сообщение с фото в msgs (это и есть оно)
                for i in range(len(msgs) - 1, -1, -1):
                    msg = msgs[i]
                    if (msg.get("_has_photo")
                            and msg.get("_msg_id") == last_user_msg_id_with_photo):
                        # Скачиваем фото
                        try:
                            tg_msg = await self.client.get_messages(
                                chat_id, ids=last_user_msg_id_with_photo,
                            )
                            if tg_msg and tg_msg.photo:
                                import io, base64
                                buf = io.BytesIO()
                                await self.client.download_media(tg_msg, file=buf)
                                data = buf.getvalue()
                                if data and len(data) < 5 * 1024 * 1024:  # < 5 MB
                                    b64 = base64.standard_b64encode(data).decode()
                                    text = msg.get("content") or ""
                                    msg["content"] = [
                                        {
                                            "type": "image",
                                            "source": {
                                                "type": "base64",
                                                "media_type": "image/jpeg",
                                                "data": b64,
                                            },
                                        },
                                        {"type": "text", "text": text or "Смотри изображение."},
                                    ]
                                    logger.info(
                                        "Claude Vision: attached photo to msg=%s "
                                        "(%d KB)", last_user_msg_id_with_photo,
                                        len(data) // 1024,
                                    )
                                else:
                                    logger.warning(
                                        "Vision: photo too big or empty (%d bytes)",
                                        len(data) if data else 0,
                                    )
                        except Exception as e:
                            logger.warning("Vision download failed: %s", e)
                        break
            except Exception as e:
                logger.warning("Vision processing top-level fail: %s", e)

        # Очистка вспомогательных полей перед отдачей в Claude API
        for m in msgs:
            m.pop("_msg_id", None)
            m.pop("_has_photo", None)
        return msgs

    async def _fetch_brain_notes(self) -> str:
        bid = storage.get_brain_chat_id()
        if not bid:
            return ""
        parts: list[str] = []
        try:
            async for m in self.client.iter_messages(bid, limit=config.AI_BRAIN_NOTES_LIMIT):
                txt = (m.text or "").strip()
                if not txt:
                    continue
                if txt.startswith("[AI-LOG]"):
                    continue
                ts = m.date.strftime("%Y-%m-%d %H:%M") if m.date else ""
                parts.insert(0, f"[{ts}] {txt}")
        except Exception as e:
            logger.warning("brain notes fetch failed: %s", e)
            return ""
        return "\n".join(parts)

    async def _log_to_brain(self, chat_id, chat_info: dict, client_text: str, ai_text: str, usage: dict):
        bid = storage.get_brain_chat_id()
        if not bid:
            return
        client_name = chat_info.get("client_name") or "—"
        in_t = usage.get("input_tokens", 0)
        out_t = usage.get("output_tokens", 0)
        ct = client_text if len(client_text) <= 600 else client_text[:600] + "…"
        at = ai_text if len(ai_text) <= 1500 else ai_text[:1500] + "…"
        log_msg = (
            f"[AI-LOG] 💬 {client_name}\n"
            f"chat_id={chat_id}, tokens in={in_t} out={out_t}\n"
            f"━━━━━━━━━━━━━━\n"
            f"👤 {ct}\n\n"
            f"🤖 {at}"
        )
        try:
            target = await self._resolve_chat_target(bid)
            await self.client.send_message(target, log_msg)
        except Exception as e:
            logger.warning("brain log send failed: %s", e)

    # === AI tool-use ===
    CRM_BOT_USERNAME = "PrideCONTROLE_bot"

    async def _execute_ai_tool(self, tool_name: str, tool_input: dict, chat_id, last_msg_id=None) -> dict:
        logger.info(
            "AI tool exec: %s input=%s chat=%s msg=%s",
            tool_name, tool_input, chat_id, last_msg_id,
        )
        if tool_name == "add_partner_to_crm":
            return await self._tool_add_partner_to_crm(
                chat_id=chat_id,
                client_username=tool_input.get("client_username", ""),
            )
        if tool_name == "escalate_to_team":
            return await self._tool_escalate_to_team(
                work_chat_id=chat_id,
                last_msg_id=last_msg_id,
                specialist=tool_input.get("specialist", ""),
                reason=tool_input.get("reason", ""),
                client_question=tool_input.get("client_question", ""),
            )
        if tool_name == "record_deal":
            return await self._tool_record_deal(work_chat_id=chat_id, **tool_input)
        if tool_name == "update_deal_status":
            return await self._tool_update_deal_status(**tool_input)
        if tool_name == "find_deal":
            return await self._tool_find_deal(**tool_input)
        if tool_name == "create_lk_card":
            return await self._tool_create_lk_card(
                chat_id=chat_id,
                bank=tool_input.get("bank", ""),
                fio=tool_input.get("fio", ""),
                price_usdt=float(tool_input.get("price_usdt", 0) or 0),
                payment_method=tool_input.get("payment_method", ""),
                deal_id=tool_input.get("deal_id", ""),
                usdt_address=tool_input.get("usdt_address", ""),
            )
        if tool_name == "set_payment_method":
            return await self._tool_set_payment_method(
                chat_id=chat_id,
                method=tool_input.get("method", ""),
                usdt_address=tool_input.get("usdt_address", ""),
            )
        return {"status": "error", "error": f"unknown_tool:{tool_name}"}

    async def _tool_set_payment_method(
        self, chat_id, method: str = "", usdt_address: str = "",
    ) -> dict:
        """Фиксирует метод оплаты и USDT-адрес клиента в managed_chats.

        Принимает короткое значение из enum brain.py:
          - 'USDT_TRC20' (требует usdt_address)
          - 'GUARANTOR' (расшифровываем в GUARANTOR_AFTER_WORK как default)
        Также принимает полные значения из accounting2.PAYMENT_METHODS.
        После записи, если в storage.pending_perevyaz уже лежит (банк+ФИО)
        от sys01/CRM-бота — сразу создаёт карточку ЛК.
        """
        m_raw = (method or "").strip().upper()
        if not m_raw:
            return {"status": "error", "error": "method_required"}
        # Алиасы: AI может прислать короткое "GUARANTOR" — это значит default
        # для PRIDE: сделка ПОСЛЕ отработки счёта.
        alias_map = {
            "GUARANTOR": "GUARANTOR_AFTER_WORK",
            "USDT": "USDT_TRC20",
            "TRC20": "USDT_TRC20",
        }
        method_full = alias_map.get(m_raw, m_raw)
        if method_full not in accounting2.PAYMENT_METHODS:
            return {
                "status": "error",
                "error": "payment_method_invalid",
                "got": m_raw,
                "allowed": list(accounting2.PAYMENT_METHODS) + list(alias_map),
            }
        addr = (usdt_address or "").strip()
        if method_full == "USDT_TRC20" and not addr:
            return {"status": "error", "error": "usdt_address_required_for_usdt"}

        # Сохраняем в managed_chats
        try:
            await storage.set_chat_payment_info(
                chat_id, method=method_full, usdt_address=addr or "",
            )
        except Exception as e:
            logger.warning("set_payment_method: save failed: %s", e)
            return {"status": "error", "error": "save_failed", "detail": str(e)}

        # Память клиента: дублируем в client_preferences (по @username)
        try:
            info = storage.get_chat_info(chat_id) or {}
            uname = (info.get("client_username") or "").lstrip("@").strip()
            if uname:
                await storage.save_client_preferences(
                    uname, payment_method=method_full, usdt_address=addr,
                )
        except Exception as e:
            logger.warning("set_payment_method: save_client_preferences failed: %s", e)

        logger.info(
            "set_payment_method: chat=%s method=%s usdt=%s",
            chat_id, method_full, addr[:10] + "..." if addr else "",
        )

        # СЦЕНАРИЙ A: карточка для этого work_chat УЖЕ существует
        # (создалась автоматически после перевязки с дефолтным методом).
        # Тогда просто обновляем её payment_method и перепостим в TG.
        created_card_id = None
        updated_existing = False
        try:
            for cid, c in (storage.list_lk_cards() or {}).items():
                if not c:
                    continue
                wc_card = int(c.get("work_chat_id") or 0)
                if wc_card and abs(wc_card) == abs(int(chat_id)):
                    if (c.get("status") or "В_РАБОТЕ") in ("В_РАБОТЕ", "ОЖИДАНИЕ", ""):
                        await storage.update_lk_card(
                            cid,
                            payment_method=method_full,
                            usdt_address=addr,
                            _allow_payment_method_change=True,
                        )
                        try:
                            await self._refresh_lk_card_post(cid)
                        except Exception:
                            pass
                        updated_existing = True
                        created_card_id = cid
                        logger.info(
                            "set_payment_method: existing lk_card %s updated method=%s",
                            cid, method_full,
                        )
                        break
        except Exception as e:
            logger.warning("set_payment_method: update existing failed: %s", e)

        # СЦЕНАРИЙ B: карточки нет, но перевязка УЖЕ была (pending_perevyaz).
        if not updated_existing:
            try:
                pending = await storage.pop_pending_perevyaz(chat_id)
                if pending and (pending.get("bank") or pending.get("fio")):
                    fresh = storage.get_chat_info(chat_id) or {}
                    class _Shim:
                        def __init__(self, cid):
                            self.chat_id = cid
                            self.message = type("M", (), {"id": None, "text": ""})()
                    shim = _Shim(chat_id)
                    await self._create_lk_card_from_perevyaz(
                        shim, fresh,
                        lk_text=pending.get("bank", ""),
                        fio_text=pending.get("fio", ""),
                    )
                    logger.info(
                        "set_payment_method: card created from pending perevyaz for chat=%s",
                        chat_id,
                    )
            except Exception as e:
                logger.warning("set_payment_method: pending card creation failed: %s", e)

        return {
            "status": "ok",
            "payment_method": method_full,
            "usdt_address": addr,
            "card_created": bool(created_card_id),
        }

    async def _tool_add_partner_to_crm(self, chat_id, client_username: str) -> dict:
        username = (client_username or "").lstrip("@").strip()
        if not username:
            return {"status": "error", "error": "client_username_empty"}

        try:
            bot_entity = await self.client.get_entity(self.CRM_BOT_USERNAME)
        except UsernameNotOccupiedError:
            return {"status": "error", "step": "resolve", "error": "crm_bot_not_found"}
        except Exception as e:
            return {"status": "error", "step": "resolve", "error": str(e)}

        try:
            await self.client(InviteToChannelRequest(chat_id, [bot_entity]))
            invite_status = "added"
        except UserAlreadyParticipantError:
            invite_status = "already_in_chat"
        except UserPrivacyRestrictedError:
            return {"status": "error", "step": "invite", "error": "privacy_restricted"}
        except FloodWaitError as e:
            return {"status": "error", "step": "invite", "error": f"flood_wait_{e.seconds}s"}
        except Exception as e:
            logger.warning("CRM invite warning: %s", e)
            invite_status = f"warn:{type(e).__name__}"

        try:
            rights = ChatAdminRights(
                change_info=False, post_messages=False, edit_messages=False,
                delete_messages=True, ban_users=False, invite_users=True,
                pin_messages=False, add_admins=False, anonymous=False, manage_call=False,
            )
            await self.client(EditAdminRequest(
                channel=chat_id, user_id=bot_entity, admin_rights=rights, rank="CRM",
            ))
            admin_status = "granted"
        except Exception as e:
            logger.warning("CRM admin grant non-fatal: %s", e)
            admin_status = f"skipped:{type(e).__name__}"

        try:
            await self.client.send_message(chat_id, f"+партнер @{username}")
        except FloodWaitError as e:
            return {"status": "error", "step": "command", "error": f"flood_wait_{e.seconds}s"}
        except Exception as e:
            return {"status": "error", "step": "command", "error": str(e)}

        # SILENT MODE: AI замолкает пока клиент заполняет анкету в
        # @PrideCONTROLE_bot. TTL 2 часа — это safety net на случай если
        # клиент бросит флоу. Снимется раньше:
        #   - явным запросом помощи от клиента (?/помог/не получ/etc)
        #   - сигналом CRM-бота «Отдать в работу» / «отправлено на обработку»
        #   - перевязкой ЛК.
        from storage import _norm_chat_id  # noqa
        try:
            silent_key = _norm_chat_id(chat_id)
            self._ai_silent_until[silent_key] = time.time() + 30 * 60  # было 2ч, снизил до 30мин
            logger.info(
                "AI silent mode ON for chat=%s (until CRM ready / 2h max) — клиент заполняет ЦРМ",
                chat_id,
            )
        except Exception as e:
            logger.warning("silent mode set failed: %s", e)

        return {
            "status": "ok",
            "invite": invite_status,
            "admin": admin_status,
            "command_sent": f"+партнер @{username}",
        }

    async def _tool_escalate_to_team(self, work_chat_id, last_msg_id, specialist: str, reason: str, client_question: str) -> dict:
        """Тегнуть менеджера ПРЯМО В work-чате клиента (НЕ в координаторской).

        Жёсткие правила анти-спама:
        1. Если менеджер уже отвечал в чате после последнего тега → не тегаем
        2. С последнего тега прошло < 5 мин → не тегаем
        3. Тегаем без кучи деталей — только «@spec — нужна помощь» + краткая причина

        Если тегнуть нельзя — возвращаем status=skipped с пояснением.
        AI должен ИНТЕРПРЕТИРОВАТЬ это как «менеджер уже знает / скоро ответит,
        клиента просто заверь что ждём» вместо ретега."""
        allowed = {"TimonSkupCL", "pride_sys01", "pride_manager1", "SIMBA_PRIDE_ADM"}
        spec = (specialist or "").lstrip("@").strip()
        if spec not in allowed:
            return {"status": "error", "error": f"unknown_specialist:{spec}"}

        if not work_chat_id:
            return {"status": "error", "error": "no_work_chat_id"}

        # Анти-спам проверка
        try:
            can_tag, refusal = await storage.can_tag_specialist(
                work_chat_id, spec, cooldown_sec=300,
            )
        except Exception as e:
            logger.warning("can_tag_specialist failed: %s", e)
            can_tag, refusal = (True, "")
        if not can_tag:
            logger.info(
                "AI: escalate to @%s in chat=%s SKIPPED — %s",
                spec, work_chat_id, refusal,
            )
            _e("escalation-skip", {
                "specialist": spec,
                "chat_id": work_chat_id,
                "reason": refusal,
            }, character="chat", severity="info")
            return {
                "status": "skipped",
                "reason": refusal,
                "specialist": spec,
                "hint": (
                    "Менеджер уже в курсе или скоро ответит. "
                    "Клиенту скажи что ждём ответа специалиста, не тегай повторно."
                ),
            }

        info = storage.get_chat_info(work_chat_id) or {}
        client_name = (info.get("client_name") or "").strip()
        # Лаконичный тег в work-чате: только @spec + ОДНА строка причины.
        # Контекст клиент знает сам — он же в этом чате.
        short_reason = (reason or "вопрос клиента").strip().rstrip(".").lower()
        text = f"@{spec}, нужна помощь: {short_reason}."
        try:
            target = await self._resolve_chat_target(work_chat_id)
            await self.client.send_message(
                target, text, parse_mode="html", link_preview=False,
            )
        except Exception as e:
            await storage.bump_escalate_stats(error=True)
            logger.warning("escalate send failed: %s", e)
            return {"status": "error", "step": "send", "error": str(e)}

        await storage.record_specialist_tag(work_chat_id, spec, reason=reason)
        await storage.bump_escalate_stats(specialist=spec)
        logger.info(
            "AI tagged @%s directly in work_chat=%s reason=%s",
            spec, work_chat_id, (reason or "")[:60],
        )
        _e("escalation", {
            "specialist": spec,
            "reason": reason,
            "chat_id": work_chat_id,
            "client_name": client_name,
            "client_question": (client_question or "")[:120],
        }, character="chat", severity="warning")
        return {
            "status": "ok",
            "specialist": spec,
            "work_chat_id": work_chat_id,
        }

    # === Tools для системы учёта сделок ===

    async def _tool_record_deal(
        self,
        deal_id: str = "",
        client_username: str = "",
        fio: str = "",
        bank: str = "",
        amount: str = "",
        fee: str = "",
        method: str = "",
        work_chat_id=None,
    ) -> dict:
        deal_id = (deal_id or "").strip()
        if not deal_id:
            return {"status": "error", "error": "deal_id_empty"}
        if storage.get_deal(deal_id):
            return {"status": "error", "error": "deal_already_exists", "deal_id": deal_id}
        ok = await storage.add_deal(
            deal_id=deal_id,
            client_username=client_username,
            fio=fio,
            bank=bank,
            amount=amount,
            fee=fee,
            method=method,
            status="ПОПОЛНИТЬ",
            work_chat_id=work_chat_id,
        )
        if not ok:
            return {"status": "error", "error": "add_failed"}
        logger.info("deal recorded: %s | @%s | %s | %s", deal_id, client_username, bank, amount)
        _e("deal-recorded", {
            "deal_id": deal_id, "client_username": client_username,
            "fio": fio, "bank": bank, "amount": amount, "method": method,
        }, character="chat", severity="success")

        moved_card_id = None
        if work_chat_id is not None:
            try:
                wc_norm = abs(int(work_chat_id))
                for cid, c in (storage.list_lk_cards() or {}).items():
                    if not c.get("work_chat_id"):
                        continue
                    if abs(int(c.get("work_chat_id"))) != wc_norm:
                        continue
                    if c.get("payment_method") != "GUARANTOR_AFTER_WORK":
                        continue
                    if c.get("status") not in ("ОТРАБОТАН", "ПОПОЛНИТЬ_И_ОТПУСТИТЬ"):
                        continue
                    # 1) Обновляем deal_id на карточке
                    await storage.update_lk_card(cid, deal_id=deal_id)
                    # 2) Меняем статус → ПОПОЛНИТЬ_И_ОТПУСТИТЬ (это финальный статус
                    #    перед ЗАВЕРШЁН для GUARANTOR_AFTER_WORK). Цикл:
                    #    В_РАБОТЕ → ОТРАБОТАН → ПОПОЛНИТЬ_И_ОТПУСТИТЬ → ЗАВЕРШЁН.
                    try:
                        await storage.set_lk_card_status(
                            cid, "ПОПОЛНИТЬ_И_ОТПУСТИТЬ", by="record_deal",
                        )
                    except Exception as e:
                        logger.warning("record_deal: status change failed: %s", e)
                    # 3) Кладём в очередь fund_release (с дедупом — обновит или создаст)
                    try:
                        await storage.add_payout("fund_release", {
                            "card_id": cid,
                            "bank": c.get("bank") or "",
                            "fio": c.get("fio") or "",
                            "supplier": c.get("supplier") or "",
                            "work_chat_id": c.get("work_chat_id") or 0,
                            "amount_usdt": float(c.get("price_usdt") or 0),
                            "deal_id": deal_id,
                        })
                    except Exception as e:
                        logger.warning("record_deal: payout upsert failed: %s", e)
                    await self._refresh_lk_card_post(cid)
                    await self._post_action_reply_to_lk_card(cid)
                    moved_card_id = cid
                    break
            except Exception as e:
                logger.warning("record_deal: lk-card auto-move failed: %s", e)

        result = {"status": "ok", "deal_id": deal_id, "initial_status": "ПОПОЛНИТЬ"}
        if moved_card_id:
            result["lk_card_moved"] = moved_card_id
            result["lk_new_status"] = "ПОПОЛНИТЬ_И_ОТПУСТИТЬ"
        return result

    async def _tool_update_deal_status(self, deal_id: str = "", new_status: str = "") -> dict:
        ok = await storage.update_deal_status(deal_id, new_status)
        if not ok:
            return {"status": "error", "error": "deal_not_found_or_invalid", "deal_id": deal_id}
        d = storage.get_deal(deal_id) or {}
        logger.info("deal status updated: %s -> %s", deal_id, new_status)
        return {
            "status": "ok",
            "deal_id": deal_id,
            "new_status": new_status,
            "client_username": d.get("client_username"),
            "fio": d.get("fio"),
            "bank": d.get("bank"),
        }

    async def _tool_find_deal(self, deal_id: str = "", username: str = "", fio: str = "", bank: str = "") -> dict:
        if not any([deal_id, username, fio, bank]):
            return {"status": "error", "error": "no_query_params"}

        # 1) Ищем в реестре сделок (storage.deals)
        results = storage.find_deal_by(
            deal_id=deal_id or None,
            username=username or None,
            fio=fio or None,
            bank=bank or None,
        )
        clean = []
        for d in results:
            cd = {k: v for k, v in d.items() if k not in ("history", "created_at")}
            cd["source"] = "deal"
            clean.append(cd)

        # 2) ВСЕГДА также ищем в lk_cards — там сделки которые ещё не получили
        # номер (например GUARANTOR_AFTER_WORK до отработки) или вообще без сделки.
        # Без этого AI отвечает «не найдена» хотя карточка есть в системе.
        lk_results = []
        try:
            lk_results = storage.find_lk_card(
                bank=bank or None,
                fio=fio or None,
                supplier=username or None,
            ) or []
        except Exception as e:
            logger.warning("_tool_find_deal lk search failed: %s", e)

        # Если ищем по конкретному deal_id — фильтруем lk-карточки по deal_id внутри
        if deal_id and lk_results:
            did = (deal_id or "").lstrip("#").strip()
            lk_results = [
                c for c in lk_results
                if did and did in ((c.get("deal_id") or "").lstrip("#"))
            ]

        for c in lk_results:
            clean.append({
                "source": "lk_card",
                "card_id": c.get("card_id"),
                "deal_id": c.get("deal_id") or "",
                "fio": c.get("fio") or "",
                "bank": c.get("bank") or "",
                "client_username": c.get("supplier") or "",
                "amount_usdt": c.get("price_usdt") or 0,
                "payment_method": c.get("payment_method") or "",
                "status": c.get("status") or "",
            })

        return {
            "status": "ok",
            "found": len(clean),
            "deals": clean,
            "hint": (
                "Если найдена карточка (source=lk_card) — сделка УЖЕ в системе. "
                "Не говори клиенту «не найдена». Подтверди и при необходимости "
                "уточни номер сделки если payment_method это требует."
                if clean else
                "Ни в реестре сделок, ни в карточках ЛК ничего не нашлось."
            ),
        }

    async def _tool_create_lk_card(
        self,
        chat_id,
        bank: str = "",
        fio: str = "",
        price_usdt: float = 0.0,
        payment_method: str = "",
        deal_id: str = "",
        usdt_address: str = "",
    ) -> dict:
        """Создаёт карточку ЛК (анкету) в Группе 1 'Личные кабинеты'."""
        bank = (bank or "").strip()
        fio = (fio or "").strip()
        payment_method = (payment_method or "").strip().upper()
        deal_id = (deal_id or "").strip()
        usdt_address = (usdt_address or "").strip()

        if not bank:
            return {"status": "error", "error": "bank_required"}
        if not fio:
            return {"status": "error", "error": "fio_required"}
        if price_usdt <= 0:
            return {"status": "error", "error": "price_invalid"}
        if payment_method not in accounting2.PAYMENT_METHODS:
            return {
                "status": "error",
                "error": "payment_method_invalid",
                "allowed": list(accounting2.PAYMENT_METHODS),
            }
        if payment_method == "USDT_TRC20" and not usdt_address:
            return {"status": "error", "error": "usdt_address_required_for_usdt"}
        # GUARANTOR_AFTER_WORK — сделка создаётся ПОСЛЕ отработки клиентом,
        # на момент создания карточки её ещё нет, deal_id опционален.
        if (
            payment_method.startswith("GUARANTOR")
            and payment_method != "GUARANTOR_AFTER_WORK"
            and not deal_id
        ):
            return {"status": "error", "error": "deal_id_required_for_guarantor"}

        lk_group = storage.get_lk_group_id()
        if not lk_group:
            return {"status": "error", "error": "lk_group_not_set"}

        info = storage.get_chat_info(chat_id) or {}
        client_id = info.get("client_id") or ""
        client_username = info.get("client_username") or ""
        # Если username пуст в managed_chats — попробуем получить через Telethon
        if not client_username and client_id:
            try:
                ent = await self.client.get_entity(int(client_id))
                if getattr(ent, "username", None):
                    client_username = ent.username
                    # Заодно сохраним в managed_chats для будущего
                    await storage.set_chat_payment_info(
                        chat_id, client_username=client_username
                    )
            except Exception as e:
                logger.warning("create_lk_card: resolve username failed: %s", e)

        card_id = await storage.add_lk_card(
            supplier=client_username or "—",
            bank=bank,
            fio=fio,
            price_usdt=float(price_usdt),
            payment_method=payment_method,
            deal_id=deal_id,
            usdt_address=usdt_address,
            # Для GUARANTOR_BEFORE с уже заданным deal_id — статус
            # ОЖИДАЕТ_ПОПОЛНЕНИЯ (нам нужно пополнить сделку до старта работы).
            # Для остальных методов или GUARANTOR_BEFORE без deal_id — В_РАБОТЕ.
            status=(
                "ОЖИДАЕТ_ПОПОЛНЕНИЯ"
                if (payment_method or "").upper() == "GUARANTOR_BEFORE" and deal_id
                else "В_РАБОТЕ"
            ),
            client_id=client_id,
            client_username=client_username,
            work_chat_id=chat_id,
            created_by="ai_tool",
        )

        try:
            await self._refresh_lk_card_post(card_id)
        except Exception as e:
            logger.warning("refresh_lk_card_post failed for card=%s: %s", card_id, e)

        return {
            "status": "ok",
            "card_id": card_id,
            "lk_group_id": lk_group,
            "bank": bank,
            "fio": fio,
            "price_usdt": price_usdt,
            "payment_method": payment_method,
        }

    async def _apply_status_change(self, deal_id: str, new_status: str):
        """Внутренняя процедура: смена статуса сделки в storage и уведомление
        клиента в его work_chat. Видимость для команды теперь — через
        Группу 1 ЛК (карточки): deal_id хранится в карточке, статус меняет
        accounting_v2 при отчёте."""
        ok = await storage.update_deal_status(deal_id, new_status)
        if not ok:
            logger.warning("apply_status_change: deal %s not found in storage", deal_id)
            return
        deal = storage.get_deal(deal_id) or {}

        work_chat = deal.get("work_chat_id")
        client_msg = self._client_status_message(new_status, deal, deal_id=deal_id)
        if not work_chat or not client_msg:
            logger.info(
                "client notify skipped: deal=%s work_chat=%s msg=%r",
                deal_id, work_chat, bool(client_msg),
            )
            return
        try:
            target = await self._resolve_chat_target(work_chat)
            await self.client.send_message(target, client_msg, link_preview=False)
            logger.info("client notified deal=%s status=%s chat=%s", deal_id, new_status, work_chat)
        except Exception as e:
            logger.warning("client notify failed for deal=%s: %s", deal_id, e)

    @staticmethod
    def _client_status_message(status: str, deal: dict, deal_id: str = "") -> str:
        bank = deal.get("bank", "")
        did = f"#{deal_id}" if deal_id else ""
        if status == "ПОПОЛНЕНО":
            return f"Сделка {did} пополнена ({bank}), начинаем работу."
        if status == "В_РАБОТЕ":
            return f"Ваш аккаунт {did} ({bank}) в работе."
        if status == "ГОТОВО_К_ОТПУСКУ":
            return f"Сделка {did} ({bank}) почти готова к отпуску."
        if status == "ЗАВЕРШЕНА":
            return f"Сделка {did} завершена ({bank}), всё прошло успешно."
        if status == "ЗАБЛОКИРОВАН":
            return f"По сделке {did} ({bank}) есть нюансы — оператор разбирается."
        if status == "ОТМЕНА_СДЕЛКИ":
            return f"Сделка {did} ({bank}) приостановлена. Менеджер свяжется."
        return ""

    # === V2: Группа 1 «Личные кабинеты» (анкеты + БРАК/БЛОК) ===

    async def _handle_lk_group_message(self, event):
        """Анкета ЛК / команды БРАК / БЛОК в Группе 1."""
        if not event.message:
            return
        text = (event.message.text or "").strip()
        if not text:
            return
        if self._me and event.sender_id == self._me.id:
            return  # своё сообщение

        low = text.lower()

        # Reply работника-выплат на анкету ЛК (или на наш action-reply)
        # с любым текстом-пруфом («оплачено», скрин, «отпустил», и т.п.) →
        # карточка → ЗАВЕРШЁН + уведомить клиента в его work_chat.
        try:
            reply_to_id = None
            if getattr(event.message, "reply_to", None):
                reply_to_id = getattr(
                    event.message.reply_to, "reply_to_msg_id", None,
                )
            if reply_to_id:
                if await self._maybe_handle_lk_payment_proof(
                    event, int(reply_to_id), text,
                ):
                    return
        except Exception as e:
            logger.warning("lk payment proof check failed: %s", e)

        # Помощь — полный список команд Группы 1 ЛК
        if re.match(r"^\s*(?:помощь|справка|/help|/?\?|help)\s*$", text, re.I):
            await self._send_help_lk_group(event)
            return

        # Дневная сводка действий — работает и в Группе 1 ЛК (рядом с карточками)
        # и в Группе 2 Бухгалтерия. Один и тот же _handle_daily_summary.
        # Раскрыть детали конкретного банка: «детали Альфа» / «раскрой ОЗОН».
        m_bd = re.match(
            r"^\s*"
            r"(?:детал(?:ь|и|ьно)?|раскрой|раскрыть|раскрывай|"
            r"разверни|развернуть|разверн[иуь]\w*|"
            r"подробн(?:о|ее|ости))"
            r"\W*([\wа-яА-Я\-]*)",
            text, re.I,
        )
        if m_bd:
            bank_arg = (m_bd.group(1) or "").strip()
            if not bank_arg:
                try:
                    await event.reply(
                        "ℹ️ Нужно указать банк. Пример: <code>детали ОЗОН</code> "
                        "или <code>раскрой Альфа</code>.",
                        parse_mode="html",
                    )
                except Exception:
                    pass
                return
            await self._handle_bank_details(event, bank_arg)
            return
        # --- НОВЫЕ КОМАНДЫ ЛК (май 2026): 3 команды для движений средств ---
        # ВАЖНО: порядок — сначала самый специфичный паттерн.
        # 3) "что пополнить и отпустить" / "пополнить и отпустить" / "отпустить"
        if re.search(
            r"^\s*/?(?:что\s+)?(?:пополн\w*|оплат\w*)\s+и\s+отпуст",
            text, re.I,
        ) or re.search(
            r"^\s*/?отпуст(?:ить|и|ь)?\b",
            text, re.I,
        ):
            await self._handle_lk_cmd_to_release(event)
            return
        # 1) "что пополнить" / "пополнить" (без "и отпустить" — уже отработали выше)
        if re.search(
            r"^\s*/?(?:что\s+)?пополн\w+",
            text, re.I,
        ):
            await self._handle_lk_cmd_to_topup(event)
            return
        # 2) "что оплатить" / "оплатить"
        if re.search(
            r"^\s*/?(?:что\s+)?оплат\w+",
            text, re.I,
        ):
            await self._handle_lk_cmd_to_pay(event)
            return

        if re.search(
            r"^\s*(?:/?сводка|/?действия|/?список\s*действий|"
            r"дневн(?:ой|ая)\s+(?:отч[её]т|свод)|"
            r"кому\s+оплат)\b",
            text, re.I,
        ):
            await self._handle_daily_summary(event)
            return

        # Массовый импорт существующих ЛК
        if low.startswith("/import_lk") or low.startswith("импорт лк"):
            await self._apply_import_lk(event, text)
            return

        # Синхронизация клиентов: проходим по managed_chats, наполняем индекс
        # @username -> work_chat. Чините legacy-беседы где client_username пустой.
        if (
            low.startswith("/sync_clients")
            or low.startswith("/checkupidgroup")
            or low.startswith("синхронизация клиентов")
        ):
            await self._apply_sync_clients(event)
            return

        # Синхронизация карточек ЛК: восстановить storage.lk_cards из истории
        # сообщений Группы 1 (если state.json был потерян).
        if (
            low.startswith("/sync_lk")
            or low.startswith("/sync_cards")
            or low.startswith("синхронизация карточек")
            or low.startswith("синхронизация лк")
            or low.startswith("синхрони")
        ):
            # Опциональный аргумент: лимит сообщений
            m_lim = re.search(r"\b(\d+)\b", text)
            limit = int(m_lim.group(1)) if m_lim else 500
            limit = max(50, min(limit, 3000))
            await self._apply_sync_lk_cards(event, limit=limit)
            return

        # Команда удаления одной карточки (по #id, по банк+ФИО или reply на анкету)
        if await self._maybe_handle_delete_one_lk(event, text):
            return

        # Команда удаления всех карточек ЛК (с двойным подтверждением)
        if await self._maybe_handle_delete_all_lk(event, text):
            return

        # БРАК / БЛОК — короткие команды
        if low.startswith("брак"):
            cmd = accounting2.parse_brak_command(text)
            if cmd:
                await self._apply_brak_command(event, cmd)
            return
        if low.startswith("блок"):
            cmd = accounting2.parse_blok_command(text)
            if cmd:
                await self._apply_blok_command(event, cmd)
            return

        # Анкета (мульти-строка с банком/ценой/методом)
        if "\n" in text and ("банк" in low or "поставщик" in low):
            # Сначала — попытка bulk-парса (несколько анкет в одном сообщении,
            # разделённых строкой «Поставщик: …»).
            blocks = accounting2.split_lk_cards_text(text)
            if len(blocks) > 1:
                await self._apply_bulk_manual_lk_cards(event, blocks)
                return
            card_data = accounting2.parse_lk_card(text)
            if not card_data:
                await self._reply_lk_template_hint(
                    event,
                    "⚠️ Не понял формат карточки. Нужен минимум банк + цена/метод.",
                )
                return
            # ФИО обязательно — иначе автомат в Группе 2 не свяжет
            # output ЛК с этой карточкой (там оператор пишет банк + ФИО).
            if not (card_data.get("fio") or "").strip():
                await self._reply_lk_template_hint(
                    event,
                    "⚠️ Не нашёл ФИО держателя счёта.",
                    extra_hint=(
                        "Добавь строку:\n<pre>ФИО: Иванов Иван Иванович</pre>\n\n"
                        "Без ФИО автомат в Группе 2 не сможет связать заявку "
                        "с этой карточкой."
                    ),
                )
                return
            # GUARANTOR_BEFORE/AFTER требуют номер сделки (она уже создана)
            method = (card_data.get("payment_method") or "").upper()
            if method in ("GUARANTOR_BEFORE", "GUARANTOR_AFTER"):
                deal_id = (card_data.get("deal_id") or "").strip().lstrip("-").strip()
                if not deal_id or deal_id in ("-", "—"):
                    await self._reply_lk_template_hint(
                        event,
                        f"⚠️ Для метода <b>{method}</b> нужен номер сделки.",
                        extra_hint=(
                            "Добавь строку:\n<pre>Номер сделки: 12345</pre>\n\n"
                            "Если сделка создаётся ПОСЛЕ отработки — используй "
                            "метод <code>Сделка в конте (после отработки)</code> "
                            "и поставь <code>Номер сделки: -</code>"
                        ),
                    )
                    return
            # USDT_TRC20 требует адрес
            if method == "USDT_TRC20":
                addr = (card_data.get("usdt_address") or "").strip()
                if not addr:
                    await self._reply_lk_template_hint(
                        event,
                        "⚠️ Для метода <b>USDT TRC20</b> нужен адрес кошелька.",
                        extra_hint="Добавь строку:\n<pre>Адрес: TXxxxxxxxxxxxxxxxxx</pre>",
                    )
                    return
            # created_by: помечаем автора карточки (Тимон / админ / worker / …)
            card_data["_created_by"] = self._resolve_created_by_tag(event)
            await self._apply_manual_lk_card(event, card_data)
            return

        # Компактный однострочный формат: БАНК ФИО ЦЕНА МЕТОД [@username] [#deal_id|USDT]
        if "\n" not in text:
            compact = accounting2.parse_lk_card_compact(text)
            if compact:
                compact["_created_by"] = self._resolve_created_by_tag(event)
                await self._apply_manual_lk_card(event, compact)
                return

        # Edit карточки: «#lk044 сделка #12345», «#lk044 адрес TXxxx», и т.п.
        # Или reply на анкету в группе + просто «сделка #12345» / «адрес TX...».
        m_edit_id = re.match(r"^\s*#?(lk\d+)\s+(.+)$", text, re.I)
        if m_edit_id:
            cid_q = m_edit_id.group(1).lower()
            rest = m_edit_id.group(2)
            if await self._handle_lk_card_edit(event, rest, card_id=cid_q):
                return
        # Reply на анкету без явного #lkNNN — поле/значение в самом тексте
        if event.message and getattr(event.message, "reply_to", None):
            if await self._handle_lk_card_edit(event, text):
                return

    async def _apply_brak_command(self, event, cmd: dict):
        """БРАК — найти карточку → статус БРАК → уведомить клиента → если
        был гарант-deal, попросить отменить + написать в чат сделок."""
        cards = storage.find_lk_card(bank=cmd["bank"], fio=cmd["fio"])
        active = [c for c in cards if c.get("status") not in ("БРАК", "ЗАВЕРШЁН")]
        if not active:
            await event.reply(
                f"⚠️ Не нашёл активную карточку: <b>{cmd['bank']} {cmd['fio']}</b>.",
                parse_mode="html",
            )
            return
        card = active[0]
        cid = card["card_id"]
        await storage.set_lk_card_status(
            cid, "БРАК",
            brak_reason=cmd.get("reason", ""),
            by="lk_group",
        )
        await self._refresh_lk_card_post(cid)
        _e("lk-brak", {
            "card_id": cid, "bank": card.get("bank"),
            "fio": card.get("fio"),
            "reason": cmd.get("reason", ""),
        }, character="lk", severity="alert")

        # Уведомить клиента в work_chat
        wc = card.get("work_chat_id")
        msg_to_client = (
            f"⚠️ К сожалению, ваш ЛК <b>{card.get('bank')}</b> "
            f"({card.get('fio')}) не подошёл."
        )
        if cmd.get("reason"):
            msg_to_client += f"\n\n<b>Причина:</b> {cmd['reason']}"
        # Если был гарант-deal → попросить отменить
        deal_id = card.get("deal_id")
        method = card.get("payment_method", "")
        if deal_id and method.startswith("GUARANTOR"):
            msg_to_client += (
                f"\n\nПо вашей сделке #{deal_id} нужно отменить — "
                f"пришлите, пожалуйста, подтверждение из бота гаранта."
            )
        if wc:
            try:
                target = await self._resolve_chat_target(wc)
                await self.client.send_message(
                    target, msg_to_client, parse_mode="html", link_preview=False,
                )
            except Exception as e:
                logger.warning("brak notify client failed: %s", e)

        # В Группу 1 ЛК — уведомление об отмене сделки (если был гарант).
        # Тимон в этой группе участник — увидит и заберёт деньги в Conte.
        if deal_id and method.startswith("GUARANTOR"):
            lk_gid = storage.get_lk_group_id()
            if lk_gid:
                try:
                    target = await self._resolve_chat_target(lk_gid)
                    await self.client.send_message(
                        target,
                        f"❌ <b>Сделка #{deal_id} ОТМЕНЕНА</b> "
                        f"(БРАК ЛК {card.get('bank')} {card.get('fio')})\n"
                        f"⚠️ Нужно ЗАБРАТЬ ДЕНЬГИ с этой сделки. @TimonSkupCL",
                        parse_mode="html",
                    )
                except Exception as e:
                    logger.warning("brak deal-cancel notify failed: %s", e)

        await event.reply(
            f"✅ ЛК <b>{card.get('bank')}</b> ({card.get('fio')}) → <b>БРАК</b>.\n"
            f"Клиент уведомлён.",
            parse_mode="html",
        )

    async def _apply_blok_command(self, event, cmd: dict):
        """БЛОК — найти карточку → статус БЛОК + сумма + примечание →
        уведомить клиента + тэг Тимона."""
        cards = storage.find_lk_card(bank=cmd["bank"], fio=cmd["fio"])
        active = [c for c in cards if c.get("status") not in ("БРАК", "ЗАВЕРШЁН")]
        if not active:
            await event.reply(
                f"⚠️ Не нашёл активную карточку: <b>{cmd['bank']} {cmd['fio']}</b>.",
                parse_mode="html",
            )
            return
        card = active[0]
        cid = card["card_id"]
        await storage.set_lk_card_status(
            cid, "БЛОК",
            block_amount_rub=cmd.get("amount_rub", 0),
            block_note=cmd.get("note", ""),
            by="lk_group",
        )
        await self._refresh_lk_card_post(cid)
        _e("lk-blok", {
            "card_id": cid, "bank": card.get("bank"),
            "fio": card.get("fio"),
            "amount_rub": cmd.get("amount_rub", 0),
            "note": cmd.get("note", ""),
        }, character="lk", severity="alert")

        wc = card.get("work_chat_id")
        msg = (
            f"🚫 На ЛК <b>{card.get('bank')}</b> ({card.get('fio')}) "
            f"возник <b>БЛОК</b> на {accounting2._fmt_rub(cmd.get('amount_rub', 0))}."
        )
        if cmd.get("note"):
            msg += f"\n\n<b>Что нужно сделать:</b> {cmd['note']}"
        msg += "\n\n@TimonSkupCL — посмотри пожалуйста."
        if wc:
            try:
                target = await self._resolve_chat_target(wc)
                await self.client.send_message(
                    target, msg, parse_mode="html", link_preview=False,
                )
            except Exception as e:
                logger.warning("blok notify client failed: %s", e)

        await event.reply(
            f"✅ ЛК <b>{card.get('bank')}</b> ({card.get('fio')}) → <b>БЛОК</b> "
            f"{accounting2._fmt_rub(cmd.get('amount_rub', 0))}.",
            parse_mode="html",
        )

    def _resolve_work_chat_by_supplier(self, supplier: str) -> dict:
        """Ищет work_chat по @username поставщика (= username клиента в managed_chats).
        Возвращает {work_chat_id, client_id, client_username} или {}.

        Сначала через обратный индекс client_username_index (O(1)),
        затем fallback линейным проходом по managed_chats — на случай,
        если индекс ещё не наполнен (старые беседы до /sync_clients)."""
        if not supplier:
            return {}
        target = supplier.lstrip("@").lower().strip()
        if not target:
            return {}

        def _build(chat_key, info):
            try:
                wc_id = int(chat_key)
            except (TypeError, ValueError):
                wc_id = chat_key
            return {
                "work_chat_id": wc_id,
                "client_id": int(info.get("client_id") or 0),
                "client_username": info.get("client_username") or target,
            }

        # 1) Быстрый путь: обратный индекс
        idx_key = storage.find_chat_by_client_username(target)
        if idx_key:
            info = storage.get_chat_info(idx_key) or {}
            if info:
                return _build(idx_key, info)

        # 2) Fallback: линейный поиск (на случай если индекс пуст)
        for chat_key in storage.get_managed_chat_ids():
            info = storage.get_chat_info(chat_key) or {}
            uname = (info.get("client_username") or "").lstrip("@").lower().strip()
            if uname and uname == target:
                return _build(chat_key, info)
        return {}

    # ID Тимона для определения created_by при ручном создании карточек.
    TIMON_USER_ID = 397572312

    # Команды takeover — взять чат под AI / отпустить
    _AI_CMD_TAKEOVER_RE = re.compile(
        r"^\s*ассистент[,\s]+"
        r"(?:возьми|работай|веди)\s+"
        r"(?:этот\s+чат\s+|тут\s+|здесь\s+|в\s+этом\s+чате\s+)?"
        r"(?:для\s+|с\s+)?"
        r"@?(\w+)\b",
        re.I | re.M,
    )
    _AI_CMD_FORGET_RE = re.compile(
        r"^\s*ассистент[,\s]+(?:забудь|перестань|стоп|выйди)"
        r"(?:\s+этот\s+чат|\s+тут|\s+здесь|\s+отсюда)?\s*$",
        re.I | re.M,
    )

    async def _maybe_handle_takeover_command(
        self, event, chat_id, chat_info: Optional[dict],
    ) -> bool:
        """Команды в любой группе с юзерботом:
          'Ассистент возьми этот чат для @nick' / 'Ассистент работай тут с @nick'
                — регистрирует чат как managed_chat для клиента @nick.
          'Ассистент забудь этот чат' — удаляет чат из managed_chats.
        Доступно только админам и worker'ам. Возвращает True если обработано."""
        text = (event.message and event.message.text) or ""
        if not text:
            return False

        # Проверка авторизации
        try:
            sender_id = int(event.sender_id) if event.sender_id else 0
        except Exception:
            sender_id = 0
        try:
            sender = await event.get_sender()
        except Exception:
            sender = None
        sender_username = (getattr(sender, "username", "") or "").lower()
        is_admin = sender_id in (storage.get_admins() or [])
        is_worker = sender_username and sender_username in {
            w.lower() for w in storage.get_workers()
        }
        if not (is_admin or is_worker):
            # Команды не от уполномоченного — игнорируем
            return False

        # FORGET
        if self._AI_CMD_FORGET_RE.search(text):
            if not chat_info:
                await event.reply("ℹ️ Этот чат и так не под AI.", parse_mode="html")
                return True
            await self._cmd_forget_chat(event, chat_id)
            return True

        # TAKEOVER
        m = self._AI_CMD_TAKEOVER_RE.search(text)
        if not m:
            return False
        username = m.group(1).strip().lstrip("@")
        if not username:
            return False
        await self._cmd_takeover_chat(event, chat_id, username, chat_info)
        return True

    async def _cmd_takeover_chat(
        self, event, chat_id, username: str, chat_info: Optional[dict],
    ):
        """Регистрирует чат как managed_chat для клиента @username.
        Не делает API-вызовов к Claude — AI сам подтянет историю при первом
        своём ответе клиенту через iter_messages."""
        try:
            user = await self.client.get_entity(username)
        except UsernameNotOccupiedError:
            await event.reply(f"⚠️ Пользователь @{username} не существует.")
            return
        except Exception as e:
            await event.reply(
                f"⚠️ Не нашёл @{username}: <code>{e}</code>", parse_mode="html",
            )
            return

        client_id = int(getattr(user, "id", 0) or 0)
        if not client_id:
            await event.reply(f"⚠️ Не смог получить ID пользователя @{username}.")
            return
        client_name = (
            (getattr(user, "first_name", "") or "")
            + " "
            + (getattr(user, "last_name", "") or "")
        ).strip() or f"@{username}"
        client_username = (getattr(user, "username", "") or username).lstrip("@")

        # Если чат уже в managed_chats — обновляем клиента
        action = "обновил" if chat_info else "взял под AI"
        await storage.register_chat(
            chat_id, client_id, client_name, client_username,
        )
        # Отметим welcome_sent=True чтобы юзербот не слал welcome задним числом
        try:
            await storage.mark_welcome_sent(chat_id)
        except Exception:
            pass

        # Сохраним кто взял для аудита (через payment_info — без отдельного метода)
        try:
            await storage.set_chat_payment_info(chat_id, client_username=client_username)
        except Exception:
            pass

        await event.reply(
            f"✅ {action.capitalize()}: клиент <b>@{client_username}</b> "
            f"({client_name}).\n"
            f"Дальше AI отвечает на его сообщения как в обычной рабочей беседе.\n\n"
            f"<i>Если AI выключен — включи в /admin → 🧠 AI.</i>",
            parse_mode="html",
        )
        logger.info(
            "ai cmd takeover: chat=%s client=%s/@%s by sender=%s",
            chat_id, client_id, client_username, event.sender_id,
        )

    async def _cmd_forget_chat(self, event, chat_id):
        """Удаляет чат из managed_chats — AI перестаёт там отвечать.
        Само сообщение/историю не трогаем."""
        try:
            removed = await storage.remove_managed_chat(chat_id)
        except Exception as e:
            await event.reply(
                f"⚠️ Не смог удалить чат из managed: <code>{e}</code>",
                parse_mode="html",
            )
            return
        if removed:
            await event.reply(
                "🔇 Этот чат больше не под AI. Сообщения клиентов AI не подхватывает.",
                parse_mode="html",
            )
            logger.info("ai cmd forget: chat=%s by sender=%s", chat_id, event.sender_id)
        else:
            await event.reply("ℹ️ Этот чат и так не был под AI.")

    # Команды AI от админов: 'Ассистент добавь @user' / 'Ассистент выдай админку @user'
    # Допускаем любой хвост после @username ("в беседу", "в чат", "сюда" и т.п.) —
    # граница слова \b вместо $. Команда должна начинаться с «ассистент»
    # (^ + re.M), но что после username — игнорируем.
    _AI_CMD_ADD_RE = re.compile(
        r"^\s*ассистент[,\s]+добавь\s+@?(\w+)\b",
        re.I | re.M,
    )
    _AI_CMD_GRANT_ADMIN_RE = re.compile(
        r"^\s*ассистент[,\s]+(?:выдай|дай|сделай)\s+админ(?:ку|а|ом|ину)?\s+@?(\w+)\b",
        re.I | re.M,
    )

    # Anti-spam: для одного chat_id отправляем только ОДИН раз за 10 минут
    _last_perevyaz_ask: dict = {}

    async def _auto_ask_payment_method_after_perevyaz(self, event, chat_id, chat_info):
        """После «✅ Перевязка ЛК <банк> успешно выполнена» сами тегаем клиента
        и спрашиваем метод оплаты. Раньше эту роль играл AI, но он не
        триггерился на сообщения от ботов."""
        from storage import _norm_chat_id as _norm
        key = _norm(chat_id)
        # Anti-spam: 10 минут защиты от повтора
        last = self._last_perevyaz_ask.get(key, 0)
        if last and time.time() - last < 600:
            return
        # Уже задан метод оплаты? — не спрашиваем
        if (chat_info.get("payment_method") or "").strip():
            return
        self._last_perevyaz_ask[key] = time.time()
        client_uname = (chat_info.get("client_username") or "").lstrip("@")
        client_id = chat_info.get("client_id") or 0
        client_tag = (
            f"<a href='tg://user?id={client_id}'>👋</a> "
            if client_id else (f"@{client_uname} " if client_uname else "")
        )
        msg = (
            f"{client_tag}<b>Перевязка успешно завершена.</b>\n\n"
            f"Подскажите — как хотите получить выплату по этому ЛК?\n\n"
            f"💸 <b>USDT TRC20</b> — пришлите ваш TRC20-адрес, "
            f"переведём сразу после отработки счёта операционистами\n"
            f"🤝 <b>Гарант в Continental</b> (сделка с @PRIDE_CL)\n"
            f"   • <b>сейчас</b> — мы пополним сделку, дальше работаем со счётом, "
            f"отпускаем после отработки\n"
            f"   • <b>после отработки</b> — пополним и отпустим по факту "
            f"завершения работы со счётом"
        )
        try:
            target = await self._resolve_chat_target(chat_id)
            await self.client.send_message(
                target, msg, parse_mode="html", link_preview=False,
            )
            logger.info(
                "AUTO-ASK payment method: chat=%s client=@%s",
                chat_id, client_uname,
            )
            _e("auto-ask-payment-method", {
                "chat_id": chat_id, "client_username": client_uname,
            }, character="chat", severity="info")
        except Exception as e:
            logger.warning("auto-ask payment method send fail: %s", e)

    async def _maybe_handle_ai_admin_command(
        self, event, text: str, chat_id,
    ) -> bool:
        """Команды админов в managed-чате клиента (work_chat):
          'Ассистент добавь @nick'        — пригласить пользователя в чат
          'Ассистент выдай админку @nick' — выдать админ-права в чате
          '/checkchatforLKCARD'           — найти данные перевязки в истории
                                            этого чата и создать карточку ЛК
                                            (если её ещё нет)
          '/checklk'                      — короткий алиас

        Работает только в managed_chats (рабочие беседы клиентов) — чтобы не
        случилось что админ напишет такое в Группе 1 ЛК и юзербот добавит
        туда левого пользователя. Возвращает True если команда обработана."""
        if not storage.get_chat_info(chat_id):
            # Не managed-чат — команды AI не действуют.
            return False
        m = self._AI_CMD_ADD_RE.search(text)
        if m:
            await self._cmd_invite_user(event, chat_id, m.group(1))
            return True
        m = self._AI_CMD_GRANT_ADMIN_RE.search(text)
        if m:
            await self._cmd_grant_admin(event, chat_id, m.group(1))
            return True
        # /checkchatforLKCARD или /checklk — восстановление карточки из контекста
        text_low = text.lower().strip()
        if (text_low.startswith("/checkchatforlkcard")
                or text_low.startswith("/checklk")):
            await self._cmd_check_chat_for_lk_card(event, chat_id)
            return True
        return False

    async def _handle_checkchat_brain_command(self, event, text: str):
        """Запуск /checkchatforLKCARD <chat_id> из брейн-чата (или любого
        не-managed чата). Резолвит chat_id из аргумента, потом вызывает
        обычную логику сканирования."""
        # Парсим chat_id из аргумента
        parts = (text or "").strip().split()
        if len(parts) < 2:
            try:
                await event.reply(
                    "⚠️ Используй: <code>/checkchatforLKCARD &lt;chat_id&gt;</code>\n"
                    "Или просто <code>/checkchatforLKCARD</code> прямо в work_chat клиента — "
                    "там chat_id берётся автоматически.",
                    parse_mode="html",
                )
            except Exception:
                pass
            return
        try:
            target_chat_id = int(parts[1])
        except ValueError:
            try:
                await event.reply(
                    f"⚠️ chat_id должен быть числом. Получил: <code>{parts[1]}</code>",
                    parse_mode="html",
                )
            except Exception:
                pass
            return
        # Проверяем что чат есть в managed_chats (мы можем его читать)
        info = storage.get_chat_info(target_chat_id)
        if not info:
            try:
                await event.reply(
                    f"⚠️ Чат <code>{target_chat_id}</code> не найден в managed_chats. "
                    f"Возможно chat_id указан неверно (должен быть отрицательным -100xxx) "
                    f"или беседа не зарегистрирована.",
                    parse_mode="html",
                )
            except Exception:
                pass
            return
        # Делаем shim-event с подменённым chat_id для существующей логики
        class _ShimEvent:
            def __init__(self, real_event, target):
                self._real = real_event
                self.chat_id = target
                # reply отправляет в брейн-чат
                self.reply = real_event.reply
        shim = _ShimEvent(event, target_chat_id)
        try:
            await event.reply(
                f"⏳ Сканирую чат <code>{target_chat_id}</code> "
                f"({info.get('client_name') or '—'})...",
                parse_mode="html",
            )
        except Exception:
            pass
        await self._cmd_check_chat_for_lk_card(shim, target_chat_id)

    async def _cmd_check_chat_for_lk_card(self, event, chat_id):
        """Сканирует историю чата (последние 200 сообщений) и пытается
        восстановить карточку ЛК если она не была создана автоматически.

        Что ищем в истории:
          • «✅ Перевязка ЛК <банк> успешно выполнена» / «ЛК <банк> перевязан»
          • «Карточка: #lkXXX» — если карточка уже есть, выходим
          • ФИО клиента (из chat_info или supplier)
          • Банк (из перевяз-сообщения)
          • Цена (упоминание $/USDT)
          • Метод оплаты (USDT / гарант / сделка)
          • USDT-адрес (TX... 34 chars)
          • Номер сделки (deal_id 5-7 цифр)
        """
        try:
            await event.reply("⏳ Сканирую историю чата для восстановления карточки ЛК...")
        except Exception:
            pass
        # Параметры из chat_info
        info = storage.get_chat_info(chat_id) or {}
        client_username = (info.get("client_username") or "").lstrip("@").strip()
        client_id = info.get("client_id") or 0
        # Поиск в истории
        bank = None
        fio = None
        price_usdt = None
        payment_method = None
        usdt_addr = None
        deal_id = None
        existing_card_id = None
        msg_count = 0
        try:
            async for m in self.client.iter_messages(chat_id, limit=200):
                msg_count += 1
                txt = ((m.text or m.message) or "").strip()
                if not txt:
                    continue
                low = txt.lower()
                # 1) Если уже есть карточка — выходим
                em = re.search(r"#?(lk\d{3,4})\b", txt, re.I)
                if em and ("карточк" in low or "лк перевяз" in low):
                    existing_card_id = em.group(1).lower()
                    break
                # 2) Перевязка → банк
                if not bank:
                    pem = re.search(
                        r"перевязк[аи]\s+лк\s+([а-яёa-z\d-]+)",
                        low,
                    )
                    if pem:
                        bank = pem.group(1).upper()
                    else:
                        pem2 = re.search(
                            r"лк\s+([а-яёa-z\d-]+)\s+перевязан",
                            low,
                        )
                        if pem2:
                            bank = pem2.group(1).upper()
                # 3) Цена $
                if not price_usdt:
                    pr = re.search(r"\b(\d{2,4})\s*\$", txt)
                    if pr:
                        try:
                            price_usdt = float(pr.group(1))
                        except Exception:
                            pass
                # 4) Метод оплаты
                if not payment_method:
                    if any(t in low for t in ("usdt", "trc20", "трц20")):
                        payment_method = "USDT_TRC20"
                    elif any(t in low for t in ("гарант до", "до отработ", "до перевяз")):
                        payment_method = "GUARANTOR_BEFORE"
                    elif any(t in low for t in ("гарант после", "после отработ")):
                        payment_method = "GUARANTOR_AFTER_WORK"
                # 5) USDT-адрес TRX (T + 33 alphanum)
                if not usdt_addr:
                    ua = re.search(r"\bT[A-HJ-NP-Za-km-z1-9]{33}\b", txt)
                    if ua:
                        usdt_addr = ua.group(0)
                # 6) Номер сделки
                if not deal_id:
                    dm = re.search(r"#(\d{5,7})\b", txt)
                    if dm:
                        deal_id = dm.group(1)
                # 7) ФИО — берём из chat_info.client_name если есть
                if not fio and info.get("client_name"):
                    fio = info["client_name"]
        except Exception as e:
            try:
                await event.reply(f"⚠️ Ошибка сканирования: {e}")
            except Exception:
                pass
            return

        if existing_card_id:
            try:
                await event.reply(
                    f"ℹ️ В чате уже есть карточка <code>#{existing_card_id}</code>. "
                    f"Восстановление не требуется. Если она потерялась — используй "
                    f"<code>#{existing_card_id}</code> в брейн-чате для проверки.",
                    parse_mode="html",
                )
            except Exception:
                pass
            return

        if not bank:
            try:
                await event.reply(
                    f"⚠️ Не нашёл признак успешной перевязки в последних "
                    f"{msg_count} сообщениях. Карточка не создана.\n"
                    f"Ожидаемые маркеры: «✅ Перевязка ЛК <банк> успешно выполнена», "
                    f"«ЛК <банк> перевязан». Если перевязка была — создай карточку вручную.",
                )
            except Exception:
                pass
            return

        if not fio:
            fio = (client_username and f"@{client_username}") or "—"

        if not price_usdt:
            # Попробовать из прайса
            try:
                prices = storage.get_lk_prices() or {}
                price_usdt = float(prices.get(bank.lower()) or 0)
            except Exception:
                price_usdt = 0

        # СОХРАНЯЕМ pending — клиент должен подтвердить.
        from storage import _norm_chat_id
        chat_key = _norm_chat_id(chat_id)
        method_label = {
            "USDT_TRC20": "USDT TRC20",
            "GUARANTOR_BEFORE": "Сделка в Continental (ДО отработки)",
            "GUARANTOR_AFTER_WORK": "Сделка в Continental (ПОСЛЕ отработки)",
            "GUARANTOR_AFTER": "Сделка в Continental (после перевязки)",
        }.get(payment_method or "", payment_method or "уточняется")

        self._pending_lk_card_confirm[chat_key] = {
            "bank": bank,
            "fio": fio,
            "price_usdt": float(price_usdt or 0),
            "payment_method": payment_method or "",
            "deal_id": deal_id or "",
            "usdt_address": usdt_addr or "",
            "client_username": client_username or "",
            "client_id": int(client_id or 0),
            "requested_at": time.time(),
            "expires_at": time.time() + 24 * 60 * 60,  # 24 часа
        }

        # Отправляем клиенту сообщение с запросом подтверждения
        client_tag = (
            f"<a href='tg://user?id={client_id}'>{fio}</a>"
            if client_id else (f"@{client_username}" if client_username else "Клиент")
        )
        confirm_msg = (
            f"📋 <b>Мне нужно создать карточку вашего ЛК.</b>\n\n"
            f"{client_tag}, вот информация которую я собрал из нашего чата:\n\n"
            f"• <b>Банк:</b> {bank}\n"
            f"• <b>ФИО:</b> {fio}\n"
            f"• <b>Цена ЛК:</b> {price_usdt}$\n"
            f"• <b>Метод оплаты:</b> {method_label}\n"
        )
        if deal_id:
            confirm_msg += f"• <b>Номер сделки:</b> #{deal_id}\n"
        if usdt_addr:
            confirm_msg += f"• <b>USDT-адрес:</b> <code>{usdt_addr}</code>\n"
        confirm_msg += (
            f"\nПодскажите — <b>всё верно?</b>\n"
            f"Если да — напишите «<b>да</b>» / «верно» / «подтверждаю».\n"
            f"Если нет — напишите что исправить."
        )

        try:
            target = await self._resolve_chat_target(chat_id)
            await self.client.send_message(
                target, confirm_msg, parse_mode="html", link_preview=False,
            )
        except Exception as e:
            logger.warning("checkchat confirm send fail: %s", e)
        try:
            _e("lk-card-confirm-requested", {
                "chat_id": chat_id, "bank": bank, "fio": fio,
                "price_usdt": price_usdt, "method": payment_method,
            }, character="lk", severity="info")
        except Exception:
            pass

    async def _cmd_invite_user(self, event, chat_id, username: str):
        """Приглашение пользователя в чат по @username (от админа)."""
        uname = (username or "").lstrip("@").strip()
        if not uname:
            await event.reply(
                "⚠️ Не понял ник. Используй: <code>Ассистент добавь @username</code>",
                parse_mode="html",
            )
            return
        try:
            user = await self.client.get_entity(uname)
        except UsernameNotOccupiedError:
            await event.reply(f"⚠️ Пользователь @{uname} не существует в Telegram.")
            return
        except Exception as e:
            await event.reply(f"⚠️ Не нашёл @{uname}: <code>{e}</code>", parse_mode="html")
            return
        try:
            await self.client(InviteToChannelRequest(chat_id, [user]))
            await event.reply(
                f"✅ Пригласил <b>@{uname}</b> в этот чат.",
                parse_mode="html",
            )
            logger.info("ai cmd: invited @%s into chat=%s by sender=%s",
                        uname, chat_id, event.sender_id)
        except UserAlreadyParticipantError:
            await event.reply(f"ℹ️ @{uname} уже в этом чате.")
        except UserPrivacyRestrictedError:
            await event.reply(
                f"⚠️ У <b>@{uname}</b> настройки приватности не позволяют его пригласить. "
                f"Попроси его сначала написать боту/в этот чат, потом повтори команду.",
                parse_mode="html",
            )
        except FloodWaitError as e:
            await event.reply(f"⚠️ FloodWait {e.seconds}s — попробуй позже.")
        except Exception as e:
            await event.reply(
                f"⚠️ Не смог пригласить @{uname}: <code>{e}</code>",
                parse_mode="html",
            )
            logger.warning("ai cmd invite failed: chat=%s user=%s err=%s",
                           chat_id, uname, e)

    async def _cmd_grant_admin(self, event, chat_id, username: str):
        """Выдача админ-прав пользователю в чате (от админа)."""
        uname = (username or "").lstrip("@").strip()
        if not uname:
            await event.reply(
                "⚠️ Не понял ник. Используй: <code>Ассистент выдай админку @username</code>",
                parse_mode="html",
            )
            return
        try:
            user = await self.client.get_entity(uname)
        except UsernameNotOccupiedError:
            await event.reply(f"⚠️ Пользователь @{uname} не существует в Telegram.")
            return
        except Exception as e:
            await event.reply(f"⚠️ Не нашёл @{uname}: <code>{e}</code>", parse_mode="html")
            return
        try:
            rights = ChatAdminRights(
                change_info=True,
                post_messages=False,
                edit_messages=True,
                delete_messages=True,
                ban_users=True,
                invite_users=True,
                pin_messages=True,
                add_admins=False,
                anonymous=False,
                manage_call=True,
            )
            await self.client(EditAdminRequest(
                channel=chat_id, user_id=user, admin_rights=rights, rank="Admin",
            ))
            await event.reply(
                f"✅ Выдал админку <b>@{uname}</b>.",
                parse_mode="html",
            )
            logger.info("ai cmd: granted admin to @%s in chat=%s by sender=%s",
                        uname, chat_id, event.sender_id)
        except FloodWaitError as e:
            await event.reply(f"⚠️ FloodWait {e.seconds}s — попробуй позже.")
        except Exception as e:
            await event.reply(
                f"⚠️ Не смог выдать админку @{uname}: <code>{e}</code>",
                parse_mode="html",
            )
            logger.warning("ai cmd grant_admin failed: chat=%s user=%s err=%s",
                           chat_id, uname, e)

    def _resolve_created_by_tag(self, event) -> str:
        """Кто создал карточку — для history в storage и для аудита.
        Возвращает 'manual:tymon' / 'manual:admin' / 'manual:<sid>' / 'manual'."""
        try:
            sid = int(event.sender_id) if event.sender_id else 0
        except Exception:
            sid = 0
        if sid == self.TIMON_USER_ID:
            return "manual:tymon"
        if sid and sid in (storage.get_admins() or []):
            return "manual:admin"
        if sid:
            return f"manual:{sid}"
        return "manual"

    async def _reply_lk_template_hint(self, event, header: str, extra_hint: str = ""):
        """Отвечает на сообщение с подсказкой формата анкеты ЛК.
        Используется когда parse_lk_card вернул None или нашлась мелкая ошибка."""
        template = (
            "<pre>Поставщик: @nickname\n"
            "Банк: Альфа\n"
            "ФИО: Иванов Иван Иванович\n"
            "Цена: 400\n"
            "Метод оплаты: Сделка в конте (после отработки)\n"
            "Номер сделки: -</pre>\n\n"
            "<i>Варианты метода:</i>\n"
            "• <code>Сделка в конте (после отработки)</code> — сделка создаётся "
            "ПОСЛЕ отработки счёта, в номере сделки ставится <code>-</code>\n"
            "• <code>Сделка в конте (до отработки)</code> — сделка уже создана "
            "ДО перевязки, нужен номер\n"
            "• <code>USDT TRC20</code> — выплата на адрес после отработки, "
            "нужна строка <code>Адрес: T...</code>"
        )
        body = f"{header}\n\n"
        if extra_hint:
            body += extra_hint + "\n\n"
        body += "Шаблон карточки:\n\n" + template
        try:
            await event.reply(body, parse_mode="html", link_preview=False)
        except Exception as e:
            logger.warning("template hint reply failed: %s", e)

    # Команда удаления ОДНОЙ карточки ЛК — без двойного подтверждения.
    # Варианты:
    #   1) «Ассистент удали ЛК #lk010» — по card_id
    #   2) «Ассистент удали ЛК АЛЬФА Иванов» — по банк+ФИО (если карточка одна)
    #   3) Reply на анкету в Группе 1 + «удалить» / «Ассистент удали»
    _AI_CMD_DELETE_LK_BY_ID_RE = re.compile(
        r"^\s*(?:ассистент[,\s]+)?(?:удали(?:ть)?|сотри|сотрите|снеси|снести)\s+"
        r"(?:лк|карточк[уи]|анкет[уы])?\s*"
        r"#?(lk\d+)\s*$",
        re.I | re.M,
    )
    _AI_CMD_DELETE_LK_BY_BANKFIO_RE = re.compile(
        r"^\s*(?:ассистент[,\s]+)?(?:удали(?:ть)?|сотри|сотрите|снеси|снести)\s+"
        r"(?:лк|карточк[уи]|анкет[уы])\s+"
        r"(?!все\b|всю\b|всех\b|всё\b|базу\b)"
        r"(.+)$",
        re.I | re.M,
    )
    _AI_CMD_DELETE_REPLY_RE = re.compile(
        r"^\s*(?:ассистент[,\s]*)?(?:удали(?:ть)?|сотри|сотрите|снеси|снести)"
        r"(?:\s+эт[ау]\s+(?:анкету|карточку|лк))?\s*$",
        re.I | re.M,
    )

    async def _maybe_handle_delete_one_lk(self, event, text: str) -> bool:
        """Удаление одной карточки ЛК. Доступно админам/Тимону/работникам."""
        try:
            sender_id = int(event.sender_id) if event.sender_id else 0
        except Exception:
            sender_id = 0
        try:
            sender = await event.get_sender()
        except Exception:
            sender = None
        sender_username = (getattr(sender, "username", "") or "").lower()
        is_admin = sender_id in (storage.get_admins() or [])
        is_tymon = sender_id == self.TIMON_USER_ID
        is_worker = sender_username and sender_username in {
            w.lower() for w in storage.get_workers()
        }
        if not (is_admin or is_tymon or is_worker):
            return False

        # 1) По #id
        m = self._AI_CMD_DELETE_LK_BY_ID_RE.search(text)
        if m:
            card_id = m.group(1).lower().strip()
            await self._do_delete_one_lk(event, card_id, reason="по id")
            return True

        # 2) Reply на анкету + слово «удалить»
        if (
            event.message and getattr(event.message, "reply_to", None)
            and self._AI_CMD_DELETE_REPLY_RE.match(text)
        ):
            reply_to = getattr(event.message.reply_to, "reply_to_msg_id", None)
            if reply_to:
                # Найти карточку по lk_group_msg_id
                cards = storage.list_lk_cards() or {}
                target_id = None
                for cid, c in cards.items():
                    if int(c.get("lk_group_msg_id") or 0) == int(reply_to):
                        target_id = cid
                        break
                if target_id:
                    await self._do_delete_one_lk(event, target_id, reason="reply на анкету")
                    return True
                # Reply есть, но не на анкету — не наша команда
                return False

        # 3) По банк+ФИО
        m = self._AI_CMD_DELETE_LK_BY_BANKFIO_RE.search(text)
        if m:
            rest = m.group(1).strip()
            parts = rest.split(maxsplit=1)
            if len(parts) >= 2:
                bank, fio = parts[0], parts[1]
                found = storage.find_lk_card(bank=bank, fio=fio) or []
                if not found:
                    await event.reply(
                        f"⚠️ Не нашёл карточку: <b>{bank} {fio}</b>",
                        parse_mode="html",
                    )
                    return True
                if len(found) > 1:
                    ids = ", ".join(f"#{c.get('card_id')}" for c in found[:5])
                    await event.reply(
                        f"⚠️ Найдено {len(found)} карточек: {ids}. "
                        f"Уточни командой <code>Ассистент удали ЛК #lkXXX</code>.",
                        parse_mode="html",
                    )
                    return True
                target_id = found[0].get("card_id")
                if target_id:
                    await self._do_delete_one_lk(event, target_id, reason=f"банк+ФИО")
                    return True

        return False

    async def _do_delete_one_lk(self, event, card_id: str, reason: str = ""):
        """Удаляет карточку:
          1. Из storage.
          2. Сообщение с анкетой (lk_group_msg_id) из Группы 1.
          3. Если карта была в массовом импорте — зачёркивает строку в сводке.
        """
        card = storage.get_lk_card(card_id) or {}
        if not card:
            await event.reply(
                f"⚠️ Карточка <code>#{card_id}</code> не найдена.",
                parse_mode="html",
            )
            return
        bank = card.get("bank") or "—"
        fio = card.get("fio") or "—"
        lk_group_msg_id = card.get("lk_group_msg_id") or 0
        import_summary_msg_id = card.get("import_summary_msg_id") or 0

        try:
            ok = await storage.delete_lk_card(card_id)
        except Exception as e:
            await event.reply(
                f"⚠️ Ошибка удаления <code>#{card_id}</code>: <code>{e}</code>",
                parse_mode="html",
            )
            return
        if not ok:
            await event.reply(
                f"⚠️ Карточка <code>#{card_id}</code> уже отсутствует.",
                parse_mode="html",
            )
            return

        # 2. Удалить сообщение с анкетой
        chat_for_delete = event.chat_id
        if lk_group_msg_id:
            try:
                target = await self._resolve_chat_target(chat_for_delete)
                await self.client.delete_messages(target, [int(lk_group_msg_id)])
                logger.info(
                    "delete_one_lk: card=%s anketa msg=%s deleted",
                    card_id, lk_group_msg_id,
                )
            except Exception as e:
                logger.warning(
                    "delete_one_lk: cannot delete anketa msg=%s: %s",
                    lk_group_msg_id, e,
                )

        # 3. Зачеркнуть строку в сводке массового импорта (если есть)
        if import_summary_msg_id:
            try:
                await self._strike_summary_line(
                    chat_for_delete, int(import_summary_msg_id), card_id,
                )
            except Exception as e:
                logger.warning(
                    "delete_one_lk: strike summary failed for card=%s: %s",
                    card_id, e,
                )

        suffix = f" ({reason})" if reason else ""
        await event.reply(
            f"🗑 Удалена карточка <code>#{card_id}</code> — "
            f"<b>{bank} {fio}</b>{suffix}.",
            parse_mode="html",
        )
        logger.info(
            "delete_one_lk: card_id=%s by sender=%s reason=%s anketa_msg=%s summary=%s",
            card_id, event.sender_id, reason,
            lk_group_msg_id, import_summary_msg_id,
        )
        _e("lk-deleted", {
            "card_id": card_id, "bank": bank, "fio": fio,
            "reason": reason,
        }, character="lk", severity="warning")

    async def _strike_summary_line(
        self, chat_id, summary_msg_id: int, card_id: str,
    ):
        """В HTML сводки массового импорта находит строку с #card_id и
        оборачивает её в <s>…</s> + добавляет «❌ удалена». Затем edit_message."""
        summary = storage.get_import_summary(summary_msg_id)
        if not summary or not summary.get("html"):
            return
        html = summary["html"]
        # Маркер: строка содержит <code>#card_id</code>
        marker = f"<code>#{card_id}</code>"
        if marker not in html:
            return
        lines = html.split("\n")
        new_lines = []
        for ln in lines:
            if marker in ln and "<s>" not in ln:
                # Зачеркнём только саму строку с маркером.
                # Сохраняем начальные эмодзи/символы (✅/🔗/⚠️/❌) перед маркером.
                new_lines.append(f"<s>{ln}</s> ❌ <i>удалена</i>")
            else:
                new_lines.append(ln)
        new_html = "\n".join(new_lines)
        try:
            target = await self._resolve_chat_target(chat_id)
            await self.client.edit_message(
                target, summary_msg_id, new_html,
                parse_mode="html", link_preview=False,
            )
        except Exception as e:
            logger.warning(
                "strike_summary edit_message failed (msg=%s): %s",
                summary_msg_id, e,
            )
            return
        await storage.update_import_summary_html(summary_msg_id, new_html)

    # Команда удаления всех ЛК — деструктивная, требует двойного «+»
    # от Тимона (id 397572312) и от любого админа (`storage.get_admins()`).
    _AI_CMD_DELETE_ALL_LK_RE = re.compile(
        r"^\s*ассистент[,\s]+"
        r"(?:удали(?:ть)?|очисти(?:ть)?|сотри|сотрите|обнули|обнулить|"
        r"стереть|снеси|снести)\s+"
        r"(?:все|всю|всех|всё|базу)\s*"
        r"(?:лк|карточк[уи]|анкет[ыу]|кабинет[ыов]*)?\s*$",
        re.I | re.M,
    )
    _AI_CMD_DELETE_CANCEL_RE = re.compile(
        r"^\s*ассистент[,\s]+(?:отмена|стоп\s+удаления?|отмени(?:ть)?)",
        re.I | re.M,
    )
    _PENDING_TTL_SEC = 600  # 10 минут на сбор подтверждений

    async def _maybe_handle_delete_all_lk(self, event, text: str) -> bool:
        """Обработка команды «Ассистент удалить все ЛК» и подтверждений «+»."""
        from storage import _norm_chat_id
        chat_key = _norm_chat_id(event.chat_id)

        try:
            sender_id = int(event.sender_id) if event.sender_id else 0
        except Exception:
            sender_id = 0

        # Команда отмены — снимает pending если есть
        if self._AI_CMD_DELETE_CANCEL_RE.search(text):
            if chat_key in self._pending_delete_all_lk:
                self._pending_delete_all_lk.pop(chat_key, None)
                try:
                    await event.reply(
                        "🚫 Запрос на удаление ВСЕХ ЛК отменён.",
                        parse_mode="html",
                    )
                except Exception:
                    pass
                return True
            return False

        # Команда инициации удаления
        if self._AI_CMD_DELETE_ALL_LK_RE.search(text):
            # Только админ или Тимон может инициировать
            is_admin = sender_id in (storage.get_admins() or [])
            is_tymon = sender_id == self.TIMON_USER_ID
            if not (is_admin or is_tymon):
                await event.reply(
                    "🚫 Удаление ЛК доступно только админам и Тимону.",
                    parse_mode="html",
                )
                return True

            total = len(storage.list_lk_cards() or {})
            if total == 0:
                await event.reply("ℹ️ База ЛК уже пустая.", parse_mode="html")
                return True

            # Создаём pending
            msg_text = (
                f"⚠️ <b>Запрос на удаление ВСЕХ карточек ЛК</b> ({total} шт.)\n\n"
                f"Запросил: <code>{sender_id}</code>\n\n"
                "Для выполнения нужны <b>два +</b>:\n"
                "• от <b>@TimonSkupCL</b>\n"
                "• от <b>админа</b> юзербота\n\n"
                "Напишите <code>+</code> в reply на это сообщение, чтобы подтвердить.\n"
                "Или <code>Ассистент отмена</code> чтобы отменить.\n\n"
                f"<i>TTL подтверждения: 10 минут.</i>"
            )
            try:
                sent = await event.reply(
                    msg_text, parse_mode="html", link_preview=False,
                )
            except Exception as e:
                logger.warning("delete_all_lk request send failed: %s", e)
                return True
            sent_id = getattr(sent, "id", None) or 0
            self._pending_delete_all_lk[chat_key] = {
                "msg_id": int(sent_id),
                "requested_by": sender_id,
                "approved_by": set(),
                "expires_at": time.time() + self._PENDING_TTL_SEC,
                "total": total,
            }
            return True

        # «+» (или «плюс») — подтверждение от уполномоченного
        if re.match(r"^\s*\+\s*$", text):
            pending = self._pending_delete_all_lk.get(chat_key)
            if not pending:
                return False
            # TTL
            if time.time() > pending.get("expires_at", 0):
                self._pending_delete_all_lk.pop(chat_key, None)
                try:
                    await event.reply(
                        "⌛ Запрос истёк (10 мин). Повтори команду заново.",
                        parse_mode="html",
                    )
                except Exception:
                    pass
                return True
            # Проверка reply'я на нужное сообщение (опционально, не обязательно)
            # Главное — sender уполномочен и не подтверждал ранее
            is_admin = sender_id in (storage.get_admins() or [])
            is_tymon = sender_id == self.TIMON_USER_ID
            if not (is_admin or is_tymon):
                await event.reply(
                    "🚫 Подтвердить может только админ или Тимон.",
                    parse_mode="html",
                )
                return True
            # Категория подтверждения
            cat = "tymon" if is_tymon else "admin"
            approved = pending["approved_by"]
            if cat in approved:
                await event.reply(
                    f"ℹ️ Ты уже подтверждал ({cat}). Жду подтверждение от "
                    f"{'админа' if cat == 'tymon' else 'Тимона'}.",
                    parse_mode="html",
                )
                return True
            approved.add(cat)
            # Готово?
            if "tymon" in approved and "admin" in approved:
                total = pending.get("total", 0)
                self._pending_delete_all_lk.pop(chat_key, None)
                try:
                    n = await storage.delete_all_lk_cards()
                    await event.reply(
                        f"🗑 <b>Удалено</b>: {n} карточек ЛК. База очищена.",
                        parse_mode="html",
                    )
                    logger.info(
                        "delete_all_lk: %d cards removed (approved by tymon+admin)",
                        n,
                    )
                except Exception as e:
                    await event.reply(
                        f"⚠️ Ошибка удаления: <code>{e}</code>",
                        parse_mode="html",
                    )
                    logger.exception("delete_all_lk failed: %s", e)
                return True
            else:
                missing = "Тимона" if "tymon" not in approved else "админа"
                await event.reply(
                    f"✅ {cat} подтвердил. Жду подтверждение от <b>{missing}</b>.",
                    parse_mode="html",
                )
                return True

        return False

    async def _apply_bulk_manual_lk_cards(self, event, blocks: list):
        """Массовый ввод: несколько анкет в одном сообщении, разделённые
        строкой «Поставщик: …». Каждый блок — отдельная карточка.
        В конце шлёт сводку (добавлено N, ошибок M).
        """
        created_by_tag = self._resolve_created_by_tag(event)
        ok_lines: list = []
        ok_card_ids: list = []
        skip_lines: list = []
        for block in blocks:
            card_data = accounting2.parse_lk_card(block)
            if not card_data:
                first_line = block.splitlines()[0][:60] if block else "?"
                skip_lines.append(
                    f"❌ <code>{first_line}…</code> — формат не распознан"
                )
                continue
            if not (card_data.get("fio") or "").strip():
                first_line = block.splitlines()[0][:60] if block else "?"
                skip_lines.append(
                    f"❌ <code>{first_line}…</code> — нет ФИО"
                )
                continue
            method = (card_data.get("payment_method") or "").upper()
            if method in ("GUARANTOR_BEFORE", "GUARANTOR_AFTER"):
                deal_id = (card_data.get("deal_id") or "").strip().lstrip("-").strip()
                if not deal_id or deal_id in ("-", "—"):
                    skip_lines.append(
                        f"❌ {card_data.get('bank')} {card_data.get('fio')} — "
                        f"для {method} нужен номер сделки"
                    )
                    continue
            # USDT адрес — желательно но не обязательно для bulk (можно дозаполнить позже)
            card_data["_created_by"] = created_by_tag
            # Резолв work_chat по supplier — то же что в обычном _apply_manual_lk_card
            supplier = (card_data.get("supplier") or "").lstrip("@")
            if supplier and not card_data.get("work_chat_id"):
                resolved = self._resolve_work_chat_by_supplier(supplier)
                if resolved:
                    card_data.update(resolved)
            try:
                created_by = card_data.pop("_created_by", None) or "manual"
                card_id = await storage.add_lk_card(
                    **card_data, created_by=created_by,
                )
            except Exception as e:
                skip_lines.append(
                    f"❌ {card_data.get('bank')} {card_data.get('fio')} — "
                    f"ошибка: <code>{e}</code>"
                )
                continue
            # Постим саму анкету как reply
            try:
                card = storage.get_lk_card(card_id) or {}
                rendered = accounting2.format_lk_card(card)
                sent = await event.reply(
                    rendered, parse_mode="html", link_preview=False,
                )
                sent_id = getattr(sent, "id", None)
                if sent_id:
                    await storage.set_lk_card_msg_id(card_id, int(sent_id))
            except Exception as e:
                logger.warning("bulk_lk: card %s post failed: %s", card_id, e)
            ok_lines.append(
                f"✅ <code>#{card_id}</code> {card_data.get('bank')} "
                f"{card_data.get('fio')}"
            )
            ok_card_ids.append(card_id)
            # Чтобы Telegram не словил FloodWait при большом количестве:
            await asyncio.sleep(0.2)

        report = f"📦 <b>Массовый импорт ЛК</b>\n\n"
        if ok_lines:
            report += f"✅ Добавлено: <b>{len(ok_lines)}</b>\n"
            report += "\n".join(ok_lines[:30])
            if len(ok_lines) > 30:
                report += f"\n<i>… и ещё {len(ok_lines) - 30}</i>"
        if skip_lines:
            if ok_lines:
                report += "\n\n"
            report += f"⚠️ Пропущено: <b>{len(skip_lines)}</b>\n"
            report += "\n".join(skip_lines[:10])
            if len(skip_lines) > 10:
                report += f"\n<i>… и ещё {len(skip_lines) - 10}</i>"
        try:
            sent_summary = await event.reply(
                report, parse_mode="html", link_preview=False,
            )
            summary_msg_id = getattr(sent_summary, "id", None)
            if summary_msg_id:
                await self._save_import_summary_links(
                    summary_msg_id, event.chat_id, report, ok_card_ids,
                )
        except Exception as e:
            logger.warning("bulk_lk summary reply failed: %s", e)

    async def _apply_manual_lk_card(self, event, card_data: dict):
        """Менеджер вручную создал анкету — сохраняем в storage и сразу
        публикуем сам шаблон анкеты (а не короткое подтверждение)."""
        # Если в карточке указан @поставщик — попробуем привязать
        # её к work_chat клиента (нужно для авто-тегания при отработке).
        supplier = (card_data.get("supplier") or "").lstrip("@")
        if supplier and not card_data.get("work_chat_id"):
            resolved = self._resolve_work_chat_by_supplier(supplier)
            if resolved:
                card_data = {**card_data, **resolved}
                logger.info(
                    "manual_lk_card: linked work_chat=%s for supplier=@%s",
                    resolved.get("work_chat_id"), supplier,
                )
            else:
                logger.warning(
                    "manual_lk_card: no work_chat for supplier=@%s — "
                    "auto-tag on отработка будет недоступен",
                    supplier,
                )
        # Извлекаем _created_by (внутреннее поле — кто создал карточку),
        # чтобы не передавать его в add_lk_card как поле модели.
        created_by = card_data.pop("_created_by", None) or "manual"
        card_id = await storage.add_lk_card(**card_data, created_by=created_by)
        _e("lk-created", {
            "card_id": card_id,
            "bank": card_data.get("bank"),
            "fio": card_data.get("fio"),
            "method": card_data.get("payment_method"),
            "source": created_by,
        }, character="lk", severity="success")
        # Получаем готовый рендер анкеты
        card = storage.get_lk_card(card_id) or {}
        text = accounting2.format_lk_card(card)
        # Публикуем анкету как reply на исходное сообщение оператора
        sent = None
        try:
            sent = await event.reply(text, parse_mode="html", link_preview=False)
        except Exception as e:
            logger.warning("manual_lk_card reply failed: %s", e)
        # Сохраняем msg_id шаблона (а НЕ исходной команды) — чтобы
        # _refresh_lk_card_post мог редактировать ту же анкету.
        sent_id = getattr(sent, "id", None)
        if sent_id:
            await storage.set_lk_card_msg_id(card_id, int(sent_id))

    async def _apply_import_lk(self, event, text: str):
        """Массовый импорт существующих ЛК.

        Формат сообщения:
            /import_lk
            АЛЬФА Иванов 400 USDT_TRC20 @ivanov TX...
            ОЗОН Петров 300 GUARANTOR_AFTER_WORK - @petrov

        Каждая строка после команды парсится как compact-формат и подвязывается
        к work_chat по @поставщику (через managed_chats).
        """
        lines = [ln.strip() for ln in (text or "").splitlines()]
        rows = [ln for ln in lines[1:] if ln]
        if not rows:
            usage = (
                "ℹ️ <b>Массовый импорт ЛК</b>\n\n"
                "Формат:\n"
                "<pre>/import_lk\n"
                "АЛЬФА Иванов Иван 400 USDT_TRC20 @ivanov TXxxx\n"
                "ОЗОН Петров 300 GUARANTOR_AFTER_WORK - @petrov\n"
                "ТОЧКА Сидоров 250 GUARANTOR_AFTER #12345 @sidorov</pre>\n"
                "Каждая строка = одна карточка ЛК.\n"
                "@поставщик нужен чтобы привязать карточку к рабочей беседе клиента."
            )
            try:
                await event.reply(usage, parse_mode="html", link_preview=False)
            except Exception:
                pass
            return

        ok_lines: list = []
        ok_card_ids: list = []
        skip_lines: list = []
        for raw in rows:
            parsed = accounting2.parse_lk_card_compact(raw)
            if not parsed:
                skip_lines.append(f"❌ <code>{raw[:80]}</code> — формат не распознан")
                continue
            supplier = (parsed.get("supplier") or "").lstrip("@")
            resolved = (
                self._resolve_work_chat_by_supplier(supplier) if supplier else {}
            )
            if resolved:
                parsed.update(resolved)
            try:
                card_id = await storage.add_lk_card(**parsed, created_by="import")
            except Exception as e:
                skip_lines.append(f"❌ <code>{raw[:80]}</code> — ошибка: {e}")
                continue
            # Постим анкету в Группу 1
            try:
                card = storage.get_lk_card(card_id) or {}
                rendered = accounting2.format_lk_card(card)
                sent = await event.reply(
                    rendered, parse_mode="html", link_preview=False,
                )
                sent_id = getattr(sent, "id", None)
                if sent_id:
                    await storage.set_lk_card_msg_id(card_id, int(sent_id))
            except Exception as e:
                logger.warning("import_lk reply failed for %s: %s", card_id, e)
            mark = "🔗" if resolved else "⚠️"
            note = "" if resolved else " <i>(work_chat не найден)</i>"
            sup_disp = _fmt_username(supplier, fallback="—")
            ok_lines.append(
                f"{mark} <code>#{card_id}</code> {parsed.get('bank')} "
                f"{parsed.get('fio')} → {sup_disp}{note}"
            )
            ok_card_ids.append(card_id)

        report = "📦 <b>Импорт ЛК завершён</b>\n\n"
        if ok_lines:
            report += f"✅ Добавлено: <b>{len(ok_lines)}</b>\n"
            report += "\n".join(ok_lines[:30])
            if len(ok_lines) > 30:
                report += f"\n<i>… и ещё {len(ok_lines) - 30}</i>"
        if skip_lines:
            if ok_lines:
                report += "\n\n"
            report += f"⚠️ Пропущено: <b>{len(skip_lines)}</b>\n"
            report += "\n".join(skip_lines[:10])
            if len(skip_lines) > 10:
                report += f"\n<i>… и ещё {len(skip_lines) - 10}</i>"
        try:
            sent_summary = await event.reply(
                report, parse_mode="html", link_preview=False,
            )
            sm_id = getattr(sent_summary, "id", None)
            if sm_id:
                await self._save_import_summary_links(
                    sm_id, event.chat_id, report, ok_card_ids,
                )
        except Exception as e:
            logger.warning("import_lk summary failed: %s", e)

    async def _save_import_summary_links(
        self, summary_msg_id: int, chat_id, html_text: str, card_ids: list,
    ):
        """Связывает сводку с каждой картой: при удалении одной из них
        юзербот сможет найти summary, зачеркнуть строку и сделать edit."""
        try:
            await storage.save_import_summary(
                summary_msg_id, chat_id, html_text, card_ids,
            )
        except Exception as e:
            logger.warning("save_import_summary failed: %s", e)
            return
        for cid in card_ids:
            try:
                await storage.update_lk_card(
                    cid, import_summary_msg_id=int(summary_msg_id),
                )
            except Exception as e:
                logger.warning(
                    "set import_summary_msg_id for %s failed: %s", cid, e,
                )

    async def _apply_sync_lk_cards(self, event, limit: int = 500):
        """Сканирует Группу 1 ЛК на N последних сообщений и восстанавливает
        отсутствующие карточки в storage.lk_cards. Полезно после потери
        state.json — все карточки можно вернуть из истории сообщений бота.
        """
        lk_gid = storage.get_lk_group_id()
        if not lk_gid:
            try:
                await event.reply("⚠️ lk_group_id не настроен.")
            except Exception:
                pass
            return

        try:
            await event.reply(
                f"🔄 Сканирую последние <b>{limit}</b> сообщений Группы 1 ЛК…",
                parse_mode="html", link_preview=False,
            )
        except Exception:
            pass

        scanned = 0
        parsed = 0
        created = 0
        updated = 0
        errors = 0
        try:
            target = await self._resolve_chat_target(lk_gid)
        except Exception as e:
            try:
                await event.reply(f"⚠️ Не смог resolve lk_group: {e}")
            except Exception:
                pass
            return

        # Идём по сообщениям от старых к новым чтобы lk_cards_seq в storage
        # обновился правильно (последняя карточка = последний seq).
        messages_buffer = []
        try:
            async for msg in self.client.iter_messages(target, limit=limit):
                scanned += 1
                if not msg or not getattr(msg, "text", None):
                    continue
                text = msg.text or ""
                # Быстрый отбор: должна быть строка "ЛК #lkN"
                if "ЛК" not in text or "#lk" not in text.lower():
                    continue
                messages_buffer.append((msg.id, text))
        except Exception as e:
            logger.warning("sync_lk_cards: iter_messages failed: %s", e)
            errors += 1

        # Сортируем по msg.id ASC — старые → новые
        messages_buffer.sort(key=lambda t: t[0])

        for msg_id, text in messages_buffer:
            card = accounting2.parse_rendered_lk_card(text)
            if not card or not card.get("card_id"):
                continue
            parsed += 1
            cid = card["card_id"]
            existing = storage.get_lk_card(cid)
            card["lk_group_msg_id"] = msg_id
            try:
                if existing:
                    await storage.update_lk_card(cid, **card)
                    # Перепривяжем msg_id отдельно (на случай если update_lk_card
                    # не трогает служебные поля)
                    await storage.set_lk_card_msg_id(cid, msg_id)
                    updated += 1
                else:
                    await storage.restore_lk_card(cid, card)
                    created += 1
            except Exception as e:
                logger.warning("sync_lk_cards: card %s failed: %s", cid, e)
                errors += 1

        try:
            await event.reply(
                (
                    f"✅ Синхронизация ЛК завершена\n\n"
                    f"📨 Просканировано сообщений: <b>{scanned}</b>\n"
                    f"🔍 Распознано как карточки: <b>{parsed}</b>\n"
                    f"➕ Создано (новые): <b>{created}</b>\n"
                    f"🔄 Обновлено (msg_id привязан): <b>{updated}</b>\n"
                    f"⚠️ Ошибок: <b>{errors}</b>\n\n"
                    "Теперь команды <code>#lkNNN ...</code> снова работают, "
                    "карточки можно редактировать."
                ),
                parse_mode="html", link_preview=False,
            )
        except Exception:
            pass
        try:
            _e("sync-lk-cards", {
                "scanned": scanned, "parsed": parsed,
                "created": created, "updated": updated, "errors": errors,
            }, severity="success" if not errors else "warning")
        except Exception:
            pass

    async def _apply_sync_clients(self, event):
        """Синхронизация client_username по всем managed_chats:
          1. Если client_username пустой — резолвим через get_entity(client_id).
          2. Наполняем обратный индекс client_username -> chat_id.
          3. Идём от старых бесед к новым (по created_at), чтобы у клиентов
             с несколькими беседами в индексе осталась самая свежая.
        """
        chat_ids = storage.get_managed_chat_ids()
        # Сортируем по created_at ASC — самая свежая беседа окажется в индексе последней.
        items = []
        for key in chat_ids:
            info = storage.get_chat_info(key) or {}
            items.append((info.get("created_at", 0), key, info))
        items.sort(key=lambda t: t[0])

        try:
            await event.reply(
                f"🔄 Синхронизирую <b>{len(items)}</b> рабочих чатов…",
                parse_mode="html", link_preview=False,
            )
        except Exception:
            pass

        resolved_n = 0
        already_n = 0
        no_username_n = 0
        errors_n = 0
        bullets: list = []
        for _, key, info in items:
            client_id = int(info.get("client_id") or 0)
            current = (info.get("client_username") or "").lstrip("@").strip()
            uname = current
            if not uname and client_id:
                try:
                    ent = await self.client.get_entity(client_id)
                    uname = (getattr(ent, "username", None) or "").lstrip("@").strip()
                except Exception as e:
                    errors_n += 1
                    logger.warning("sync_clients: get_entity %s failed: %s", client_id, e)
                    continue
            if not uname:
                no_username_n += 1
                continue
            ok = await storage.update_client_username(key, uname)
            if ok and not current:
                resolved_n += 1
                bullets.append(f"🆕 <code>{key}</code> → @{uname}")
            elif ok:
                resolved_n += 1
            else:
                already_n += 1

        report = (
            "✅ <b>Синхронизация завершена</b>\n\n"
            f"Всего чатов: <b>{len(items)}</b>\n"
            f"Привязано/обновлено: <b>{resolved_n}</b>\n"
            f"Уже актуальны: <b>{already_n}</b>\n"
            f"Без username: <b>{no_username_n}</b>\n"
            f"Ошибок резолва: <b>{errors_n}</b>"
        )
        if bullets:
            report += "\n\n<b>Резолвлено впервые:</b>\n" + "\n".join(bullets[:30])
            if len(bullets) > 30:
                report += f"\n<i>… и ещё {len(bullets) - 30}</i>"
        try:
            await event.reply(report, parse_mode="html", link_preview=False)
        except Exception as e:
            logger.warning("sync_clients summary failed: %s", e)

    # === Редактирование карточки ЛК ===
    # Команды можно отправлять либо с явным #lkNNN в начале, либо как
    # reply на анкету. В обоих случаях парсим поле + значение и обновляем.
    _LK_EDIT_FIELD_PATTERNS = [
        (re.compile(
            r"^\s*(?:сделка|deal|номер\s*сделки|deal_id)\s*[:\-]?\s*#?(\S+)\s*$",
            re.I,
        ), "deal_id"),
        (re.compile(
            r"^\s*(?:адрес|usdt|trc20|wallet|кошел[её]к)\s*[:\-]?\s*(\S+)\s*$",
            re.I,
        ), "usdt_address"),
        (re.compile(
            r"^\s*(?:цена|price)\s*[:\-]?\s*([\d.,]+)\s*\$?\s*$",
            re.I,
        ), "price_usdt"),
        (re.compile(
            r"^\s*(?:метод(?:\s*оплаты)?|способ\s*оплаты|payment)\s*[:\-]?\s*(.+?)\s*$",
            re.I,
        ), "payment_method"),
        (re.compile(
            r"^\s*(?:банк|bank)\s*[:\-]?\s*(.+?)\s*$",
            re.I,
        ), "bank"),
        (re.compile(
            r"^\s*(?:ф\.?и\.?о\.?|fio|holder)\s*[:\-]?\s*(.+?)\s*$",
            re.I,
        ), "fio"),
        (re.compile(
            r"^\s*(?:поставщик|supplier|клиент)\s*[:\-]?\s*@?(\S+)\s*$",
            re.I,
        ), "supplier"),
    ]
    _LK_FIELD_LABELS = {
        "deal_id": "номер сделки",
        "usdt_address": "USDT-адрес",
        "price_usdt": "цена",
        "payment_method": "метод оплаты",
        "bank": "банк",
        "fio": "ФИО",
        "supplier": "поставщик",
    }

    def _parse_lk_field_update(self, text: str):
        """Парсит «поле: значение» — возвращает (field, raw_value) или None."""
        if not text:
            return None
        clean = text.strip()
        if not clean:
            return None
        for pat, field in self._LK_EDIT_FIELD_PATTERNS:
            m = pat.match(clean)
            if m:
                return field, m.group(1).strip()
        return None

    async def _handle_lk_card_edit(
        self, event, payload_text: str, card_id: Optional[str] = None,
    ) -> bool:
        """Применяет правку поля к карточке. Возвращает True если что-то
        изменилось (или ошибка отвечена пользователю). False — текст не
        распознан как edit (передать выше по цепочке хендлеров)."""
        # Если card_id не передан — пробуем reply на анкету
        if not card_id and event.message and getattr(event.message, "reply_to", None):
            reply_to = getattr(event.message.reply_to, "reply_to_msg_id", None)
            if reply_to:
                cards = storage.list_lk_cards() or {}
                for cid, c in cards.items():
                    if int(c.get("lk_group_msg_id") or 0) == int(reply_to):
                        card_id = cid
                        break
        if not card_id:
            return False
        parsed = self._parse_lk_field_update(payload_text)
        if not parsed:
            return False
        field, raw_val = parsed
        # Подготовка значения
        update: dict = {}
        if field == "deal_id":
            v = raw_val.lstrip("#").strip()
            if v in ("-", "—", ""):
                v = ""
            update["deal_id"] = v
        elif field == "usdt_address":
            v = raw_val.strip()
            if v in ("-", "—"):
                v = ""
            update["usdt_address"] = v
        elif field == "price_usdt":
            try:
                update["price_usdt"] = float(raw_val.replace(",", ".").rstrip("$"))
            except ValueError:
                await event.reply(
                    f"⚠️ Не понял цену: <code>{raw_val}</code>",
                    parse_mode="html",
                )
                return True
        elif field == "payment_method":
            normalized = accounting2._normalize_method(raw_val)
            if not normalized:
                await event.reply(
                    f"⚠️ Не понял метод: <code>{raw_val}</code>. "
                    "Допустимы: USDT_TRC20 / GUARANTOR_BEFORE / GUARANTOR_AFTER / "
                    "GUARANTOR_AFTER_WORK или фразы «сделка в конте (до/после отработки)».",
                    parse_mode="html",
                )
                return True
            update["payment_method"] = normalized
            # Явная админская правка метода — разрешаем перезапись guard'а в storage.
            update["_allow_payment_method_change"] = True
        elif field in ("bank", "fio"):
            update[field] = raw_val.strip()
        elif field == "supplier":
            update["supplier"] = raw_val.lstrip("@").strip()
        if not update:
            return False
        ok = await storage.update_lk_card(card_id, **update)
        if not ok:
            await event.reply(
                f"⚠️ Карточка <code>#{card_id}</code> не найдена.",
                parse_mode="html",
            )
            return True
        await self._refresh_lk_card_post(card_id)
        label = self._LK_FIELD_LABELS.get(field, field)
        await event.reply(
            f"✅ <code>#{card_id}</code> · {label} обновлён.",
            parse_mode="html",
        )
        logger.info(
            "lk_card_edit: card=%s field=%s value=%r by=%s",
            card_id, field, update.get(field), event.sender_id,
        )

        # Спец-логика: задан deal_id для ОТРАБОТАН + GUARANTOR_AFTER_WORK →
        # 1) переводим статус в ПОПОЛНИТЬ_И_ОТПУСТИТЬ (это полноценный статус)
        # 2) кладём в очередь fund_release (с дедупом).
        # Цикл статусов: В_РАБОТЕ → ОТРАБОТАН → ПОПОЛНИТЬ_И_ОТПУСТИТЬ → ЗАВЕРШЁН.
        if field == "deal_id" and update["deal_id"]:
            try:
                card = storage.get_lk_card(card_id) or {}
                if (card.get("status") in ("ОТРАБОТАН", "ПОПОЛНИТЬ_И_ОТПУСТИТЬ")
                        and (card.get("payment_method") or "").upper()
                            == "GUARANTOR_AFTER_WORK"):
                    try:
                        await storage.set_lk_card_status(
                            card_id, "ПОПОЛНИТЬ_И_ОТПУСТИТЬ", by="manual_edit_deal_id",
                        )
                    except Exception as e:
                        logger.warning("status change after deal_id edit: %s", e)
                    try:
                        await storage.add_payout("fund_release", {
                            "card_id": card_id,
                            "bank": card.get("bank") or "",
                            "fio": card.get("fio") or "",
                            "supplier": card.get("supplier") or "",
                            "work_chat_id": card.get("work_chat_id") or 0,
                            "amount_usdt": float(card.get("price_usdt") or 0),
                            "deal_id": update["deal_id"],
                        })
                    except Exception as e:
                        logger.warning("payout upsert (manual deal_id): %s", e)
                    await self._refresh_lk_card_post(card_id)
                    await self._post_action_reply_to_lk_card(card_id)
            except Exception as e:
                logger.warning(
                    "auto payout upsert after deal_id edit failed: %s", e,
                )

        # Спец-логика: задан usdt_address для ОТРАБОТАН + USDT_TRC20 →
        # повторный reply на анкету с инструкцией (теперь адрес есть).
        if field == "usdt_address" and update["usdt_address"]:
            try:
                card = storage.get_lk_card(card_id) or {}
                if (card.get("status") == "ОТРАБОТАН"
                        and (card.get("payment_method") or "").upper()
                            == "USDT_TRC20"):
                    await self._post_action_reply_to_lk_card(card_id)
            except Exception as e:
                logger.warning(
                    "post-action reply after usdt_address edit failed: %s", e,
                )
        return True

    async def _post_action_reply_to_lk_card(self, card_id: str) -> bool:
        """Кидает reply на анкету ЛК в Группе 1 с инструкцией для работника-
        выплат — какое следующее действие нужно. Вызывается после смены
        статуса на ОТРАБОТАН/ПОПОЛНИТЬ_И_ОТПУСТИТЬ.

        Возвращает True если reply отправлен."""
        card = storage.get_lk_card(card_id) or {}
        if not card:
            return False
        msg_id = int(card.get("lk_group_msg_id") or 0)
        lk_gid = storage.get_lk_group_id()
        if not lk_gid or not msg_id:
            return False
        status = card.get("status") or ""
        method = (card.get("payment_method") or "").upper()
        deal_id = (card.get("deal_id") or "").strip().lstrip("#")
        usdt_addr = (card.get("usdt_address") or "").strip()
        price = accounting2._fmt_usdt(card.get("price_usdt", 0))

        text = ""
        if status == "ПОПОЛНИТЬ_И_ОТПУСТИТЬ" and deal_id:
            text = (
                f"💎 <b>ПОПОЛНИТЬ И ОТПУСТИТЬ</b> сделку "
                f"<code>#{deal_id}</code>\n"
                f"Сумма: <b>{price}</b>"
            )
        elif status == "ОТРАБОТАН":
            if method == "USDT_TRC20":
                if usdt_addr:
                    text = (
                        f"💸 <b>ОПЛАТИТЕ USDT TRC20</b>\n"
                        f"Адрес: <code>{usdt_addr}</code>\n"
                        f"Сумма: <b>{price}</b>"
                    )
                else:
                    text = (
                        "⚠️ <b>USDT TRC20</b> — адрес не задан.\n"
                        "Дополни командой <code>адрес TX...</code> в reply "
                        "на эту анкету (или <code>#" + card_id + " адрес TX...</code>)"
                    )
            elif method in ("GUARANTOR_BEFORE", "GUARANTOR_AFTER") and deal_id:
                text = (
                    f"🔓 <b>ОТПУСТИТЕ</b> сделку <code>#{deal_id}</code>\n"
                    f"Сумма: <b>{price}</b>"
                )
            elif method == "GUARANTOR_AFTER_WORK":
                if deal_id:
                    text = (
                        f"💎 <b>ПОПОЛНИТЬ И ОТПУСТИТЬ</b> сделку "
                        f"<code>#{deal_id}</code>"
                    )
                else:
                    text = (
                        "⏳ <b>Ждём создания сделки клиентом.</b>\n"
                        "Когда придёт номер — статус изменится автоматически."
                    )
            else:
                text = (
                    f"⚠️ Метод <code>{method or '—'}</code> — действие неясно."
                )
        if not text:
            return False
        try:
            target = await self._resolve_chat_target(lk_gid)
            sent = await self.client.send_message(
                target, text, reply_to=msg_id,
                parse_mode="html", link_preview=False,
            )
            # Сохраняем msg_id action-reply в карточку — чтобы пруф от работника
            # (reply «оплачено» на наш action-reply) тоже находил карточку.
            sent_id = getattr(sent, "id", None)
            if sent_id:
                try:
                    await storage.update_lk_card(
                        card_id, post_action_reply_msg_id=int(sent_id),
                    )
                except Exception as e:
                    logger.warning(
                        "save post_action_reply_msg_id failed: %s", e,
                    )
            logger.info(
                "post action reply to lk_card=%s status=%s method=%s",
                card_id, status, method,
            )
            return True
        except Exception as e:
            logger.warning(
                "post action reply to lk_card=%s failed: %s",
                card_id, e,
            )
            return False

    async def _resolve_work_chat_for_card(self, card: dict) -> Optional[int]:
        """Резолвит work_chat_id для карточки ЛК, даже если поле пустое.
        Порядок:
          1) card.work_chat_id (если есть)
          2) card.client_username → managed_chats (find_chat_by_client_username)
          3) Поиск среди managed_chats по совпадению ФИО
          4) Поиск среди сделок (deal.work_chat_id) по client_username/fio

        Если резолв сработал и в карточке поле пустое — сохраняет найденное
        обратно в карточку.
        """
        if not card:
            return None
        card_id = card.get("card_id") or card.get("id") or ""
        # 1) Уже есть
        wc = card.get("work_chat_id")
        if wc:
            try:
                return int(wc)
            except Exception:
                return None
        resolved = None
        # 2) Через client_username
        uname = (card.get("client_username") or "").lstrip("@").strip()
        if uname:
            try:
                wc_key = storage.find_chat_by_client_username(uname)
                if wc_key:
                    resolved = int(wc_key)
            except Exception as e:
                logger.debug("resolve_wc: by username %s failed: %s", uname, e)
        # 2b) Через supplier (= username клиента в managed_chats для нашего флоу)
        if not resolved:
            sup = (card.get("supplier") or "").lstrip("@").strip()
            if sup:
                try:
                    wc_key = storage.find_chat_by_client_username(sup)
                    if wc_key:
                        resolved = int(wc_key)
                except Exception as e:
                    logger.debug("resolve_wc: by supplier %s failed: %s", sup, e)
        # 3) Через ФИО — перебираем managed_chats
        if not resolved:
            fio_norm = (card.get("fio") or "").strip().lower()
            if fio_norm:
                try:
                    managed = storage.state.get("managed_chats") or {}
                    for k, info in managed.items():
                        name = (info.get("client_name") or "").strip().lower()
                        if not name:
                            continue
                        # совпадение ФИО или fio попадает в client_name
                        if name == fio_norm or fio_norm in name or name in fio_norm:
                            try:
                                resolved = int(k)
                                break
                            except Exception:
                                continue
                except Exception as e:
                    logger.debug("resolve_wc: by fio failed: %s", e)
        # 4) Через deals — ищем сделку с тем же ФИО/username
        if not resolved:
            try:
                deals = storage.list_deals() or {}
                for did, d in deals.items():
                    if not d.get("work_chat_id"):
                        continue
                    if uname and (d.get("client_username") or "").lstrip("@").lower() == uname.lower():
                        resolved = int(d["work_chat_id"])
                        break
                    if fio_norm and (d.get("fio") or "").strip().lower() == fio_norm:
                        resolved = int(d["work_chat_id"])
                        break
            except Exception as e:
                logger.debug("resolve_wc: via deals failed: %s", e)
        # Сохраняем найденный work_chat_id в карточку
        if resolved and card_id:
            try:
                await storage.update_lk_card(card_id, work_chat_id=int(resolved))
                logger.info(
                    "resolve_wc: saved work_chat_id=%s to card=%s",
                    resolved, card_id,
                )
            except Exception as e:
                logger.warning("resolve_wc: save back failed: %s", e)
        return resolved

    def _do_payouts_list(self) -> str:
        """Сводка всех 3 очередей выплат."""
        try:
            storage.reload_sync()
        except Exception:
            pass
        rel = storage.list_payouts("release")
        fr = storage.list_payouts("fund_release")
        usdt = storage.list_payouts("usdt")
        lines = ["💰 <b>Очереди выплат</b>\n"]
        if rel:
            lines.append(f"\n🤝 <b>ОТПУСТИТЬ СДЕЛКУ</b> (гарант уже пополнен): {len(rel)}")
            for it in rel[:10]:
                deal = f" #{it.get('deal_id')}" if it.get("deal_id") else ""
                lines.append(
                    f"  • #{it['id']} {it.get('bank')} · {it.get('fio')}{deal} · "
                    f"<b>{it.get('amount_usdt', 0)}$</b>"
                )
        if fr:
            lines.append(f"\n🤝 <b>ПОПОЛНИТЬ И ОТПУСТИТЬ</b>: {len(fr)}")
            for it in fr[:10]:
                deal_part = f"сделка #{it.get('deal_id')}" if it.get("deal_id") else "<i>ждём номер сделки</i>"
                lines.append(
                    f"  • #{it['id']} {it.get('bank')} · {it.get('fio')} · "
                    f"<b>{it.get('amount_usdt', 0)}$</b> · {deal_part}"
                )
        if usdt:
            lines.append(f"\n💸 <b>USDT TRC20 ВЫПЛАТЫ</b>: {len(usdt)}")
            for it in usdt[:10]:
                lines.append(
                    f"  • #{it['id']} {it.get('bank')} · {it.get('fio')} · "
                    f"<b>{it.get('amount_usdt', 0)} USDT</b> → <code>{it.get('usdt_address')}</code>"
                )
        if not (rel or fr or usdt):
            lines.append("\n✅ <i>Очереди пусты.</i>")
        lines.append(
            "\n\n<b>Команды:</b>\n"
            "• <code>сделка #ХХХХ пополнена 400</code> — фиксируем пополнение по сделке\n"
            "• <code>отпущено #lk0001</code> — закрываем гарант-выплату (уведомление клиенту)\n"
            "• <code>выплачено #lk0001 ХЕШ_ТРАНЗАКЦИИ</code> — USDT отправлен"
        )
        return "\n".join(lines)

    async def _mark_payout_funded(self, deal_id: str, amount: float) -> str:
        """Менеджер пишет «сделка #X пополнена 400» — фиксирует факт пополнения.
        Если сделка из fund_release — двигаем в release (готова к отпуску).
        Если из release — обновляем сумму."""
        try:
            storage.reload_sync()
        except Exception:
            pass
        # Найти в fund_release (типичный случай)
        for q in ("fund_release", "release"):
            arr = storage.list_payouts(q)
            for item in arr:
                if str(item.get("deal_id") or "").lstrip("#") == str(deal_id):
                    if q == "fund_release":
                        # Двигаем в release как пополненную
                        await storage.remove_payout(q, item["id"])
                        new_id = await storage.add_payout("release", {
                            **{k: v for k, v in item.items() if k not in ("id", "ts", "status")},
                            "amount_usdt": amount,
                            "funded_at": time.time(),
                        })
                        return (
                            f"✅ Сделка #{deal_id} помечена как пополненная "
                            f"(<b>{amount} USDT</b>) и перенесена в очередь "
                            f"<b>ОТПУСТИТЬ СДЕЛКУ</b> (#release-{new_id})."
                        )
                    else:
                        await storage.update_payout(q, item["id"],
                                                     amount_usdt=amount,
                                                     funded_at=time.time())
                        return f"✅ Сделка #{deal_id} в очереди ОТПУСТИТЬ — сумма обновлена: {amount} USDT"
        # Не найдена — добавим в release как чистое пополнение
        new_id = await storage.add_payout("release", {
            "card_id": "", "bank": "?", "fio": "?",
            "deal_id": str(deal_id), "amount_usdt": amount,
            "funded_at": time.time(),
            "note": "manual: fund via «сделка пополнена»",
        })
        return f"⚠️ Сделка #{deal_id} не была в очереди — создана новая запись release-{new_id}"

    async def _mark_usdt_paid(self, card_id: str, tx_hash: str) -> str:
        """Менеджер: «выплачено #lk0001 abcdef...» — USDT отправлен.
        Шлёт клиенту уведомление с tronscan-ссылкой + удаляет из очереди."""
        try:
            storage.reload_sync()
        except Exception:
            pass
        match = storage.find_payout_by_card(card_id, queue="usdt")
        if not match:
            return f"⚠️ #{card_id} не в USDT-очереди"
        q, item = match
        bank = item.get("bank") or "—"
        fio = item.get("fio") or "—"
        addr = item.get("usdt_address") or ""
        amount = item.get("amount_usdt") or 0
        wc = item.get("work_chat_id")
        tronscan_url = f"https://tronscan.org/#/transaction/{tx_hash}"
        # Шлём клиенту
        client_msg = (
            f"💸 <b>Выплата отправлена</b>\n\n"
            f"ЛК: <b>{bank}</b> ({fio})\n"
            f"Сумма: <b>{amount} USDT</b>\n"
            f"Адрес: <code>{addr}</code>\n\n"
            f"🔗 <a href=\"{tronscan_url}\">Проверить на TronScan</a>\n"
            f"Хеш: <code>{tx_hash}</code>"
        )
        notified = False
        if wc:
            try:
                target = await self._resolve_chat_target(int(wc))
                await self.client.send_message(target, client_msg, parse_mode="html", link_preview=False)
                notified = True
            except Exception as e:
                logger.warning("usdt paid notify failed: %s", e)
        # Удаляем из очереди
        await storage.remove_payout(q, item["id"])
        # Меняем статус ЛК на ЗАВЕРШЁН
        try:
            await storage.set_lk_card_status(card_id, "ЗАВЕРШЁН", by="leo")
            await self._refresh_lk_card_post(card_id)
        except Exception:
            pass
        result = f"✅ USDT-выплата по #{card_id} закрыта. Хеш: {tx_hash[:16]}..."
        if notified:
            result += " · клиент уведомлён ✓"
        return result

    async def _mark_guarantor_released(self, key: str) -> str:
        """Менеджер: «отпущено #lk0001» или «отпущено 12345» — гарант-сделка
        отпущена. Шлёт клиенту уведомление + удаляет из очереди + статус ЗАВЕРШЁН."""
        try:
            storage.reload_sync()
        except Exception:
            pass
        key_l = key.lower().lstrip("#")
        match = None
        if key_l.startswith("lk"):
            match = storage.find_payout_by_card(key_l)
        else:
            match = storage.find_payout_by_deal(key_l)
        if not match:
            return f"⚠️ {key} не найдено в очередях гаранта"
        q, item = match
        if q not in ("release", "fund_release"):
            return f"⚠️ {key} в очереди {q}, а не гарант"
        bank = item.get("bank") or "—"
        fio = item.get("fio") or "—"
        amount = item.get("amount_usdt") or 0
        deal_id = item.get("deal_id") or ""
        wc = item.get("work_chat_id")
        deal_line = f" #{deal_id}" if deal_id else ""
        client_msg = (
            f"🤝 <b>Сделка отпущена</b>\n\n"
            f"ЛК: <b>{bank}</b> ({fio})\n"
            f"Сделка{deal_line} закрыта, сумма <b>{amount} USDT</b> "
            f"должна прийти к вам в гаранте в течение нескольких минут.\n\n"
            f"Спасибо за сотрудничество! 🙏"
        )
        notified = False
        if wc:
            try:
                target = await self._resolve_chat_target(int(wc))
                await self.client.send_message(target, client_msg, parse_mode="html", link_preview=False)
                notified = True
            except Exception as e:
                logger.warning("guarantor released notify failed: %s", e)
        await storage.remove_payout(q, item["id"])
        cid = item.get("card_id")
        if cid:
            try:
                await storage.set_lk_card_status(cid, "ЗАВЕРШЁН", by="leo")
                await self._refresh_lk_card_post(cid)
            except Exception:
                pass
        return f"✅ Гарант-выплата {key} закрыта" + (" · клиент уведомлён ✓" if notified else "")

    async def _notify_client_status_change(self, card_id: str, new_status: str) -> bool:
        """Уведомление клиенту при смене статуса ЛК на БЛОК / БРАК / ОТРАБОТАН / ЗАВЕРШЁН.
        Шаблон выбирается по new_status. Целевой work_chat резолвится строго
        через @supplier — никаких fallback (security-first)."""
        try:
            storage.reload_sync()
        except Exception:
            pass
        card = storage.get_lk_card(card_id) or {}
        if not card:
            return False
        bank = card.get("bank") or "—"
        fio = card.get("fio") or "—"
        supplier = (card.get("supplier") or "").lstrip("@").strip()
        wc = None
        if supplier:
            try:
                wc_key = storage.find_chat_by_client_username(supplier)
                if wc_key:
                    wc = int(wc_key)
            except Exception as e:
                logger.warning(
                    "notify_status: supplier @%s lookup failed: %s", supplier, e,
                )
        if not wc:
            logger.info(
                "notify_status %s: card=%s wc=None — пропускаем (нет supplier match)",
                new_status, card_id,
            )
            return False

        # Алиас ЗАВЕРШЕН → ЗАВЕРШЁН
        if new_status == "ЗАВЕРШЕН":
            new_status = "ЗАВЕРШЁН"

        method = (card.get("payment_method") or "").upper()
        usdt_addr = (card.get("usdt_address") or "").strip()
        deal_id = (card.get("deal_id") or "").lstrip("#").strip()

        # ── ОЖИДАЕТ_ПОПОЛНЕНИЯ (GUARANTOR_BEFORE — клиент дал номер, мы пополняем) ──
        if new_status == "ОЖИДАЕТ_ПОПОЛНЕНИЯ":
            deal_line = f" <b>#{deal_id}</b>" if deal_id else ""
            msg = (
                f"✅ <b>Номер сделки{deal_line} зафиксирован</b>\n\n"
                f"ЛК <b>{bank}</b> ({fio}).\n\n"
                f"Сделка принята в систему. <b>Мы пополним её в течение часа</b> "
                f"и сразу возьмём ваш счёт на перевязку. Как только сделка будет "
                f"пополнена — напишем сюда подтверждение."
            )
        # ── В_РАБОТЕ для GUARANTOR_BEFORE = «сделка пополнена, начинаем работу» ──
        elif new_status == "В_РАБОТЕ" and method == "GUARANTOR_BEFORE":
            deal_line = f" <b>#{deal_id}</b>" if deal_id else ""
            msg = (
                f"💰 <b>Сделка{deal_line} пополнена</b>\n\n"
                f"ЛК <b>{bank}</b> ({fio}).\n\n"
                f"Начинаем перевязку счёта — операционисты возьмут в работу. "
                f"Как только всё будет готово, пришлём подтверждение."
            )
        # ── шаблоны для БЛОК / БРАК (метод оплаты не влияет) ──
        elif new_status == "БЛОК":
            msg = (
                f"🚫 <b>Блокировка ЛК {bank}</b>\n\n"
                f"К сожалению, на ваш ЛК <b>{bank}</b> ({fio}) банк наложил блокировку "
                f"<b>после отработки</b>.\n\n"
                f"Пожалуйста, свяжитесь с банком (горячая линия / отделение / чат) "
                f"и выясните причину блока. Как только разберётесь — напишите нам, "
                f"мы возобновим работу или пересчитаем условия."
            )
        elif new_status == "БРАК":
            msg = (
                f"⚠️ <b>ЛК {bank} помечен как БРАК</b>\n\n"
                f"К сожалению, ЛК <b>{bank}</b> ({fio}) не подошёл нам по техническим "
                f"причинам (банк не пропускает операции / счёт не активен / другие "
                f"ограничения).\n\n"
                f"По этому ЛК работа прекращена. Если хотите — можем оформить другой ЛК, "
                f"напишите подробности."
            )

        # ── ОТРАБОТАН: метод оплаты определяет дальнейший шаг ──
        elif new_status == "ОТРАБОТАН":
            head = (
                f"✅ <b>ЛК {bank} отработан</b>\n\n"
                f"Работа по вашему ЛК <b>{bank}</b> ({fio}) успешно завершена.\n"
            )
            price_usdt = float(card.get("price_usdt") or 0)

            if method == "USDT_TRC20":
                if usdt_addr:
                    tail = (
                        f"\n💸 <b>Метод оплаты:</b> USDT TRC20\n"
                        f"📍 Ваш адрес: <code>{usdt_addr}</code>\n"
                        f"💰 Сумма: <b>{price_usdt} USDT</b>\n\n"
                        f"Готовим перевод. Как отправим — пришлём хеш транзакции "
                        f"(можно проверить на TronScan)."
                    )
                    # Добавляем в очередь USDT
                    try:
                        await storage.add_payout("usdt", {
                            "card_id": card_id, "bank": bank, "fio": fio,
                            "supplier": supplier, "work_chat_id": wc,
                            "usdt_address": usdt_addr, "amount_usdt": price_usdt,
                        })
                    except Exception as e:
                        logger.warning("add_payout usdt failed: %s", e)
                else:
                    tail = (
                        f"\n💸 <b>Метод оплаты:</b> USDT TRC20\n\n"
                        f"⚠️ <b>Пришлите ваш USDT TRC20 адрес</b> — мы оформим выплату."
                    )
            elif method in ("GUARANTOR_AFTER_WORK", "GUARANTOR_AFTER"):
                tail = (
                    f"\n🤝 <b>Метод оплаты:</b> гарант после отработки\n\n"
                    f"<b>📝 Как создать сделку в Continental:</b>\n"
                    f"1️⃣ Зайдите на Continental → создайте новую сделку с пользователем <b>@PRIDE_CL</b>\n"
                    f"2️⃣ Заполните параметры:\n"
                    f"   • <b>Сумма:</b> {price_usdt} USDT\n"
                    f"   • <b>Назначение:</b> оплата ЛК {bank} · {fio}\n"
                    f"   • <b>Кто пополняет:</b> @PRIDE_CL (мы)\n"
                    f"   • <b>Получатель:</b> вы\n"
                    f"3️⃣ В описании сделки укажите ссылку на условия:\n"
                    f"   🔗 <a href=\"https://telegra.ph/PRIDE---Usloviya-i-polozheniya-provedeniya-sdelok-po-pokupke-rasschyotnyh-schetov-02-24\">УСЛОВИЯ ПРОВЕДЕНИЯ СДЕЛКИ PRIDE</a>\n"
                    f"4️⃣ Создайте сделку и <b>пришлите её номер</b> одним сообщением сюда.\n\n"
                    f"После того как пришлёте номер — мы пополним сделку и отпустим её "
                    f"<b>в течение дня</b> после подтверждения отработки."
                )
                # Поставим в очередь ожидания номера сделки (deal_id пуст)
                try:
                    await storage.add_payout("fund_release", {
                        "card_id": card_id, "bank": bank, "fio": fio,
                        "supplier": supplier, "work_chat_id": wc,
                        "amount_usdt": price_usdt,
                        "deal_id": "",  # клиент пришлёт
                    })
                except Exception as e:
                    logger.warning("add_payout fund_release failed: %s", e)
            elif method == "GUARANTOR_BEFORE":
                deal_line = f" <b>#{deal_id}</b>" if deal_id else ""
                tail = (
                    f"\n🤝 <b>Метод оплаты:</b> гарант (сделка{deal_line} уже пополнена)\n\n"
                    f"Со своей стороны вам <b>ничего делать не нужно</b> — "
                    f"сделка уже подтверждена и пополнена. Мы её <b>отпустим в течение дня</b>, "
                    f"средства автоматически придут к вам."
                )
                # Очередь «отпустить»
                try:
                    existing = storage.find_payout_by_card(card_id, queue="release")
                    if not existing:
                        await storage.add_payout("release", {
                            "card_id": card_id, "bank": bank, "fio": fio,
                            "supplier": supplier, "work_chat_id": wc,
                            "amount_usdt": price_usdt,
                            "deal_id": deal_id,
                        })
                except Exception as e:
                    logger.warning("add_payout release failed: %s", e)
            else:
                tail = (
                    f"\n💳 <b>Метод оплаты ещё не зафиксирован.</b>\n\n"
                    f"Подскажите, как удобнее получить оплату:\n"
                    f"• <b>USDT TRC20</b> — пришлите адрес\n"
                    f"• <b>Гарант</b> — оформим сделку через Continental @PRIDE_CL"
                )
            msg = head + tail

        # ── ЗАВЕРШЁН: выплата подтверждена ──
        elif new_status == "ЗАВЕРШЁН":
            head = (
                f"🏁 <b>ЛК {bank} полностью завершён</b>\n\n"
                f"Работа и выплата по ЛК <b>{bank}</b> ({fio}) полностью завершены.\n"
            )
            if method == "USDT_TRC20" and usdt_addr:
                tail = (
                    f"\n💸 USDT отправлен на адрес <code>{usdt_addr}</code>.\n"
                    f"Проверьте баланс — должно зачислиться через несколько минут.\n\n"
                    f"Спасибо за сотрудничество! 🙏"
                )
            elif method in ("GUARANTOR_AFTER_WORK", "GUARANTOR_AFTER", "GUARANTOR_BEFORE"):
                deal_line = f" #{deal_id}" if deal_id else ""
                tail = (
                    f"\n🤝 Гарант-сделка{deal_line} закрыта, средства выведены вам.\n\n"
                    f"Спасибо за сотрудничество! 🙏\n"
                    f"Будете снова продавать — пишите, оформим быстро."
                )
            else:
                tail = (
                    f"\nСпасибо за сотрудничество! 🙏\n"
                    f"Будете снова продавать — пишите, оформим быстро."
                )
            msg = head + tail
        else:
            return False
        try:
            target = await self._resolve_chat_target(wc)
            await self.client.send_message(
                target, msg, parse_mode="html", link_preview=False,
            )
            logger.info(
                "notify_status %s sent: card=%s wc=%s",
                new_status, card_id, wc,
            )
            try:
                _e("status-notify-sent", {
                    "card_id": card_id, "status": new_status,
                    "bank": bank, "fio": fio, "work_chat_id": wc,
                }, character="lk")
            except Exception:
                pass
            return True
        except Exception as e:
            logger.warning(
                "notify_status %s send failed: card=%s wc=%s err=%s",
                new_status, card_id, wc, e,
            )
            return False

    async def _handle_block_no_work_actions(self, card_id: str) -> bool:
        """Side-effects при переходе ЛК в статус БЛОК_БЕЗ_ОТРАБОТКИ:
          1) Отменяем сделку (если есть deal_id) — статус ОТМЕНЁНА_БЛОК
          2) Оповещаем клиента в его work_chat — что произошёл блок без
             отработки и нужно решить в банке вопрос почему блок
          3) Кидаем reply на анкету в Группе 1 ЛК с инструкцией для
             работника-выплат (отмена сделки + забрать деньги если был гарант)

        Возвращает True если хотя бы одно действие выполнено."""
        try:
            storage.reload_sync()
        except Exception:
            pass
        card = storage.get_lk_card(card_id) or {}
        if not card:
            logger.warning("block_no_work: card #%s not found", card_id)
            return False
        # Карточке нужен card_id для save-back
        card_with_id = dict(card)
        card_with_id["card_id"] = card_id

        bank = card.get("bank") or ""
        fio = card.get("fio") or ""
        deal_id = (card.get("deal_id") or "").strip().lstrip("#").strip("-—")
        method = (card.get("payment_method") or "").upper()

        # === СТРОГИЙ резолв work_chat ТОЛЬКО через @supplier ===
        # Никаких fallback по ФИО / deals — это может попасть в чужой чат.
        # Логика:
        #   1) card.supplier — username поставщика (на чей счёт оформлено)
        #   2) Ищем chat_id поставщика в managed_chats через client_username_index
        #   3) Если не нашли → НЕ шлём уведомление (security-first).
        wc = None
        supplier = (card.get("supplier") or "").lstrip("@").strip()
        supplier_resolved = False
        if supplier:
            try:
                wc_key = storage.find_chat_by_client_username(supplier)
                if wc_key:
                    wc = int(wc_key)
                    supplier_resolved = True
                    # Сохраним work_chat_id в карточку для следующих операций
                    if not card.get("work_chat_id"):
                        try:
                            await storage.update_lk_card(
                                card_id, work_chat_id=wc,
                            )
                        except Exception:
                            pass
            except Exception as e:
                logger.warning(
                    "block_no_work: supplier @%s lookup failed: %s",
                    supplier, e,
                )
        # Если supplier пустой ИЛИ не нашли его в managed_chats — work_chat=None
        # и клиента уведомлять НЕ будем (это безопаснее чем отправить чужому).
        did_anything = False

        # 1) Отменяем сделку, если она есть
        deal_cancelled = False
        if deal_id and storage.get_deal(deal_id):
            try:
                ok = await storage.update_deal_status(
                    deal_id, "ОТМЕНЁНА_БЛОК",
                )
                if ok:
                    deal_cancelled = True
                    did_anything = True
                    _e("deal-cancelled-block-no-work", {
                        "deal_id": deal_id, "card_id": card_id,
                        "bank": bank, "fio": fio,
                    }, character="lk", severity="alert")
                    logger.info(
                        "block_no_work: deal cancelled deal=%s card=%s",
                        deal_id, card_id,
                    )
            except Exception as e:
                logger.warning(
                    "block_no_work: cancel deal %s failed: %s",
                    deal_id, e,
                )

        # 2) Оповещаем клиента в work_chat
        client_notified = False
        if wc:
            msg = (
                f"⛔ <b>Блок без отработки</b>\n\n"
                f"К сожалению, на ЛК <b>{bank}</b> ({fio}) банк наложил "
                f"блок ещё до <b>НАЧАЛА</b> отработки.\n\n"
                f"Сейчас мы не можем даже начать работу по этому счёту — "
                f"нужно <b>обратиться в банк и выяснить причину блока</b> "
                f"(служба поддержки, чат банка, отделение или горячая линия).\n\n"
                f"По счёту не прошли средства, такое бывает и причин может "
                f"быть множество — причину, к сожалению, узнать можно только "
                f"у банка.\n\n"
                f"Как только разберётесь с банком и блок снимут — "
                f"напишите нам, мы возобновим работу."
            )
            if deal_cancelled:
                msg += (
                    f"\n\nПо вашей сделке <b>#{deal_id}</b> мы инициировали "
                    f"отмену — пожалуйста, подтвердите отмену со своей "
                    f"стороны в боте гаранта."
                )
            try:
                target = await self._resolve_chat_target(wc)
                await self.client.send_message(
                    target, msg, parse_mode="html", link_preview=False,
                )
                client_notified = True
                did_anything = True
                logger.info(
                    "block_no_work: client notified card=%s wc=%s",
                    card_id, wc,
                )
            except Exception as e:
                logger.warning(
                    "block_no_work: notify client failed card=%s: %s",
                    card_id, e,
                )

        # 3) Reply на анкету в Группе 1 с инструкцией работнику
        msg_id = int(card.get("lk_group_msg_id") or 0)
        lk_gid = storage.get_lk_group_id()
        if lk_gid and msg_id:
            lines = [
                "⛔ <b>БЛОК БЕЗ ОТРАБОТКИ</b>",
                f"ЛК: <b>{bank}</b> ({fio})",
            ]
            if deal_cancelled:
                lines.append(f"❌ Сделка <code>#{deal_id}</code> — ОТМЕНА.")
                lines.append("💸 Нужно ЗАБРАТЬ ДЕНЬГИ со сделки. @TimonSkupCL")
            elif deal_id:
                lines.append(
                    f"⚠️ По сделке <code>#{deal_id}</code> — проверить статус."
                )
            if client_notified:
                lines.append(
                    f"📩 Поставщику <b>@{supplier}</b> отправлено уведомление "
                    f"в его рабочую беседу."
                )
            elif not supplier:
                lines.append(
                    "⚠️ <b>Поставщик не указан</b> в карточке — "
                    "уведомление НЕ отправлено. Свяжитесь с клиентом вручную."
                )
            elif not supplier_resolved:
                lines.append(
                    f"⚠️ Поставщик <b>@{supplier}</b> не найден в managed_chats — "
                    f"уведомление НЕ отправлено. Свяжитесь вручную или сделайте "
                    f"<code>/sync_clients</code>."
                )
            else:
                lines.append(
                    f"⚠️ Уведомление поставщику <b>@{supplier}</b> не доставлено "
                    f"(ошибка отправки). Свяжитесь вручную."
                )
            lines.append(
                "🏦 Нужно решить в банке вопрос — почему произошёл блок."
            )
            instr_text = "\n".join(lines)
            try:
                target = await self._resolve_chat_target(lk_gid)
                await self.client.send_message(
                    target, instr_text, reply_to=msg_id,
                    parse_mode="html", link_preview=False,
                )
                did_anything = True
            except Exception as e:
                logger.warning(
                    "block_no_work: post instruction failed card=%s: %s",
                    card_id, e,
                )

        _e("lk-block-no-work-processed", {
            "card_id": card_id, "bank": bank, "fio": fio,
            "deal_id": deal_id,
            "deal_cancelled": deal_cancelled,
            "client_notified": client_notified,
        }, character="lk", severity="alert")

        return did_anything

    async def _refresh_lk_card_post(self, card_id: str) -> bool:
        """Edit/post карточки в Группе 1 актуальным состоянием."""
        card = storage.get_lk_card(card_id)
        if not card:
            return False
        gid = storage.get_lk_group_id()
        if not gid:
            return False
        text = accounting2.format_lk_card(card)
        msg_id = card.get("lk_group_msg_id") or 0
        try:
            target = await self._resolve_chat_target(gid)
            if msg_id:
                try:
                    await self.client.edit_message(
                        target, msg_id, text, parse_mode="html", link_preview=False,
                    )
                    return True
                except Exception:
                    pass
            sent = await self.client.send_message(
                target, text, parse_mode="html", link_preview=False,
            )
            new_id = getattr(sent, "id", None)
            if new_id:
                await storage.set_lk_card_msg_id(card_id, new_id)
            return True
        except Exception as e:
            logger.warning("refresh_lk_card_post card=%s: %s", card_id, e)
            return False

    async def _create_lk_card_from_perevyaz(
        self, event, chat_info: dict, lk_text: str = "", fio_text: str = "",
    ) -> Optional[str]:
        """Триггер «Перевяз ЛК выполнен» — создаём карточку в Группе 1.

        Источники данных по приоритету:
          - bank: lk_text (из 'ЛК:' в перевязке) → deal.bank
          - fio:  fio_text (из 'ФИО:' в перевязке) → deal.fio
          - method, usdt_address: chat_info (что AI собрал через set_payment_method)
          - price: deal.amount → прайс по банку
          - deal_id: deal.deal_id (если уже создан до перевязки)
          - supplier/client_username: chat_info.client_username → resolve по client_id

        Если не хватает только метода — запрашиваем у клиента ТОЛЬКО метод.
        Банк/ФИО уже в самом сообщении CRM-бота, спрашивать их повторно нельзя.
        """
        wc = event.chat_id
        method = chat_info.get("payment_method", "")
        client_uname = chat_info.get("client_username") or ""
        # Резолв username через Telethon если в managed_chats пусто
        if not client_uname:
            client_id = chat_info.get("client_id")
            if client_id:
                try:
                    ent = await self.client.get_entity(int(client_id))
                    if getattr(ent, "username", None):
                        client_uname = ent.username
                        await storage.update_client_username(wc, client_uname)
                except Exception as e:
                    logger.warning("perevyaz: resolve username failed: %s", e)
        # Сделка для этого work_chat (для GUARANTOR_BEFORE/_AFTER уже создана)
        deal = None
        for did, d in (storage.list_deals() or {}).items():
            if d.get("work_chat_id") and abs(int(d["work_chat_id"])) == abs(int(wc)):
                if d.get("status") not in ("ЗАВЕРШЕНА", "ОТМЕНА_СДЕЛКИ"):
                    deal = {"deal_id": did, **d}
                    break

        # Bank: предпочитаем то что прислал CRM в перевязке
        bank = (lk_text or "").strip() or ((deal or {}).get("bank") or "")
        # FIO: то же самое
        fio = (fio_text or "").strip() or ((deal or {}).get("fio") or "")
        # Цена: deal.amount → прайс по банку
        price = float((deal or {}).get("amount") or 0)
        if not price and bank:
            price = accounting2.lookup_pricing(bank)

        deal_id = (deal or {}).get("deal_id") or ""
        usdt_addr = chat_info.get("usdt_address") or ""

        # Минимум для создания карточки: bank.
        # ВАЖНО (логика май 2026, ред. v2): карточка создаётся СРАЗУ с
        # дефолтным методом, чтобы попасть в Отдел ЛК/Группу 1 без задержки.
        # Но AI ОБЯЗАН затем уточнить метод у клиента (это один из случаев
        # когда AI может тегать клиента + слать несколько сообщений + ставить
        # напоминалки). Когда клиент пришлёт ответ — _tool_set_payment_method
        # ОБНОВИТ существующую карточку (а не создаст дубль).
        if not bank:
            logger.warning(
                "perevyaz: bank не определён (lk_text=%r, deal=%s) — пропускаем",
                lk_text, bool(deal),
            )
            return None
        method_was_default = False
        if not method:
            method = "GUARANTOR_AFTER_WORK"
            method_was_default = True
            try:
                await storage.set_chat_payment_info(
                    wc, method=method, usdt_address="",
                )
            except Exception as e:
                logger.warning("perevyaz: set_chat_payment_info(default) failed: %s", e)
            logger.info(
                "perevyaz: auto-default method=GUARANTOR_AFTER_WORK для chat=%s "
                "— карточка создаётся, AI продолжит уточнять у клиента", wc,
            )

        # Воронка: РС сдан (анкета ЛК создана = клиент реально отдал счёт)
        try:
            await storage.bump_funnel("rs_handed")
        except Exception:
            pass
        # Память клиента: сохраняем метод оплаты и адрес под @username
        # чтобы при следующем перевязе AI не спрашивал заново.
        try:
            if client_uname:
                await storage.save_client_preferences(
                    client_uname,
                    payment_method=method,
                    usdt_address=usdt_addr,
                    fio=fio,
                    bank=bank,
                )
        except Exception as e:
            logger.warning("save_client_preferences failed: %s", e)
        card_id = await storage.add_lk_card(
            supplier=client_uname,
            bank=bank,
            fio=fio,
            price_usdt=price,
            payment_method=method,
            deal_id=deal_id,
            usdt_address=usdt_addr,
            status="В_РАБОТЕ",
            client_id=chat_info.get("client_id") or 0,
            client_username=client_uname,
            work_chat_id=wc,
            created_by="perevyaz",
        )
        await self._refresh_lk_card_post(card_id)
        logger.info(
            "LK card created from perevyaz: %s for chat=%s bank=%s fio=%s method=%s",
            card_id, wc, bank, fio, method,
        )

        # Если метод поставили по дефолту — AI ВСЁ РАВНО уточняет у клиента.
        # _request_lk_data_from_client тегает клиента + ставит reminder loop.
        # Когда клиент ответит, set_payment_method обновит payment_method
        # этой же карточки (без создания дубля — он идемпотентный).
        if method_was_default:
            try:
                await self._request_lk_data_from_client(
                    event, chat_info, deal, bank=bank, fio=fio,
                )
                logger.info(
                    "perevyaz: method-clarification reminder scheduled for chat=%s card=%s",
                    wc, card_id,
                )
            except Exception as e:
                logger.warning("perevyaz: method-clarification scheduling failed: %s", e)
        _e("lk-created", {
            "card_id": card_id, "bank": bank, "fio": fio,
            "method": method, "source": "perevyaz",
        }, character="lk", severity="success")
        return card_id

    async def _request_lk_data_from_client(
        self, event, chat_info: dict, deal: Optional[dict],
        bank: str = "", fio: str = "",
    ):
        """Перевяз есть, но данных не хватает — спросить у клиента + reminder.

        ВАЖНО: банк и ФИО CRM-бот присылает прямо в тексте перевязки
        (`ЛК:` и `ФИО:`), повторно их спрашивать не нужно. Спрашиваем
        только реально недостающие поля (обычно — метод оплаты)."""
        wc = event.chat_id
        missing = []
        # bank/fio проверяем с учётом того что было распарсено из перевязки
        eff_bank = bank or (deal or {}).get("bank") or ""
        eff_fio = fio or (deal or {}).get("fio") or ""
        if not eff_bank:
            missing.append("банк")
        if not eff_fio:
            missing.append("ФИО держателя счёта")
        if not chat_info.get("payment_method"):
            missing.append("метод оплаты (USDT TRC20 или сделка в гаранте)")
        if not missing:
            return
        msg = (
            f"✅ Перевяз ЛК зафиксирован. Чтобы продолжить, уточните:\n"
            + "\n".join(f"• {x}" for x in missing)
        )
        try:
            target = await self._resolve_chat_target(wc)
            await self.client.send_message(target, msg, link_preview=False)
        except Exception as e:
            logger.warning("request_lk_data send failed: %s", e)
        # Сохраняем bank+fio перевязки в pending, чтобы когда клиент назовёт
        # метод — забрать оттуда и сразу создать карточку (без повторного
        # перевязного события от CRM-бота).
        if eff_bank or eff_fio:
            try:
                await storage.set_pending_perevyaz(wc, eff_bank, eff_fio)
            except Exception as e:
                logger.warning("set_pending_perevyaz failed: %s", e)
        # Запоминаем что нужны данные — reminder loop
        from storage import _norm_chat_id
        key = _norm_chat_id(wc)
        # Простой механизм: pending dict, проверяется фоновой задачей.
        # Здесь — отдельный launcher, без бесконечного создания тасок.
        if not hasattr(self, "_lk_pending"):
            self._lk_pending = {}
        if key not in self._lk_pending:
            self._lk_pending[key] = {
                "chat_id": wc,
                "reminder_count": 0,
                "started_at": time.time(),
            }
            asyncio.create_task(self._lk_reminder_loop(wc, key))

    async def _lk_reminder_loop(self, wc, key: str):
        """Раз в 5 минут пинаем клиента, пока данные не появятся (или 6 раз).

        Условия выхода (НЕ шлём напоминалку):
          1) Карточка ЛК уже существует.
          2) Метод оплаты собран → триггерим создание карточки и выходим.
          3) Клиент пишет в чате после старта pending — значит он не застрял,
             AI ведёт диалог сам, спамить ⏰-напоминаниями не нужно.
        """
        started_at = (self._lk_pending.get(key) or {}).get("started_at", time.time())
        from storage import _norm_chat_id  # noqa
        norm_key = _norm_chat_id(wc)
        for _ in range(6):
            await asyncio.sleep(300)  # 5 минут
            # 1) Карточка уже есть → нечего напоминать
            existing = storage.find_lk_card(work_chat_id=wc) or []
            if existing:
                logger.info(
                    "lk_reminder: chat=%s — карточка(и) уже есть (%d), выходим",
                    wc, len(existing),
                )
                self._lk_pending.pop(key, None)
                return
            chat_info = storage.get_chat_info(wc) or {}
            # 2) Все данные собраны → триггерим creation
            method = chat_info.get("payment_method")
            if method:
                fake_event = type("E", (), {"chat_id": wc, "message": None})()
                try:
                    await self._create_lk_card_from_perevyaz(fake_event, chat_info)
                except Exception:
                    pass
                self._lk_pending.pop(key, None)
                return
            # 3) Клиент уже общался после запроса — AI ведёт диалог сам
            last_client = (self._last_client_msg_ts or {}).get(norm_key, 0)
            if last_client > started_at:
                logger.info(
                    "lk_reminder: chat=%s — клиент активен (last_client_msg > started_at), "
                    "AI сам ведёт диалог, выходим без напоминания",
                    wc,
                )
                self._lk_pending.pop(key, None)
                return
            # Напомним
            try:
                target = await self._resolve_chat_target(wc)
                await self.client.send_message(
                    target,
                    "⏰ Напоминаю — без указания метода оплаты и реквизитов "
                    "мы не сможем работать с вашим ЛК. Ответьте, пожалуйста.",
                    link_preview=False,
                )
            except Exception as e:
                logger.warning("lk reminder send failed: %s", e)
        self._lk_pending.pop(key, None)

    # === V2: Группа 2 «Бухгалтерия» (заявки v2 + расчёт + auto-update ЛК) ===

    async def _handle_accounting_v2_message(self, event):
        """Заявки V2 + команды управления (удалить/редактировать/help)."""
        if not event.message:
            return
        text = (event.message.text or "").strip()
        if not text:
            return
        if self._me and event.sender_id == self._me.id:
            return

        low = text.lower()

        # Команды сводки / деталей / payment-proof ПЕРЕНЕСЕНЫ в Группу 1 ЛК.
        # В бухгалтерии остаются только команды заявок (СТАРТ-отчёт,
        # удалить/редактировать заявку, правка цены) — это инструменты
        # оператора, не работника-выплат.

        # Команда: удалить заявку N
        m = re.match(r"^\s*удалить\s+заявку\s+#?(\d+)\s*$", text, re.I)
        if m:
            app_id = int(m.group(1))
            date_str = accounting2.today_str()
            ok = await storage.remove_application_v2(date_str, app_id)
            if ok:
                await event.reply(
                    f"🗑 Заявка #{app_id} за {date_str} удалена.",
                    parse_mode="html",
                )
            else:
                await event.reply(
                    f"⚠️ Заявка #{app_id} за {date_str} не найдена.",
                    parse_mode="html",
                )
            return

        # Команда: редактировать/исправить/изменить заявку N
        m = re.match(
            r"^\s*(?:редактировать|исправить|изменить)\s+заявку\s+#?(\d+)\s*$",
            text, re.I,
        )
        if m:
            app_id = int(m.group(1))
            date_str = accounting2.today_str()
            apps = storage.get_applications_v2(date_str) or []
            target = next(
                (a for a in apps if int(a.get("id", 0)) == app_id), None
            )
            if not target:
                await event.reply(
                    f"⚠️ Заявка #{app_id} за {date_str} не найдена.",
                    parse_mode="html",
                )
                return
            self._editing_app[event.chat_id] = (date_str, app_id)
            await event.reply(
                f"✏️ Жду новый текст заявки <b>#{app_id}</b> за {date_str}.\n\n"
                "Пришлите её в полном формате (<code>ЗАЯВКА N\nПРИЕМ: ...</code>).\n"
                "Старая заявка будет удалена и заменена новой.\n\n"
                "Чтобы отменить — напишите <code>отмена</code>.",
                parse_mode="html",
            )
            return

        # Команда: правка цены ЛК в заявке.
        # Формат: "заявка N <банк/ФИО любые слова> <цена>$"
        # Примеры:
        #   заявка 1 Альфа Иванов 350
        #   заявка 1 Иванов Иван Иванович Альфа 400$
        m = re.match(
            r"^\s*заявка\s+#?(\d+)\s+(.+?)\s+(\d+(?:\.\d+)?)\s*\$?\s*$",
            text, re.I,
        )
        if m and "\n" not in text:
            app_id = int(m.group(1))
            middle = m.group(2).strip()
            new_price = float(m.group(3))
            date_str = accounting2.today_str()
            apps = storage.get_applications_v2(date_str) or []
            target = next(
                (a for a in apps if int(a.get("id", 0)) == app_id), None
            )
            if not target:
                await event.reply(
                    f"⚠️ Заявка #{app_id} за {date_str} не найдена.",
                    parse_mode="html",
                )
                return
            # В middle ищем известный банк, остальное — ФИО
            BANKS = ("альфа", "озон", "точка", "втб", "райф", "райффайзен",
                     "уралсиб", "локо", "бкс", "дело", "убрир", "тинькофф")
            words = middle.split()
            bank_word = None
            fio_words = []
            for w in words:
                if w.lower() in BANKS or w.lower().startswith("райф"):
                    bank_word = w
                else:
                    fio_words.append(w)
            if not bank_word:
                await event.reply(
                    "⚠️ Не нашёл банк в команде. Формат: "
                    "<code>заявка N БАНК ФИО ЦЕНА</code>",
                    parse_mode="html",
                )
                return
            fio_q = " ".join(fio_words).strip()
            # Найти ЛК в заявке (intake или outputs) по банку+ФИО
            intake = target.get("intake") or {}
            outputs = target.get("outputs") or []
            updated_item = None
            for item in [intake] + outputs:
                if not item:
                    continue
                if (item.get("bank") or "").lower() != bank_word.lower():
                    continue
                if fio_q and fio_q.lower() not in (item.get("fio") or "").lower():
                    continue
                item["price_usdt_override"] = new_price
                updated_item = item
                break
            if not updated_item:
                await event.reply(
                    f"⚠️ ЛК <b>{bank_word} {fio_q}</b> в заявке #{app_id} не найден.",
                    parse_mode="html",
                )
                return
            # Пересчитать с учётом предыдущих заявок дня (без самой target)
            lk_cards = storage.list_lk_cards() or {}
            all_apps = storage.get_applications_v2(date_str) or []
            prev_apps = [
                p for p in all_apps if int(p.get("id", 0)) < int(app_id)
            ]
            new_computed = accounting2.compute_application_v2(
                target, lk_cards, prev_apps=prev_apps,
            )
            target["computed"] = new_computed
            await storage.update_application_v2(
                date_str, app_id,
                intake=intake,
                outputs=outputs,
                computed=new_computed,
            )
            # Перерисовать отчёт (edit) если знаем msg_id
            new_report = accounting2.format_application_report_v2(target, new_computed)
            new_report = (
                f"♻️ <i>Цена ЛК {bank_word} {fio_q} обновлена → "
                f"{new_price:.0f}$</i>\n\n" + new_report
            )
            report_msg_id = target.get("report_msg_id")
            edited = False
            if report_msg_id:
                try:
                    target_chat = await self._resolve_chat_target(event.chat_id)
                    await self.client.edit_message(
                        target_chat, int(report_msg_id), new_report,
                        parse_mode="html", link_preview=False,
                    )
                    edited = True
                except Exception as e:
                    logger.warning("edit report msg failed: %s", e)
            if not edited:
                await event.reply(new_report, parse_mode="html", link_preview=False)
            else:
                # Подтверждение оператору
                await event.reply(
                    f"✏️ Цена ЛК <b>{bank_word} {fio_q}</b> в заявке #{app_id} "
                    f"обновлена → <b>{new_price:.0f}$</b>. Отчёт пересчитан.",
                    parse_mode="html",
                )
            return

        # Команда: отмена редактирования
        if low in ("отмена", "cancel", "отменить") and event.chat_id in self._editing_app:
            ed_date, ed_id = self._editing_app.pop(event.chat_id)
            await event.reply(
                f"❎ Редактирование заявки #{ed_id} ({ed_date}) отменено.",
                parse_mode="html",
            )
            return

        # Заявка V2 (мульти-строка с «ЗАЯВКА N»)
        if "\n" in text and "заявка" in low:
            app = accounting2.parse_application_v2(text)
            if app:
                # Если был режим редактирования — удаляем старую перед применением
                editing = self._editing_app.pop(event.chat_id, None)
                replaced_info = None
                if editing:
                    ed_date, ed_id = editing
                    await storage.remove_application_v2(ed_date, ed_id)
                    replaced_info = (ed_date, ed_id)
                await self._apply_application_v2(event, app, replaced=replaced_info)
                return

        # Help — полный список команд Группы 2
        if re.match(r"^\s*(?:помощь|справка|/help|/?\?|help)\s*$", text, re.I):
            await self._send_help_accounting_group(event)
            return

    async def _maybe_handle_lk_payment_proof(
        self, event, reply_to_msg_id: int, proof_text: str,
    ) -> bool:
        """Reply работника-выплат В Группе 1 ЛК на анкету (или на наш
        action-reply на анкету) — пруф оплаты/отпуска. Юзербот:
          1. Карточка → ЗАВЕРШЁН.
          2. Refresh анкеты в Группе 1.
          3. Уведомление клиенту в его work_chat.

        Чтобы случайные сообщения не триггерили — проверяем что текст НЕ
        парсится как edit карточки (field=value)."""
        # Сначала найдём карточку по reply_to_msg_id (анкета или наш reply).
        cards = storage.list_lk_cards() or {}
        target_card = None
        target_cid = None
        for cid, c in cards.items():
            anketa = int(c.get("lk_group_msg_id") or 0)
            action_msg = int(c.get("post_action_reply_msg_id") or 0)
            if reply_to_msg_id in (anketa, action_msg) and anketa or action_msg:
                target_card = c
                target_cid = cid
                break
        if not target_card:
            return False
        # Если текст — это edit-команда (поле/значение), не трактуем как пруф.
        if self._parse_lk_field_update(proof_text):
            return False
        # Если это команда удаления — тоже не пруф.
        if self._AI_CMD_DELETE_REPLY_RE.match(proof_text or ""):
            return False
        status = target_card.get("status") or ""
        if status in ("ЗАВЕРШЁН", "БРАК"):
            return False

        # Меняем статус → ЗАВЕРШЁН
        try:
            await storage.set_lk_card_status(
                target_cid, "ЗАВЕРШЁН", by="lk_payment_proof",
            )
            await self._refresh_lk_card_post(target_cid)
        except Exception as e:
            logger.warning(
                "lk_payment_proof: set ЗАВЕРШЁН failed for %s: %s",
                target_cid, e,
            )

        # Уведомить клиента в work_chat
        wc = target_card.get("work_chat_id")
        method = (target_card.get("payment_method") or "").upper()
        bank = target_card.get("bank") or "—"
        fio = target_card.get("fio") or "—"
        deal_id = (target_card.get("deal_id") or "").strip().lstrip("#")
        usdt_addr = (target_card.get("usdt_address") or "").strip()
        notified = False
        if wc:
            if method == "USDT_TRC20":
                msg = (
                    f"✅ Оплата за ЛК <b>{bank}</b> ({fio}) "
                    f"на ваш USDT TRC20 <b>переведена</b>.\n\n"
                    f"Сумма: <b>{accounting2._fmt_usdt(target_card.get('price_usdt', 0))}</b>\n"
                )
                if usdt_addr:
                    msg += f"Адрес: <code>{usdt_addr}</code>\n"
                msg += "Проверьте поступление 🙏"
            elif method in (
                "GUARANTOR_BEFORE", "GUARANTOR_AFTER", "GUARANTOR_AFTER_WORK",
            ):
                if deal_id:
                    msg = (
                        f"✅ Сделка <code>#{deal_id}</code> по ЛК "
                        f"<b>{bank}</b> ({fio}) <b>пополнена и отпущена "
                        f"в вашу сторону</b>.\n\nПроверьте Conte 🙏"
                    )
                else:
                    msg = (
                        f"✅ ЛК <b>{bank}</b> ({fio}) обработан, выплата ушла."
                    )
            else:
                msg = f"✅ ЛК <b>{bank}</b> ({fio}) обработан, выплата ушла."
            try:
                target = await self._resolve_chat_target(wc)
                await self.client.send_message(
                    target, msg, parse_mode="html", link_preview=False,
                )
                notified = True
                logger.info(
                    "lk_payment_proof: notified client work_chat=%s card=%s",
                    wc, target_cid,
                )
            except Exception as e:
                logger.warning(
                    "lk_payment_proof notify failed for card=%s: %s",
                    target_cid, e,
                )

        # Подтверждение в Группе 1 ЛК
        try:
            await event.reply(
                f"✅ <code>#{target_cid}</code> {bank} {fio} → "
                f"<b>ЗАВЕРШЁН</b>. "
                + ("Клиент уведомлён." if notified else
                   "<i>Клиент НЕ уведомлён (нет work_chat)</i>"),
                parse_mode="html",
            )
        except Exception as e:
            logger.warning("lk_payment_proof ack failed: %s", e)
        return True

    async def _maybe_handle_payment_proof(
        self, event, reply_to_msg_id: int, proof_text: str,
    ) -> bool:
        """Работник reply'ит на сообщение юзербота с инструкциями (со пруфом
        выплаты/отпуска). Юзербот:
          1. Находит карточки ЛК где accounting_reply_msg_id == reply_to_msg_id.
          2. Для каждой карточки: статус → ЗАВЕРШЁН, refresh поста в Группе 1.
          3. Пишет клиенту в его work_chat (USDT/GUARANTOR — по методу).
        """
        cards = storage.list_lk_cards() or {}
        matched = [
            (cid, c) for cid, c in cards.items()
            if int(c.get("accounting_reply_msg_id") or 0) == int(reply_to_msg_id)
            and c.get("status") not in ("ЗАВЕРШЁН", "БРАК")
        ]
        if not matched:
            return False

        notified = 0
        for cid, card in matched:
            # 1. Статус → ЗАВЕРШЁН
            try:
                await storage.set_lk_card_status(
                    cid, "ЗАВЕРШЁН", by="accounting_v2_proof",
                )
                await self._refresh_lk_card_post(cid)
            except Exception as e:
                logger.warning("set ЗАВЕРШЁН failed for %s: %s", cid, e)

            # 2. Уведомление клиенту в его work_chat
            wc = card.get("work_chat_id")
            if not wc:
                logger.warning(
                    "payment proof: no work_chat for card=%s — клиента не уведомить",
                    cid,
                )
                continue
            method = (card.get("payment_method") or "").upper()
            bank = card.get("bank") or "—"
            fio = card.get("fio") or "—"
            deal_id = (card.get("deal_id") or "").strip().lstrip("#")
            usdt_addr = (card.get("usdt_address") or "").strip()

            if method == "USDT_TRC20":
                msg = (
                    f"✅ Оплата за ЛК <b>{bank}</b> ({fio}) "
                    f"на ваш USDT TRC20 <b>переведена</b>.\n\n"
                    f"Сумма: <b>{accounting2._fmt_usdt(card.get('price_usdt', 0))}</b>\n"
                )
                if usdt_addr:
                    msg += f"Адрес: <code>{usdt_addr}</code>\n"
                msg += "Проверьте поступление 🙏"
            elif method in ("GUARANTOR_BEFORE", "GUARANTOR_AFTER", "GUARANTOR_AFTER_WORK"):
                if deal_id:
                    msg = (
                        f"✅ Сделка <code>#{deal_id}</code> по ЛК <b>{bank}</b> "
                        f"({fio}) <b>пополнена и отпущена в вашу сторону</b>.\n\n"
                        "Проверьте Conte 🙏"
                    )
                else:
                    msg = (
                        f"✅ ЛК <b>{bank}</b> ({fio}) обработан, выплата ушла. "
                        "Если что-то не пришло — напишите."
                    )
            else:
                msg = (
                    f"✅ ЛК <b>{bank}</b> ({fio}) обработан, выплата ушла."
                )

            try:
                target = await self._resolve_chat_target(wc)
                await self.client.send_message(
                    target, msg, parse_mode="html", link_preview=False,
                )
                notified += 1
                logger.info(
                    "payment proof: notified client work_chat=%s card=%s",
                    wc, cid,
                )
                _e("payment-confirmed", {
                    "card_id": cid, "bank": bank, "fio": fio,
                    "method": method, "deal_id": deal_id,
                }, character="accounting", severity="success")
            except Exception as e:
                logger.warning(
                    "payment proof notify failed for card=%s chat=%s: %s",
                    cid, wc, e,
                )

        # Подтверждение работнику в Группе 2
        try:
            await event.reply(
                f"✅ Зафиксировал, карточки → ЗАВЕРШЁН ({len(matched)}). "
                f"Клиентов уведомил: {notified}.",
                parse_mode="html", link_preview=False,
            )
        except Exception as e:
            logger.warning("payment proof ack failed: %s", e)
        return True

    async def _send_help_lk_group(self, event):
        """Полная справка по командам Группы 1 «Личные кабинеты»."""
        text = (
            "📋 <b>Команды Группы 1 «Личные кабинеты»</b>\n"
            "\n"
            "<b>➕ Создать карточку</b>\n"
            "Мульти-строчный (рекомендуется):\n"
            "<pre>Поставщик: @username\n"
            "Банк: Альфа\n"
            "ФИО: Иванов Иван Иванович\n"
            "Цена: 400\n"
            "Метод оплаты: Сделка в конте (после отработки)\n"
            "Номер сделки: -</pre>\n"
            "\n"
            "Однострочный (компактный):\n"
            "<code>АЛЬФА Иванов Иван 400 USDT_TRC20 @ivanov TXxxx</code>\n"
            "\n"
            "<b>Методы оплаты:</b>\n"
            "• <code>USDT_TRC20</code> — выплата на USDT после отработки\n"
            "• <code>GUARANTOR_BEFORE</code> / <i>Сделка в конте (до отработки)</i>\n"
            "• <code>GUARANTOR_AFTER</code> / <i>Сделка в конте (после перевязки)</i>\n"
            "• <code>GUARANTOR_AFTER_WORK</code> / <i>Сделка в конте (после отработки)</i>\n"
            "\n"
            "<b>📦 Массовый импорт</b>\n"
            "<code>/import_lk</code> — несколько строк compact-формата\n"
            "Или несколько мульти-анкет разделённых строкой <code>Поставщик:</code>\n"
            "\n"
            "<b>🔄 Синхронизация клиентов</b>\n"
            "<code>/sync_clients</code> — резолв @username по managed_chats\n"
            "\n"
            "<b>❌ БРАК / 🚫 БЛОК</b>\n"
            "<code>БРАК ОЗОН Иванов причина</code>\n"
            "<code>БЛОК АЛЬФА Петров 50000 описание_как_снять</code>\n"
            "\n"
            "<b>✏️ Дополнить / изменить поле карточки</b>\n"
            "По <code>#lkNNN</code>:\n"
            "<code>#lk044 сделка #12345</code>\n"
            "<code>#lk044 адрес TXxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx</code>\n"
            "<code>#lk044 цена 350</code>\n"
            "<code>#lk044 метод USDT_TRC20</code>\n"
            "<code>#lk044 банк ОЗОН</code> / <code>#lk044 ФИО Иванов И.И.</code>\n"
            "Либо reply на анкету (без <code>#lkNNN</code>):\n"
            "<code>сделка #12345</code> / <code>адрес TX...</code> / "
            "<code>цена 350</code> / <code>метод USDT_TRC20</code>\n"
            "<i>Если задаёшь номер сделки для ОТРАБОТАН + GUARANTOR_AFTER_WORK — "
            "карточка автоматически переходит в ПОПОЛНИТЬ_И_ОТПУСТИТЬ.</i>\n"
            "\n"
            "<b>🗑 Удалить ОДНУ карточку</b>\n"
            "<code>Ассистент удали ЛК #lk010</code>\n"
            "<code>Ассистент удали ЛК Альфа Иванов</code>\n"
            "Или reply на анкету + <code>удалить</code>\n"
            "→ удаляется и анкета в чате, и строка зачёркивается в сводке импорта.\n"
            "\n"
            "<b>🗑 Удалить ВСЕ карточки</b>\n"
            "<code>Ассистент удалить все ЛК</code> — затем по <code>+</code> от "
            "Тимона и от админа (двойное подтверждение).\n"
            "\n"
            "<b>📋 Сводка действий</b>\n"
            "<code>сводка</code> / <code>действия</code> / <code>дневной отчёт</code>\n"
            "\n"
            "<b>💸 Команды по движениям средств</b>\n"
            "<code>что пополнить</code> — сделки в конте ДО/ПОСЛЕ перевязки, "
            "ждущие пополнения\n"
            "<code>что оплатить</code> — USDT TRC20 после отработки (очередь выплат)\n"
            "<code>что пополнить и отпустить</code> — гарант после отработки\n"
            "\n"
            "<b>🔍 Детали по банку</b>\n"
            "<code>детали ОЗОН</code> / <code>раскрой Альфа</code> / "
            "<code>подробно Точка</code>\n"
            "\n"
            "<b>✅ Подтверждение оплаты / отпуска</b>\n"
            "Reply на анкету ЛК (или на наш «💸 ОПЛАТИТЕ» / «🔓 ОТПУСТИТЕ»):\n"
            "любой текст-пруф («оплачено», «отпустил», скрин) →\n"
            "карточка → <b>🏁 ЗАВЕРШЁН</b>, клиент в work_chat получает уведомление\n"
            "(«Оплата за ЛК … переведена» / «Сделка #N пополнена и отпущена»).\n"
            "\n"
            "<b>ℹ️ Эта помощь</b>\n"
            "<code>помощь</code> / <code>/help</code> / <code>справка</code> / "
            "<code>?</code>"
        )
        try:
            for chunk in _split_text(text, 3900):
                await event.reply(chunk, parse_mode="html", link_preview=False)
                await asyncio.sleep(0.2)
        except Exception as e:
            logger.warning("help_lk reply failed: %s", e)

    async def _send_help_accounting_group(self, event):
        """Полная справка по командам Группы 2 «Бухгалтерия»."""
        text = (
            "💼 <b>Команды Группы 2 «Бухгалтерия»</b>\n"
            "\n"
            "<b>📊 Создать заявку (СТАРТ-отчёт)</b>\n"
            "<pre>заявка 1\n"
            "прием:\n"
            "АЛЬФА - Иванов Иван Иванович - 1050000\n"
            "вывод:\n"
            "ЛОКО - брат братович - 600000\n"
            "ОЗОН - Петров - 400000\n"
            "курс откупа: 80\n"
            "курс выплаты партнёру: 80\n"
            "наша доля: 37</pre>\n"
            "\n"
            "Юзербот:\n"
            "• Посчитает маржу (МЫ_ПОЛУЧИЛИ × 0.98 − ВЫПЛАТА_ПАРТНЁРУ − ОПЛАТА_ЗА_ЛК)\n"
            "• Переведёт все ЛК заявки в <b>ОТРАБОТАН</b> (карточки в Группе 1)\n"
            "• Сделает <b>reply на анкеты ЛК в Группе 1</b> с конкретными "
            "действиями для работника-выплат (USDT-адрес / номер сделки).\n"
            "\n"
            "<b>✏️ Удалить / редактировать заявку</b>\n"
            "<code>удалить заявку 1</code>\n"
            "<code>редактировать заявку 1</code> (или <code>исправить</code> / <code>изменить</code>)\n"
            "\n"
            "<b>💰 Правка цены ЛК в заявке</b>\n"
            "<code>заявка 1 АЛЬФА Иванов 350</code>\n"
            "→ юзербот пересчитает маржу и отредактирует свой исходный отчёт.\n"
            "\n"
            "<i>Сводка действий / детали по банку / подтверждение оплаты — "
            "теперь в Группе 1 ЛК (рядом с карточками).</i>\n"
            "\n"
            "<b>ℹ️ Эта помощь</b>\n"
            "<code>помощь</code> / <code>/help</code> / <code>справка</code> / "
            "<code>?</code>"
        )
        try:
            for chunk in _split_text(text, 3900):
                await event.reply(chunk, parse_mode="html", link_preview=False)
                await asyncio.sleep(0.2)
        except Exception as e:
            logger.warning("help_accounting reply failed: %s", e)

    async def _handle_bank_details(self, event, bank_query: str):
        """Раскрыть детали по конкретному банку: все активные карточки
        этого банка с поставщиком, ФИО, ценой, методом, статусом.
        Используется когда из сводки нужно копнуть глубже."""
        bank_q = (bank_query or "").strip()
        logger.info(
            "bank_details: chat=%s bank_query=%r",
            event.chat_id, bank_q,
        )
        if not bank_q:
            return
        bank_q_lc = bank_q.lower()
        cards = storage.list_lk_cards() or {}
        matches = []
        for cid, c in cards.items():
            bank = (c.get("bank") or "").strip()
            status = c.get("status") or ""
            if status in ("БРАК", "ЗАВЕРШЁН"):
                continue
            if not bank:
                continue
            # Совпадение по подстроке в любую сторону (АЛЬФА ↔ Альфа-Банк)
            bl = bank.lower()
            if bank_q_lc not in bl and bl not in bank_q_lc:
                continue
            matches.append((cid, c))

        if not matches:
            try:
                await event.reply(
                    f"📋 По банку <b>{bank_q}</b> активных карточек не найдено.",
                    parse_mode="html",
                )
            except Exception:
                pass
            return

        # Группируем по статусу для читаемости
        groups: dict = {}
        for cid, c in matches:
            st = c.get("status") or "—"
            groups.setdefault(st, []).append((cid, c))

        # Порядок секций
        order = [
            "ПОПОЛНИТЬ_И_ОТПУСТИТЬ",
            "ОТРАБОТАН",
            "В_РАБОТЕ",
        ]
        # Эмодзи на статус
        st_emoji = {
            "В_РАБОТЕ": "🟢",
            "ОТРАБОТАН": "✅",
            "ПОПОЛНИТЬ_И_ОТПУСТИТЬ": "💎",
            "ОЖИДАЕТ_ПОПОЛНЕНИЯ": "⏳",
            "БЛОК": "🚫",
        }

        lines = [
            f"📋 <b>Детали по банку {bank_q.upper()}</b> — найдено: <b>{len(matches)}</b>",
            "",
        ]
        seen_statuses = set()
        for st in order + [s for s in groups if s not in order]:
            if st not in groups:
                continue
            seen_statuses.add(st)
            items = groups[st]
            emoji = st_emoji.get(st, "•")
            label = st.replace("_", " ")
            lines.append(f"{emoji} <b>{label}</b> ({len(items)}):")
            for cid, c in items:
                sup = _fmt_username(
                    c.get("supplier") or c.get("client_username"),
                    fallback="—",
                )
                price = accounting2._fmt_usdt(c.get("price_usdt") or 0)
                method = c.get("payment_method") or "—"
                deal_id = (c.get("deal_id") or "").strip().lstrip("#")
                usdt_addr = (c.get("usdt_address") or "").strip()
                extra = ""
                if deal_id:
                    extra = f" · сделка <code>#{deal_id}</code>"
                elif usdt_addr:
                    extra = f" · <code>{usdt_addr[:14]}…</code>"
                lines.append(
                    f"  • <code>#{cid}</code> {c.get('fio') or '—'} — "
                    f"{sup} — {price} — <i>{method}</i>{extra}"
                )
            lines.append("")

        text = "\n".join(lines).rstrip()
        try:
            for chunk in _split_text(text, 3900):
                await event.reply(chunk, parse_mode="html", link_preview=False)
                await asyncio.sleep(0.2)
        except Exception as e:
            logger.warning("bank_details reply failed: %s", e)

    async def _handle_lk_cmd_to_topup(self, event):
        """🟡 Что пополнить — карточки/сделки с методами «сделка в конте ДО или
        ПОСЛЕ перевязки», ещё не пополненные."""
        try:
            cards = storage.list_lk_cards() or {}
        except Exception as e:
            await event.reply(f"⚠️ storage error: {e}")
            return
        rows = []
        for cid, c in cards.items():
            if not c:
                continue
            method = (c.get("payment_method") or "").upper()
            status = (c.get("status") or "В_РАБОТЕ").upper()
            if method not in ("GUARANTOR_BEFORE", "GUARANTOR_AFTER"):
                continue
            if status not in ("В_РАБОТЕ", "ОТРАБОТАН"):
                continue
            bank = (c.get("bank") or "—").upper()
            fio = c.get("fio") or "—"
            deal_id = (c.get("deal_id") or "").strip() or "—"
            price = c.get("price_usdt") or 0
            method_label_short = (
                "ДО перевязки" if method == "GUARANTOR_BEFORE"
                else "ПОСЛЕ перевязки"
            )
            rows.append(
                f"• <b>{bank}</b> / {fio} / сделка <code>#{deal_id}</code> / "
                f"{price}$ <i>({method_label_short})</i>"
            )
        if not rows:
            await event.reply(
                "🟡 <b>ЧТО ПОПОЛНИТЬ</b>\n\nНет сделок ожидающих пополнения.",
                parse_mode="html",
            )
            return
        head = f"🟡 <b>ЧТО ПОПОЛНИТЬ</b> ({len(rows)} шт.)\n\n"
        await event.reply(head + "\n".join(rows), parse_mode="html")

    async def _handle_lk_cmd_to_pay(self, event):
        """🟢 Что оплатить — карточки в статусе ОТРАБОТАН с методом USDT_TRC20.
        Очередь выплат партнёру в крипте."""
        try:
            cards = storage.list_lk_cards() or {}
        except Exception as e:
            await event.reply(f"⚠️ storage error: {e}")
            return
        rows = []
        for cid, c in cards.items():
            if not c:
                continue
            method = (c.get("payment_method") or "").upper()
            status = (c.get("status") or "В_РАБОТЕ").upper()
            if method != "USDT_TRC20":
                continue
            if status != "ОТРАБОТАН":
                continue
            bank = (c.get("bank") or "—").upper()
            fio = c.get("fio") or "—"
            supplier = (c.get("supplier") or "").lstrip("@")
            usdt_addr = (c.get("usdt_address") or "").strip()
            price = c.get("price_usdt") or 0
            addr_line = (
                f"<code>{usdt_addr}</code>" if usdt_addr else "⚠️ нет адреса"
            )
            rows.append(
                f"• <b>{bank}</b> / {fio} / @{supplier} / "
                f"<b>{price}$</b> → {addr_line}"
            )
        if not rows:
            await event.reply(
                "🟢 <b>ЧТО ОПЛАТИТЬ</b>\n\nНет карточек ожидающих оплаты USDT.",
                parse_mode="html",
            )
            return
        head = f"🟢 <b>ЧТО ОПЛАТИТЬ</b> (USDT TRC20, {len(rows)} шт.)\n\n"
        await event.reply(head + "\n".join(rows), parse_mode="html")

    async def _handle_lk_cmd_to_release(self, event):
        """🔵 Что пополнить и отпустить — карточки ОТРАБОТАН / ПОПОЛНИТЬ_И_ОТПУСТИТЬ
        с GUARANTOR_AFTER_WORK (гарант после отработки)."""
        try:
            cards = storage.list_lk_cards() or {}
        except Exception as e:
            await event.reply(f"⚠️ storage error: {e}")
            return
        rows = []
        for cid, c in cards.items():
            if not c:
                continue
            method = (c.get("payment_method") or "").upper()
            status = (c.get("status") or "В_РАБОТЕ").upper()
            if method != "GUARANTOR_AFTER_WORK":
                continue
            if status not in ("ОТРАБОТАН", "ПОПОЛНИТЬ_И_ОТПУСТИТЬ"):
                continue
            bank = (c.get("bank") or "—").upper()
            fio = c.get("fio") or "—"
            supplier = (c.get("supplier") or "").lstrip("@")
            deal_id = (c.get("deal_id") or "").strip() or "—"
            price = c.get("price_usdt") or 0
            rows.append(
                f"• <b>{bank}</b> / {fio} / @{supplier} / "
                f"сделка <code>#{deal_id}</code> / <b>{price}$</b>"
            )
        if not rows:
            await event.reply(
                "🔵 <b>ЧТО ПОПОЛНИТЬ И ОТПУСТИТЬ</b>\n\nНет сделок в очереди.",
                parse_mode="html",
            )
            return
        head = (
            f"🔵 <b>ЧТО ПОПОЛНИТЬ И ОТПУСТИТЬ</b> "
            f"(гарант после отработки, {len(rows)} шт.)\n\n"
        )
        await event.reply(head + "\n".join(rows), parse_mode="html")

    async def _handle_daily_summary(self, event):
        """Команда «сводка/действия/что оплатить» в Группе 2.

        Пробегает по всем активным карточкам ЛК и группирует по требуемому
        действию. Никаких AI-вызовов — чистый storage read.
        """
        cards = storage.list_lk_cards() or {}
        # Группы действий
        g_pay_usdt: list = []        # ОТРАБОТАН + USDT_TRC20
        g_release_deal: list = []     # ОТРАБОТАН + GUARANTOR (deal_id есть)
        g_fund_release: list = []     # ПОПОЛНИТЬ_И_ОТПУСТИТЬ
        g_wait_client: list = []      # ОТРАБОТАН + GUARANTOR_AFTER_WORK без deal_id
        g_in_work: list = []          # В_РАБОТЕ — ничего делать не нужно, но покажем для контекста

        for cid, c in cards.items():
            status = c.get("status") or ""
            if status in ("БРАК", "ЗАВЕРШЁН"):
                continue
            method = (c.get("payment_method") or "").upper()
            deal_id = (c.get("deal_id") or "").strip().lstrip("#")
            entry = {
                "card_id": cid,
                "supplier": c.get("supplier") or c.get("client_username") or "",
                "bank": c.get("bank") or "—",
                "fio": c.get("fio") or "—",
                "price_usdt": c.get("price_usdt") or 0,
                "deal_id": deal_id,
                "usdt_address": c.get("usdt_address") or "",
                "method": method,
                "status": status,
            }
            if status == "ПОПОЛНИТЬ_И_ОТПУСТИТЬ":
                g_fund_release.append(entry)
            elif status == "ОТРАБОТАН":
                if method == "USDT_TRC20":
                    g_pay_usdt.append(entry)
                elif method in ("GUARANTOR_BEFORE", "GUARANTOR_AFTER") and deal_id:
                    g_release_deal.append(entry)
                elif method == "GUARANTOR_AFTER_WORK" and not deal_id:
                    g_wait_client.append(entry)
                elif method == "GUARANTOR_AFTER_WORK" and deal_id:
                    g_fund_release.append(entry)
                else:
                    g_release_deal.append(entry)
            elif status == "В_РАБОТЕ":
                g_in_work.append(entry)

        def _fmt_entry(e: dict) -> str:
            sup = _fmt_username(e["supplier"], fallback="—")
            extra = ""
            if e.get("deal_id"):
                extra = f" — <code>#{e['deal_id']}</code>"
            elif e.get("usdt_address"):
                extra = f" — <code>{e['usdt_address'][:12]}...</code>"
            return (
                f"• {sup} — <b>{e['bank']}</b> — {e['fio']} — "
                f"{accounting2._fmt_usdt(e['price_usdt'])}{extra}"
            )

        sections = []
        if g_pay_usdt:
            sections.append("💸 <b>Оплатить USDT TRC20:</b>")
            sections.extend(_fmt_entry(e) for e in g_pay_usdt)
            sections.append("")
        if g_release_deal:
            sections.append("🔓 <b>Отпустить сделку:</b>")
            sections.extend(_fmt_entry(e) for e in g_release_deal)
            sections.append("")
        if g_fund_release:
            sections.append("💎 <b>Пополнить и отпустить:</b>")
            sections.extend(_fmt_entry(e) for e in g_fund_release)
            sections.append("")
        if g_wait_client:
            sections.append("⏳ <b>Ждать сделку от клиента:</b>")
            sections.extend(_fmt_entry(e) for e in g_wait_client)
            sections.append("")
        if g_in_work:
            # Агрегация по банкам — длинный список не нужен, всё равно
            # действия по этим ЛК не требуются. Один пробег по entries.
            by_bank: dict = {}
            for e in g_in_work:
                bk = (e.get("bank") or "—").strip()
                by_bank[bk] = by_bank.get(bk, 0) + 1
            sections.append(
                f"🟢 <b>В работе у операционистов:</b> всего <b>{len(g_in_work)}</b>"
            )
            for bank, n in sorted(by_bank.items(), key=lambda x: -x[1]):
                sections.append(f"• <b>{bank}</b> — {n}")
            sections.append("")

        if not sections:
            text = "📋 <b>Действия на сегодня</b>\n\n<i>Нет активных карточек.</i>"
        else:
            text = "📋 <b>Действия на сегодня</b>\n\n" + "\n".join(sections).rstrip()

        try:
            for chunk in _split_text(text, 3900):
                await event.reply(chunk, parse_mode="html", link_preview=False)
                await asyncio.sleep(0.2)
        except Exception as e:
            logger.warning("daily_summary reply failed: %s", e)

    async def _apply_application_v2(self, event, app: dict, replaced=None):
        """Сохранить заявку, посчитать, ответить отчётом, авто-перевести ЛК.

        replaced: (date_str, app_id) если новая заявка заменяет старую (режим
        редактирования). Используется только для пометки в отчёте.
        """
        date_str = app.get("date") or accounting2.today_str()
        lk_cards = storage.list_lk_cards() or {}
        # Сначала сохраняем заявку чтобы получить id (нужен compute для current_app_id)
        full_app = {**app, "date": date_str}
        new_id = await storage.add_application_v2(date_str, full_app)
        full_app["id"] = new_id

        # prev_apps — все заявки за этот день, кроме текущей и заменяемой
        prev_apps_all = storage.get_applications_v2(date_str) or []
        replaced_id = replaced[1] if replaced else None
        prev_apps = [
            p for p in prev_apps_all
            if int(p.get("id", 0)) != int(new_id)
            and int(p.get("id", 0)) != int(replaced_id or 0)
        ]
        computed = accounting2.compute_application_v2(
            full_app, lk_cards, prev_apps=prev_apps,
        )
        full_app["computed"] = computed
        # Обновляем заявку с computed
        await storage.update_application_v2(date_str, new_id, computed=computed)

        report = accounting2.format_application_report_v2(full_app, computed)
        if replaced:
            ed_date, ed_id = replaced
            report = (
                f"♻️ <i>Заменена заявка #{ed_id} ({ed_date}).</i>\n\n" + report
            )

        # Auto-update ЛК → ОТРАБОТАН.
        # Учитываем И приёмный ЛК (intake), И выводные (outputs) — все они
        # реально отработали когда заявка закрыта.
        moved = 0
        intake = app.get("intake") or {}
        intake_list = []
        if intake.get("bank") and intake.get("fio"):
            intake_list.append(intake)
        for o in [*intake_list, *app.get("outputs", [])]:
            cards = storage.find_lk_card(
                bank=o.get("bank") or "", fio=o.get("fio") or ""
            )
            active = [
                c for c in cards
                if c.get("status") not in ("ОТРАБОТАН", "БРАК", "ЗАВЕРШЁН",
                                           "ПОПОЛНИТЬ_И_ОТПУСТИТЬ")
            ]
            if not active:
                continue
            card = active[0]
            cid = card["card_id"]
            method = card.get("payment_method", "")
            if method == "GUARANTOR_AFTER_WORK":
                # Особый случай — сделка после отработки.
                # Идём в work_chat клиента, тегаем, просим создать сделку.
                await storage.set_lk_card_status(
                    cid, "ОТРАБОТАН",
                    by="accounting_v2",
                    last_application_id=new_id,
                )
                await self._refresh_lk_card_post(cid)
                await self._post_action_reply_to_lk_card(cid)
                asyncio.create_task(
                    self._request_post_work_deal(card)
                )
            else:
                await storage.set_lk_card_status(
                    cid, "ОТРАБОТАН",
                    by="accounting_v2",
                    last_application_id=new_id,
                )
                await self._refresh_lk_card_post(cid)
                await self._post_action_reply_to_lk_card(cid)
            moved += 1

        if moved:
            report += f"\n\n🔄 Автоматом → ОТРАБОТАН: <b>{moved}</b> карточек."

        sent = await event.reply(report, parse_mode="html", link_preview=False)
        sent_id = getattr(sent, "id", None)
        if sent_id:
            await storage.update_application_v2(
                date_str, new_id, report_msg_id=int(sent_id)
            )
        logger.info(
            "applied app_v2 id=%s date=%s margin=%.0f$ moved=%d",
            new_id, date_str, computed.get("margin_usdt", 0), moved,
        )
        _e("application-processed", {
            "id": new_id, "date": date_str,
            "margin_usdt": computed.get("margin_usdt", 0),
            "moved": moved,
        }, character="accounting", severity="success")

        # Раньше тут шёл reply со списком действий в Группе 2.
        # Сейчас reply делается на анкету КАЖДОЙ карточки в Группе 1
        # через _post_action_reply_to_lk_card (вызывается выше при смене
        # статуса). Группа 2 — только отчёты, никаких действий-инструкций.

    async def _send_post_application_actions(
        self, event, app: dict, parent_msg_id: Optional[int],
    ):
        """После отчёта в Группе 2 — reply со списком действий по каждому ЛК.

        Для каждого банка+ФИО находим карточку в Группе 1 и формируем
        инструкцию по `payment_method`:
          • USDT_TRC20 → «ОПЛАТИТЕ на адрес ...»
          • GUARANTOR_BEFORE/AFTER (есть deal_id) → «ОТПУСТИТЕ сделку #N»
          • GUARANTOR_AFTER_WORK без deal_id → «ОЖИДАЙТЕ создания сделки»
          • ПОПОЛНИТЬ_И_ОТПУСТИТЬ → «ПОПОЛНИТЕ И ОТПУСТИТЕ #N»
        """
        intake = app.get("intake") or {}
        outputs = app.get("outputs") or []
        intake_list = []
        if intake.get("bank") and intake.get("fio"):
            intake_list.append(intake)
        all_lks = [*intake_list, *outputs]
        if not all_lks:
            return

        lines = ["💼 <b>Действия по заявке:</b>", ""]
        cards_with_msg: list = []  # [(card_id, line_idx)] — для msg_id update
        for o in all_lks:
            bank = (o.get("bank") or "").strip()
            fio = (o.get("fio") or "").strip()
            cards = storage.find_lk_card(bank=bank, fio=fio)
            if not cards:
                lines.append(
                    f"⚠️ <b>{bank} {fio}</b> — карточка не найдена в Группе 1"
                )
                continue
            card = cards[0]
            cid = card.get("card_id", "?")
            method = (card.get("payment_method") or "").upper()
            status = card.get("status") or ""
            deal_id = (card.get("deal_id") or "").strip().lstrip("#")
            usdt_addr = (card.get("usdt_address") or "").strip()
            supplier = card.get("supplier") or card.get("client_username") or ""
            supplier_tag = _fmt_username(supplier, fallback="—")

            action = ""
            if status == "ПОПОЛНИТЬ_И_ОТПУСТИТЬ" and deal_id:
                action = (
                    f"💎 <b>ПОПОЛНИТЬ И ОТПУСТИТЬ</b> сделку <code>#{deal_id}</code>"
                )
            elif method == "USDT_TRC20":
                if usdt_addr:
                    action = (
                        f"💸 <b>ОПЛАТИТЕ USDT TRC20</b> на адрес: "
                        f"<code>{usdt_addr}</code>"
                    )
                else:
                    action = (
                        "⚠️ <b>USDT TRC20</b> — адрес не задан, уточни у клиента"
                    )
            elif method in ("GUARANTOR_BEFORE", "GUARANTOR_AFTER") and deal_id:
                action = (
                    f"🔓 <b>ОТПУСТИТЕ</b> сделку <code>#{deal_id}</code>"
                )
            elif method == "GUARANTOR_AFTER_WORK":
                if deal_id:
                    action = (
                        f"💎 <b>ПОПОЛНИТЬ И ОТПУСТИТЬ</b> сделку <code>#{deal_id}</code>"
                    )
                else:
                    action = (
                        "⏳ <b>Ожидаем создания сделки клиентом</b> — "
                        "когда AI получит номер, статус сменится"
                    )
            else:
                action = (
                    f"⚠️ Метод <code>{method or '—'}</code> — действие неясно"
                )

            lines.append(
                f"• {supplier_tag} — <b>{bank}</b> — {fio} — "
                f"{accounting2._fmt_usdt(card.get('price_usdt', 0))}\n  └─ {action}"
            )
            cards_with_msg.append(cid)

        body = "\n".join(lines)
        try:
            kwargs = {"parse_mode": "html", "link_preview": False}
            if parent_msg_id:
                kwargs["reply_to"] = int(parent_msg_id)
            sent = await event.reply(body, **kwargs)
        except Exception as e:
            logger.warning("post-app actions reply failed: %s", e)
            return

        # Сохранить msg_id reply'я в каждую упомянутую карточку — для
        # последующего распознавания reply-пруфа от работника-выплат.
        sent_id = getattr(sent, "id", None)
        if sent_id:
            for cid in cards_with_msg:
                try:
                    await storage.update_lk_card(
                        cid, accounting_reply_msg_id=int(sent_id)
                    )
                except Exception as e:
                    logger.warning(
                        "save accounting_reply_msg_id for %s failed: %s",
                        cid, e,
                    )

    async def _request_post_work_deal(self, card: dict):
        """ЛК отработан, метод = GUARANTOR_AFTER_WORK → теги клиента в work_chat,
        просим создать сделку. Дальше AI получит номер и обновит карточку."""
        wc = card.get("work_chat_id")
        client_uname = card.get("client_username") or ""
        cid = card.get("card_id", "?")
        supplier = card.get("supplier") or ""
        if not wc:
            # Пытаемся резолвить work_chat прямо сейчас по @supplier —
            # если карточка создавалась вручную ДО регистрации клиента в managed_chats.
            resolved = self._resolve_work_chat_by_supplier(supplier)
            if resolved.get("work_chat_id"):
                wc = resolved["work_chat_id"]
                if not client_uname:
                    client_uname = resolved.get("client_username") or ""
                # Сохраним привязку чтобы в следующий раз сразу нашлось.
                try:
                    await storage.update_lk_card(
                        cid,
                        work_chat_id=wc,
                        client_id=resolved.get("client_id", 0),
                        client_username=client_uname,
                    )
                    logger.info(
                        "post-work deal: late-resolved work_chat=%s for card=%s "
                        "supplier=@%s", wc, cid, supplier,
                    )
                except Exception as e:
                    logger.warning("post-work deal: late-resolve save failed: %s", e)
            else:
                logger.warning(
                    "post-work deal SKIPPED for card=%s: no work_chat_id "
                    "(supplier=@%s — клиент с таким @username не зарегистрирован "
                    "в managed_chats; добавьте его через /start или используйте "
                    "/import_lk после того как клиент создаст рабочую беседу)",
                    cid, supplier or "—",
                )
                return
        msg = (
            f"✅ Ваш ЛК <b>{card.get('bank')}</b> ({card.get('fio')}) "
            f"<b>отработан</b>.\n\n"
        )
        client_tag = _fmt_username(client_uname, fallback="")
        if client_tag:
            msg += f"{client_tag} "
        msg += (
            f"создайте, пожалуйста, гарант-сделку в Conte и пришлите номер — "
            f"оформим вашу выплату ({accounting2._fmt_usdt(card.get('price_usdt', 0))})."
        )
        try:
            target = await self._resolve_chat_target(wc)
            await self.client.send_message(
                target, msg, parse_mode="html", link_preview=False,
            )
            logger.info("post-work deal request sent for card=%s chat=%s", cid, wc)
        except Exception as e:
            logger.warning("post-work deal request failed for card=%s: %s", cid, e)

    # === Перевяз ЛК — авто-форвард в Отработка аккаунтов ===

    _PEREVYAZ_RE = re.compile(r"перевяз\s+лк\s+выполнен", re.I)
    _PEREVYAZ_FIO_RE = re.compile(r"фио\s*:?[\s]*(.+)", re.I)
    _PEREVYAZ_LK_RE = re.compile(r"лк\s*:?[\s]*(.+)", re.I)

    # Маркеры что клиент завершил заполнение анкеты в @PrideCONTROLE_bot —
    # анкета отправлена операционистам, AI снова может вести диалог.
    _CRM_READY_MARKERS = (
        "отдать в работу",
        "отправлено на обработку",
        "отправлена на обработку",
        "принят в работу",
        "принята в работу",
        "приняты в работу",
    )

    async def _maybe_release_silent_on_crm_ready(self, event, chat_id):
        """Если в managed-чате пришло сообщение с маркером «анкета готова»,
        снимаем AI silent mode для этого чата."""
        from storage import _norm_chat_id  # noqa
        key = _norm_chat_id(chat_id)
        if key not in self._ai_silent_until:
            return
        text = ((event.message and event.message.text) or "").lower()
        if not text:
            return
        if any(m in text for m in self._CRM_READY_MARKERS):
            self._ai_silent_until.pop(key, None)
            logger.info(
                "AI silent mode lifted for chat=%s — CRM ready marker detected",
                chat_id,
            )

    async def _maybe_autodetect_deal_id(
        self, event, chat_id, text_raw: str,
    ) -> bool:
        """Страховка от ситуации «AI забыл вызвать record_deal».

        Если клиент прислал чистый номер сделки (5-7 цифр с # или без), и в
        этом work_chat есть подходящая карточка ЛК (ОТРАБОТАН + GUARANTOR_AFTER_WORK) —
        автоматически применяем.

        Логика выбора КАРТОЧКИ когда их несколько:
          1) Если только 1 candidate — применяем сразу.
          2) Если несколько — пытаемся определить по контексту сообщения:
             - reply-to на анкету конкретного ЛК → берём ту
             - в тексте упомянут банк (Альфа/Озон/ВТБ/...) → фильтруем по банку
             - в тексте упомянут ФИО (любая часть) → фильтруем по ФИО
          3) Если после фильтра остался ровно 1 — применяем.
          4) Иначе — шлём клиенту вопрос «уточните для какого ЛК» и НЕ применяем."""
        # Чистое число 5-7 цифр: «78802», «#78802 Альфа», «78802 Иванов»
        import re as _re_dd
        m = _re_dd.match(r"^\s*#?\s*(\d{5,7})\b", text_raw)
        if not m:
            return False
        deal_id = m.group(1)
        # Собираем ВСЕ подходящие карточки в этом work_chat
        try:
            from storage import _norm_chat_id as _norm
            wc_norm = _norm(chat_id)
            candidates = []
            for cid, c in (storage.list_lk_cards() or {}).items():
                if _norm(c.get("work_chat_id") or 0) != wc_norm:
                    continue
                if (c.get("payment_method") or "").upper() != "GUARANTOR_AFTER_WORK":
                    continue
                if (c.get("status") or "").upper() not in (
                    "ОТРАБОТАН", "ПОПОЛНИТЬ_И_ОТПУСТИТЬ",
                ):
                    continue
                candidates.append((cid, c))
            if not candidates:
                return False
        except Exception as e:
            logger.warning("autodetect deal_id: card lookup fail: %s", e)
            return False

        text_lc = text_raw.lower()
        # Случай 1: ровно одна — применяем
        if len(candidates) == 1:
            cid, c = candidates[0]
        else:
            # Несколько кандидатов — пытаемся отфильтровать
            filtered = list(candidates)

            # Фильтр 1: reply-to на анкету конкретного ЛК
            try:
                reply_msg = await event.get_reply_message()
                if reply_msg and reply_msg.id:
                    reply_msg_id = int(reply_msg.id)
                    by_msg = [
                        (cid, c) for (cid, c) in filtered
                        if int(c.get("lk_group_msg_id") or 0) == reply_msg_id
                    ]
                    if by_msg:
                        filtered = by_msg
            except Exception:
                pass

            # Фильтр 2: упомянут банк в тексте (только если ещё больше 1)
            if len(filtered) > 1:
                by_bank = [
                    (cid, c) for (cid, c) in filtered
                    if (c.get("bank") or "").lower() in text_lc
                ]
                if by_bank:
                    filtered = by_bank

            # Фильтр 3: упомянуто ФИО или фамилия (только если ещё больше 1)
            if len(filtered) > 1:
                def fio_match(c):
                    fio = (c.get("fio") or "").lower()
                    if not fio:
                        return False
                    # Любая часть ФИО длиной 3+ символа в тексте
                    for token in fio.split():
                        if len(token) >= 3 and token in text_lc:
                            return True
                    return False
                by_fio = [(cid, c) for (cid, c) in filtered if fio_match(c)]
                if by_fio:
                    filtered = by_fio

            if len(filtered) == 1:
                cid, c = filtered[0]
                logger.info(
                    "autodetect deal_id: matched 1 of %d candidates by context",
                    len(candidates),
                )
            else:
                # Не смогли однозначно определить → спрашиваем клиента
                logger.info(
                    "autodetect deal_id: %d candidates, %d after filter — asking client",
                    len(candidates), len(filtered),
                )
                try:
                    lines = [
                        f"⚠️ Получил номер <b>#{deal_id}</b>, но у вас сейчас "
                        f"несколько ЛК ожидают номер сделки:",
                        "",
                    ]
                    for cid_x, c_x in candidates:
                        lines.append(
                            f"• <b>{c_x.get('bank') or '—'}</b> · "
                            f"{c_x.get('fio') or '—'} · {c_x.get('price_usdt') or 0} USDT"
                        )
                    lines.append("")
                    lines.append(
                        "Уточните: для какого ЛК этот номер? Напишите банк или ФИО — "
                        "например «<code>78802 Альфа</code>» или «<code>78802 Иванов</code>»."
                    )
                    target = await self._resolve_chat_target(chat_id)
                    await self.client.send_message(
                        target, "\n".join(lines), parse_mode="html",
                        link_preview=False,
                    )
                except Exception as e:
                    logger.warning("autodetect deal_id: ambiguity msg fail: %s", e)
                return True  # обработано (вопрос задан), AI не нужен

        logger.info(
            "AUTO deal_id: chat=%s card=%s deal_id=%s (метод GUARANTOR_AFTER_WORK)",
            chat_id, cid, deal_id,
        )
        # 1) Обновить deal_id
        try:
            await storage.update_lk_card(cid, deal_id=deal_id)
        except Exception as e:
            logger.warning("autodetect deal_id: update_lk_card fail: %s", e)

        # 2) Статус → ПОПОЛНИТЬ_И_ОТПУСТИТЬ
        try:
            await storage.set_lk_card_status(
                cid, "ПОПОЛНИТЬ_И_ОТПУСТИТЬ", by="auto_deal_id",
            )
        except Exception as e:
            logger.warning("autodetect deal_id: status change fail: %s", e)

        # 3) В очередь fund_release (storage.add_payout сам дедуплицирует)
        try:
            await storage.add_payout("fund_release", {
                "card_id": cid,
                "bank": c.get("bank") or "",
                "fio": c.get("fio") or "",
                "supplier": c.get("supplier") or "",
                "work_chat_id": c.get("work_chat_id") or 0,
                "amount_usdt": float(c.get("price_usdt") or 0),
                "deal_id": deal_id,
            })
        except Exception as e:
            logger.warning("autodetect deal_id: add_payout fail: %s", e)

        # 4) Также сохраняем как deal-запись в storage.deals (если ещё нет)
        try:
            if not storage.get_deal(deal_id):
                await storage.add_deal(
                    deal_id=deal_id,
                    client_username=(c.get("client_username") or c.get("supplier") or "").lstrip("@"),
                    fio=c.get("fio") or "",
                    bank=c.get("bank") or "",
                    amount=str(c.get("price_usdt") or 0),
                    fee="",
                    method="GUARANTOR_AFTER_WORK",
                    status="ПОПОЛНИТЬ",
                    work_chat_id=int(c.get("work_chat_id") or 0),
                )
        except Exception as e:
            logger.warning("autodetect deal_id: add_deal fail: %s", e)

        # 5) Refresh анкеты в группе ЛК + reply на анкету
        try:
            await self._refresh_lk_card_post(cid)
        except Exception:
            pass
        try:
            await self._post_action_reply_to_lk_card(cid)
        except Exception:
            pass

        # 6) Отправляем клиенту подтверждение
        try:
            bank = c.get("bank") or "—"
            amount = c.get("price_usdt") or 0
            msg = (
                f"✅ <b>Номер сделки #{deal_id} зафиксирован.</b>\n\n"
                f"ЛК <b>{bank}</b> · сумма <b>{amount} USDT</b>.\n\n"
                f"Ожидайте — <b>мы пополним сделку</b> и сразу её отпустим. "
                f"Средства придут в гаранте в течение дня."
            )
            target = await self._resolve_chat_target(chat_id)
            await self.client.send_message(
                target, msg, parse_mode="html", link_preview=False,
            )
        except Exception as e:
            logger.warning("autodetect deal_id: client notify fail: %s", e)

        # 7) Эмит-событие на дашборд
        try:
            _e("auto-deal-id-applied", {
                "chat_id": chat_id, "card_id": cid, "deal_id": deal_id,
                "bank": c.get("bank"), "fio": c.get("fio"),
            }, character="chat", severity="success")
        except Exception:
            pass

        return True

    async def _maybe_autodetect_payment_method(
        self, event, chat_id, text_lc: str,
    ) -> bool:
        """Страховка от ситуации «AI забыл вызвать set_payment_method».

        Если клиент в своём сообщении явно упомянул метод
        (гарант/USDT/TRC20/конте/...) — юзербот САМ ставит payment_method и,
        если в storage есть pending_perevyaz (банк+ФИО) — сразу создаёт
        карточку ЛК в Группе 1.

        Возвращает True если метод был определён."""
        # Маркеры USDT
        is_usdt = any(m in text_lc for m in (
            "usdt", "trc20", "трц20", "трц 20", "трон",
        ))
        # Маркеры гаранта/Conte
        is_guarantor = any(m in text_lc for m in (
            "гарант", "конте", "контик", "сделк", "conte",
        ))
        # Доп. сигналы про сделку без «сделки»: «можно после», «после отработки»
        # сами по себе не определяют гарант, нужно сочетание с одним из marker'ов выше.
        if not (is_usdt or is_guarantor):
            return False

        method = ""
        # Маркеры что клиент требует гарант СРАЗУ / ДО отработки (override-триггеры).
        # Они могут переключить метод даже если уже стоит GUARANTOR_AFTER_WORK.
        before_markers = (
            "сразу", "сначала сделк", "до перевяз", "перед перевяз",
            "вперёд", "вперед", "до отработ", "перед отработ",
            "пополните прям", "пополните сейчас", "хочу гарант сейчас",
            "хочу гарант прям", "иначе ресн", "сначала деньг",
            "не отдам логин пока", "сразу гарант", "гарант прям",
        )
        is_demand_before = any(m in text_lc for m in before_markers)

        if is_usdt:
            method = "USDT_TRC20"
        elif is_guarantor or is_demand_before:
            # Уточняем разновидность
            if is_demand_before:
                method = "GUARANTOR_BEFORE"
            elif any(m in text_lc for m in ("после отработ", "когда отработа", "по отработ")):
                method = "GUARANTOR_AFTER_WORK"
            elif any(m in text_lc for m in ("после перевяз", "после")):
                method = "GUARANTOR_AFTER"
            else:
                method = "GUARANTOR_AFTER_WORK"  # default для PRIDE — сделка после отработки

        if not method:
            return False

        # Сохраняем method в managed_chats
        await storage.set_chat_payment_info(chat_id, method=method)
        logger.info(
            "auto-detect: chat=%s method=%s (text=%r)",
            chat_id, method, text_lc[:80],
        )

        # Если перевязка уже была — забираем pending и создаём карточку.
        pending = await storage.pop_pending_perevyaz(chat_id)
        if pending and (pending.get("bank") or pending.get("fio")):
            # У USDT_TRC20 нужен адрес — если не задан, карточку всё равно
            # создаём (можно дозаполнить адрес позже отдельным сообщением AI).
            try:
                chat_info_fresh = storage.get_chat_info(chat_id) or {}
                await self._create_lk_card_from_perevyaz(
                    event, chat_info_fresh,
                    lk_text=pending.get("bank", ""),
                    fio_text=pending.get("fio", ""),
                )
                logger.info(
                    "auto-detect: card created from pending perevyaz for chat=%s",
                    chat_id,
                )
            except Exception as e:
                logger.warning(
                    "auto-detect: create card failed for chat=%s: %s",
                    chat_id, e,
                )
        return True

    # Параллельный триггер: операционист @pride_sys01/@pride_sys02 пишет
    # в клиентский work-чат сообщение вида «Иванов Иван — Альфа — перевяз
    # успешен» (без участия CRM-бота). Равноценно маркеру от CRM-бота.
    _WORKER_USERNAMES_FOR_PEREVYAZ = ("pride_sys01", "pride_sys02")

    # Регексы для распознавания позитивного маркера перевязки в свободном
    # тексте от sys01/sys02. Любого из них достаточно.
    _PEREVYAZ_TRIGGER_WORDS_RE = re.compile(
        r"\b(перевяз\w*\s+(успешн\w+|выполнен\w*|готов\w*|заверш\w+|сделан\w*)|"
        r"(успешн\w+|готов\w*|заверш\w+|сделан\w*)\s+перевяз\w*|"
        r"перевязал\w*|перепривяз\w+\s+(успешн\w+|готов\w*)|"
        r"бинд\s+(успешн\w+|готов\w*|выполнен\w*))",
        re.I,
    )

    # Известные банки — берём через словари алиасов из accounting2, плюс
    # дополнительные русские формы.
    _KNOWN_BANK_TOKENS = (
        "альфа", "альфа-банк", "alfa", "alpha",
        "озон", "ozon",
        "райф", "райффайзен", "raif",
        "точка", "tochka",
        "уралсиб", "uralsib",
        "локо", "loko", "локо-банк",
        "втб", "vtb",
        "русский стандарт", "rus_standard",
        "бкс", "bks", "дело", "delo", "убрир", "ubrir",
        "сбер", "sber",
        "тинькофф", "тиньк", "tinkoff",
    )

    def _extract_bank_token(self, text: str) -> str:
        """Найти первое упоминание банка из _KNOWN_BANK_TOKENS в тексте.
        Возвращает каноничное имя или пустую строку."""
        if not text:
            return ""
        tl = text.lower()
        # Сортируем по длине (длинные сначала чтоб «альфа-банк» не подменился «альфа»).
        for tok in sorted(self._KNOWN_BANK_TOKENS, key=len, reverse=True):
            if tok in tl:
                # Канонизируем
                if tok in ("альфа", "альфа-банк", "alfa", "alpha"):
                    return "Альфа"
                if tok in ("озон", "ozon"):
                    return "ОЗОН"
                if tok in ("райф", "райффайзен", "raif"):
                    return "Райффайзен"
                if tok in ("точка", "tochka"):
                    return "Точка"
                if tok in ("уралсиб", "uralsib"):
                    return "Уралсиб"
                if tok in ("локо", "loko", "локо-банк"):
                    return "Локо"
                if tok in ("втб", "vtb"):
                    return "ВТБ"
                if tok in ("русский стандарт", "rus_standard"):
                    return "Русский стандарт"
                if tok in ("бкс", "bks"):
                    return "БКС"
                if tok in ("дело", "delo"):
                    return "Дело"
                if tok in ("убрир", "ubrir"):
                    return "УБРИР"
                if tok in ("сбер", "sber"):
                    return "Сбер"
                if tok in ("тинькофф", "тиньк", "tinkoff"):
                    return "Тинькофф"
        return ""

    # ФИО — последовательность из 2-4 русских/латинских слов с заглавных букв,
    # каждое >=2 букв. Жадно ловим, потом валидируем.
    _FIO_RE = re.compile(
        r"\b([А-ЯЁA-Z][а-яёa-z]{1,}(?:\s+[А-ЯЁA-Z][а-яёa-z]{1,}){1,3})\b"
    )

    def _extract_fio(self, text: str) -> str:
        """Достать ФИО из текста (2-4 слова с заглавных).
        Останавливаемся на токене банка чтобы не залезть в «Локо/Альфа» и т.п."""
        if not text:
            return ""
        # 1) Если строка содержит «—», «-», «|» — режем по ним и берём первый
        # сегмент с валидным ФИО. Это покрывает «Иванов Иван — Альфа — перевяз».
        segments = re.split(r"\s*[—–\-|]\s*", text)
        candidate_text = ""
        for seg in segments:
            seg = seg.strip()
            if not seg:
                continue
            # Если сегмент содержит токен банка — это банк, не ФИО
            if self._extract_bank_token(seg):
                continue
            # Если сегмент содержит триггер «перевяз/успешен/...» — это статус
            if self._PEREVYAZ_TRIGGER_WORDS_RE.search(seg):
                continue
            # Иначе — кандидат на ФИО
            candidate_text = seg
            break

        # Если разделителей не было — пробуем по всему тексту, но обрезаем по
        # первой найденной банк-метке
        if not candidate_text:
            candidate_text = text
            bank_lc = self._extract_bank_token(text).lower()
            if bank_lc:
                tl = candidate_text.lower()
                # Ищем позицию любого из алиасов банка
                cut = len(candidate_text)
                for tok in sorted(self._KNOWN_BANK_TOKENS, key=len, reverse=True):
                    pos = tl.find(tok)
                    if pos != -1 and pos < cut:
                        cut = pos
                candidate_text = candidate_text[:cut]

        m = self._FIO_RE.search(candidate_text)
        if not m:
            return ""
        fio = m.group(1).strip()
        bad_lc = {"перевяз успешен", "перевяз выполнен", "перевяз готов"}
        if fio.lower() in bad_lc:
            return ""
        return fio

    async def _maybe_handle_perevyaz_by_worker(
        self, event, chat_info: dict, sender_username: str,
    ) -> bool:
        """Если @pride_sys01 / @pride_sys02 написал в work-чат сообщение типа
        «Иванов Иван Иванович — Альфа — перевяз успешен» — это равноценно
        маркеру от CRM-бота. Создаём карточку ЛК."""
        if not sender_username:
            return False
        if sender_username.lower() not in self._WORKER_USERNAMES_FOR_PEREVYAZ:
            return False

        text = (event.message.text or "")
        if not text:
            return False

        # 1) Должен быть позитивный маркер «перевяз успешен/готов/выполнен»
        if not self._PEREVYAZ_TRIGGER_WORDS_RE.search(text):
            return False

        # 2) Должен быть распознаваемый банк
        bank = self._extract_bank_token(text)
        if not bank:
            logger.info(
                "perevyaz-by-worker: chat=%s, sender=%s — есть триггер но не нашёл банк, скип",
                event.chat_id, sender_username,
            )
            return False

        # 3) ФИО — желательно (но не обязательно: можно дозаполнить позже)
        fio = self._extract_fio(text)

        # 4) Дубликат? Если на этого клиента уже есть активная карточка с тем
        # же банком — не создаём, только лог.
        try:
            chat_id = event.chat_id
            existing = storage.find_lk_card(
                bank=bank, work_chat_id=chat_id,
            ) or []
            active = [
                c for c in existing
                if (c.get("status") or "").upper() not in ("ЗАВЕРШЁН", "ЗАВЕРШЕН", "ОТМЕНЁН", "ОТМЕНЕН")
            ]
            if active:
                logger.info(
                    "perevyaz-by-worker: chat=%s — дубликат, активная карточка %s уже есть",
                    chat_id, active[0].get("card_id"),
                )
                return True  # отдаём True чтоб не упасть в обычный AI-flow
        except Exception as e:
            logger.warning("perevyaz-by-worker: dup check failed: %s", e)

        logger.info(
            "perevyaz-by-worker detected: chat=%s, sender=@%s, bank=%r, fio=%r",
            event.chat_id, sender_username, bank, fio,
        )

        # Снимаем silent mode (это явный сигнал что флоу прошёл дальше)
        try:
            from storage import _norm_chat_id  # noqa
            self._ai_silent_until.pop(_norm_chat_id(event.chat_id), None)
        except Exception:
            pass

        try:
            await self._create_lk_card_from_perevyaz(
                event, chat_info, lk_text=bank, fio_text=fio,
            )
            _e("lk-from-worker-perevyaz", {
                "chat_id": event.chat_id,
                "worker": sender_username,
                "bank": bank,
                "fio": fio,
            }, severity="info")
        except Exception as e:
            logger.warning("perevyaz-by-worker: card creation failed: %s", e)
        return True

    async def _maybe_handle_perevyaz(self, event, chat_info: dict) -> bool:
        """Триггер Перевяз ЛК выполнен — парсим банк и ФИО прямо из текста
        CRM-бота и создаём карточку в Группе 1 ЛК."""
        text = (event.message.text or "")
        if not self._PEREVYAZ_RE.search(text):
            return False
        lk_text = ""
        fio_text = ""
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            if not lk_text:
                m_lk = self._PEREVYAZ_LK_RE.match(stripped)
                if m_lk:
                    lk_text = m_lk.group(1).strip()
                    continue
            if not fio_text:
                m_fio = self._PEREVYAZ_FIO_RE.match(stripped)
                if m_fio:
                    fio_text = m_fio.group(1).strip()
                    continue
        # Подчищаем lk_text — берём только первое слово (название банка),
        # вдруг CRM-бот в одной строке написал «Озон Альфа Точка» (мульти-LK).
        if lk_text:
            lk_text = lk_text.split()[0]
        logger.info(
            "perevyaz detected: lk=%r fio=%r chat=%s",
            lk_text, fio_text, event.chat_id,
        )
        # Перевязка = безусловный конец CRM-флоу, снимаем silent.
        try:
            from storage import _norm_chat_id  # noqa
            self._ai_silent_until.pop(_norm_chat_id(event.chat_id), None)
        except Exception:
            pass
        try:
            await self._create_lk_card_from_perevyaz(
                event, chat_info, lk_text=lk_text, fio_text=fio_text,
            )
        except Exception as e:
            logger.warning("perevyaz: lk-card creation failed: %s", e)
        return True

    # === Dashboard command worker ===

    async def _dashboard_command_worker(self):
        """Опрашивает storage.dashboard_commands каждые 5 сек, выполняет
        pending команды. Распознаёт:
          • «рассылка работчатам: TEXT» / «broadcast workchats: TEXT»
          • «рассылка боту: TEXT» / «broadcast bot: TEXT»
          • «рассылка незарегистрированным: TEXT» / «inactive: TEXT»
          • «stats» — отдаёт быструю стату
          • «pause ai» / «resume ai»
          • «список клиентов» / «list clients»
        Результат записывает обратно в команду + emit-event.
        """
        await asyncio.sleep(3)  # дать боту полностью встать
        while True:
            try:
                # Подтянуть свежие команды (на случай если api.py их добавил
                # из другого процесса)
                try:
                    storage.reload_sync()
                except Exception:
                    pass
                pending = storage.get_pending_dashboard_commands()
                for cmd in pending:
                    cmd_id = cmd.get("id")
                    text = (cmd.get("text") or "").strip()
                    if not text:
                        await storage.mark_dashboard_command_done(
                            cmd_id, "empty", status="skipped",
                        )
                        continue
                    # SMS-команды обрабатывает ОТДЕЛЬНЫЙ worker в crm_bot.py
                    # (нужен bot-инстанс CRM-бота для отправки сообщений
                    # с inline-кнопками). Пропускаем — пусть CRM подберёт.
                    if re.match(r"^__sms_(advance|reset)\s+\S+\s*$", text, re.I):
                        continue
                    try:
                        result = await self._execute_dashboard_command(text)
                    except Exception as e:
                        result = f"error: {e}"
                        logger.warning("dashboard cmd failed: %s — %s", text, e)
                    await storage.mark_dashboard_command_done(cmd_id, result)
                    try:
                        _e("dashboard-cmd-done", {
                            "id": cmd_id, "text": text[:120], "result": result[:200],
                        })
                    except Exception:
                        pass
            except Exception as e:
                logger.warning("_dashboard_command_worker tick failed: %s", e)
            # 1.5s poll — для быстрой синхронизации дашборд↔ТГ
            await asyncio.sleep(1.5)

    async def _execute_dashboard_command(self, text: str) -> str:
        """Парсит и выполняет одну команду из дашборда. Возвращает текст
        результата (для отображения в дашборде)."""
        low = text.lower().strip()

        # ===== STATS =====
        if low in ("stats", "стата", "статистика"):
            cards = storage.list_lk_cards() or {}
            managed = storage.state.get("managed_chats") or {}
            ai_stats = storage.state.get("ai_stats", {}) or {}
            return (
                f"📊 LK={len(cards)} · chats={len(managed)} · "
                f"AI replies={ai_stats.get('replies_total', 0)} · "
                f"users={len(storage.list_bot_users())}"
            )

        # ===== ОЧИСТКА БУХГАЛТЕРИИ (маржа / заявки / AI errors) =====
        if low in (
            "очистить маржу", "очисти маржу", "обнули маржу",
            "очистить бухгалтерию", "очисти бухгалтерию", "обнули бухгалтерию",
            "очистить заявки", "очисти заявки",
            "clear margin", "clear accounting", "reset accounting",
        ):
            apps_count = sum(
                len(v or []) for v in (storage.state.get("applications_v2") or {}).values()
            )
            # Чистим ТОЛЬКО заявки V2 — маржа считается из них и обнулится сама.
            # deals_stats, AI stats, ЛК, чаты, прайс — НЕ трогаем.
            storage.state["applications_v2"] = {}
            await storage._save_unlocked()
            return (
                f"🧹 Бухгалтерия очищена: удалено заявок V2 = {apps_count}, "
                "маржа сброшена на 0. Карточки ЛК, сделки, чаты, прайс — не тронуты."
            )
        if low in (
            "очистить ai", "очисти ai", "очистить статистику ai", "очисти статистику ai",
            "очистить ошибки ai", "очисти ошибки ai",
            "clear ai", "reset ai stats", "clear ai errors",
        ):
            old = dict(storage.state.get("ai_stats") or {})
            storage.state["ai_stats"] = {}
            storage.state["escalate_stats"] = {}
            await storage._save_unlocked()
            return (
                f"🧹 AI стата сброшена. Было: replies={old.get('replies_total', 0)}, "
                f"errors={old.get('errors_total', 0)}"
            )
        if low in (
            "очистить всё", "очисти всё", "сбросить статистику",
            "reset all stats", "clear all stats",
        ):
            storage.state["applications_v2"] = {}
            storage.state["deals_stats"] = {}
            storage.state["ai_stats"] = {}
            storage.state["escalate_stats"] = {}
            storage.state["writeback_stats"] = {}
            storage.state["funnel_stats"] = {}
            await storage._save_unlocked()
            return "🧹 Все счётчики сброшены: маржа, AI, эскалации, writeback, воронка."

        # ===== PAUSE/RESUME AI =====
        if low in ("pause ai", "пауза ai", "ai off", "stop ai"):
            await storage.set_ai_enabled(False)
            return "✅ AI выключен"
        if low in ("resume ai", "старт ai", "ai on", "start ai"):
            await storage.set_ai_enabled(True)
            return "✅ AI включён"

        # ===== LIST CLIENTS =====
        if low in ("list clients", "список клиентов", "клиенты"):
            users = storage.list_bot_users()
            inactive = storage.list_inactive_bot_users()
            return (
                f"👥 Bot users: {len(users)} (не зашли в чат: {len(inactive)}) · "
                f"Work chats: {len(storage.state.get('managed_chats') or {})}"
            )

        # ===== BROADCAST: work chats =====
        m = re.match(
            r"^\s*(?:рассылка|broadcast)\s*"
            r"(?:в\s*)?(?:работ\w*\s*чат\w*|workchats?|work[-_ ]?chats?|"
            r"рабочим|клиентам)\s*[:\-]\s*(.+)$",
            text, re.I | re.S,
        )
        if m:
            msg = m.group(1).strip()
            return await self._broadcast_workchats(msg)

        # ===== BROADCAST: bot users =====
        m = re.match(
            r"^\s*(?:рассылка|broadcast)\s*"
            r"(?:по\s*)?(?:бот\w*|bot|всем)\s*[:\-]\s*(.+)$",
            text, re.I | re.S,
        )
        if m:
            msg = m.group(1).strip()
            return await self._broadcast_bot_users(msg, only_inactive=False)

        # ===== BROADCAST: inactive (не зашли в чат) =====
        m = re.match(
            r"^\s*(?:рассылка|broadcast)\s*"
            r"(?:незарегистрир\w*|inactive|не\s+(?:вошли|зашли))\s*[:\-]\s*(.+)$",
            text, re.I | re.S,
        )
        if m:
            msg = m.group(1).strip()
            return await self._broadcast_bot_users(msg, only_inactive=True)

        # ===== AUDIT =====
        if low in ("/audit", "audit", "аудит", "/аудит"):
            return self._do_audit()

        # ===== DAILY REPORT =====
        if low in ("/daily_report", "/daily", "сводка", "/сводка"):
            return self._do_daily_report()

        # ===== OPERATOR REPORT =====
        m = re.match(r"^/operator\s+@?(\S+)\s*$", text, re.I)
        if m:
            return self._do_operator_report(m.group(1))

        # ===== FIND CARD =====
        m = re.match(r"^/find_card\s+(.+)$", text, re.I)
        if m:
            return self._do_find_card(m.group(1))

        # ===== CHANGE LK STATUS via command =====
        m = re.match(r"^#?(lk\d+)\s+статус\s+(.+)$", text, re.I)
        if m:
            cid = m.group(1).lower()
            new_status = m.group(2).strip().upper()
            allowed = {"В_РАБОТЕ", "ОТРАБОТАН", "ПОПОЛНИТЬ_И_ОТПУСТИТЬ",
                       "ОЖИДАЕТ_ПОПОЛНЕНИЯ",
                       "ЗАВЕРШЁН", "ЗАВЕРШЕН", "БРАК", "БЛОК",
                       "БЛОК_БЕЗ_ОТРАБОТКИ"}
            if new_status not in allowed:
                return f"⚠️ статус {new_status} не разрешён. Допустимы: {sorted(allowed)}"
            # ПОПОЛНИТЬ_И_ОТПУСТИТЬ — алиас для ОТРАБОТАН. Метод оплаты НЕ трогаем.
            if new_status == "ПОПОЛНИТЬ_И_ОТПУСТИТЬ":
                new_status = "ОТРАБОТАН"
            ok = await storage.set_lk_card_status(cid, new_status, by="leo")
            if not ok:
                return f"⚠️ карточка #{cid} не найдена"
            try:
                await self._refresh_lk_card_post(cid)
            except Exception:
                pass
            # Side-effects при БЛОК_БЕЗ_ОТРАБОТКИ: отмена сделки + клиент
            if new_status == "БЛОК_БЕЗ_ОТРАБОТКИ":
                try:
                    await self._handle_block_no_work_actions(cid)
                except Exception as e:
                    logger.warning(
                        "block_no_work side-effects card=%s: %s", cid, e,
                    )
            elif new_status in ("БЛОК", "БРАК", "ОТРАБОТАН", "ЗАВЕРШЁН", "ЗАВЕРШЕН"):
                # Простое уведомление клиенту о смене статуса
                try:
                    await self._notify_client_status_change(cid, new_status)
                except Exception as e:
                    logger.warning(
                        "status_change_notify card=%s status=%s: %s",
                        cid, new_status, e,
                    )
            return f"✅ #{cid} → {new_status}"

        # ===== PAYOUTS: «отпущено #lk0001» или «отпущено 12345» =====
        # Шлётся из api.py /api/payouts/released когда менеджер жмёт «Отпустить» на дашборде.
        # Закрывает гарант-выплату, шлёт клиенту уведомление, статус ЛК → ЗАВЕРШЁН.
        m = re.match(r"^отпущено\s+#?(\S+)\s*$", text, re.I)
        if m:
            return await self._mark_guarantor_released(m.group(1).strip())

        # ===== PAYOUTS: «выплачено #lk0001 TXHASH» =====
        # Шлётся из api.py /api/payouts/usdt_paid после ввода TronScan хеша.
        # Шлёт клиенту ссылку на tronscan, статус ЛК → ЗАВЕРШЁН.
        m = re.match(r"^выплачено\s+#?(lk\d+)\s+(\S+)\s*$", text, re.I)
        if m:
            return await self._mark_usdt_paid(m.group(1).lower(), m.group(2).strip())

        # ===== PAYOUTS: «сделка #DEAL пополнена SUM» =====
        # Шлётся из api.py /api/payouts/deal_funded когда менеджер ввёл сумму пополнения.
        # Не закрывает выплату — только помечает что сделка пополнена (ждём «Отпустить»).
        m = re.match(r"^сделка\s+#?(\S+)\s+пополнена\s+([0-9]+(?:\.[0-9]+)?)\s*$", text, re.I)
        if m:
            deal_id = m.group(1).lstrip("#").strip()
            amount = float(m.group(2))
            match = storage.find_payout_by_deal(deal_id)
            if not match:
                return f"⚠️ сделка #{deal_id} не найдена в очередях"
            q, item = match
            try:
                await storage.update_payout(q, item["id"], funded_amount=amount, funded_at=time.time())
            except Exception as e:
                return f"⚠️ update_payout failed: {e}"
            return f"✅ сделка #{deal_id} помечена пополненной на {amount} USDT (жду «Отпустить»)"

        # ===== INTERNAL: sync LK card (от api.py — после правок в дашборде) =====
        m = re.match(r"^__sync_lk_card\s+(lk\d+)\s*$", text, re.I)
        if m:
            cid = m.group(1).lower()
            try:
                storage.reload_sync()
            except Exception:
                pass
            try:
                ok = await self._refresh_lk_card_post(cid)
                return (
                    f"✅ #{cid} sync ok" if ok
                    else f"⚠️ #{cid}: sync не удался (нет lk_group/msg_id?)"
                )
            except Exception as e:
                logger.warning("__sync_lk_card %s failed: %s", cid, e)
                return f"⚠️ #{cid}: sync exception {e!r}"

        # ===== INTERNAL: notify client about status change (от api.py) =====
        # api.py отправляет это после смены статуса карточки с дашборда.
        # Шлёт клиенту сообщение в его work_chat по шаблону из _notify_client_status_change.
        m = re.match(r"^__notify_status\s+(lk\d+)\s+(\S+)\s*$", text, re.I)
        if m:
            cid = m.group(1).lower()
            new_status = m.group(2).strip().upper()
            try:
                storage.reload_sync()
            except Exception:
                pass
            try:
                ok = await self._notify_client_status_change(cid, new_status)
                return (
                    f"✅ #{cid} → клиент уведомлён о {new_status}" if ok
                    else f"⚠️ #{cid}: уведомление {new_status} не отправлено (work_chat не резолвится?)"
                )
            except Exception as e:
                logger.warning("__notify_status %s %s failed: %s", cid, new_status, e)
                return f"⚠️ #{cid} {new_status}: notify exception {e!r}"

        # ===== HELPDESK: форсированный ответ через PRIDE ASSISTANT =====
        # (когда менеджер выбрал toggle "от Ассистента")
        m = re.match(r"^__support_reply_assistant\s+(-?\d+)\s+(\d+)\s+(.+)$", text, re.I | re.DOTALL)
        if m:
            chat_id = int(m.group(1))
            manager_uid = int(m.group(2))
            reply_text = m.group(3).strip()
            if not reply_text:
                return "⚠️ пустой текст"
            try:
                target = await self._resolve_chat_target(chat_id)
                sent_pa = await self.client.send_message(
                    target, reply_text, parse_mode="html", link_preview=False,
                )
                pa_sid = (self._me.id if self._me else 0)
                try:
                    from storage import _norm_chat_id as _nrm
                    cache = storage.state.setdefault("support_msg_cache", {})
                    arr = cache.setdefault(str(_nrm(chat_id)), [])
                    msg_entry = {
                        "id": getattr(sent_pa, "id", int(time.time()*1000)),
                        "ts": time.time(),
                        "role": "assistant",
                        "author": "PRIDE ASSISTANT",
                        "sender_id": pa_sid,
                        "text": reply_text[:4000],
                        "via": "pride_assistant_forced",
                    }
                    arr.append(msg_entry)
                    if len(arr) > 200:
                        del arr[: len(arr) - 200]
                    await storage._save_unlocked()
                    _e("support-message", {
                        "chat_id": str(_nrm(chat_id)),
                        "raw_chat_id": chat_id,
                        "msg": msg_entry,
                    }, character="chat", severity="info")
                except Exception as ec:
                    logger.warning("cache assistant reply fail: %s", ec)
                return f"✅ отправлено как PRIDE ASSISTANT в чат {chat_id}"
            except Exception as e:
                logger.warning("assistant reply fail: %s", e)
                return f"⚠️ send failed: {e}"

        # ===== HELPDESK: отправка ответа менеджера в work_chat клиента =====
        # Команда вида: __support_reply <chat_id> <manager_uid> <текст>
        m = re.match(r"^__support_reply\s+(-?\d+)\s+(\d+)\s+(.+)$", text, re.I | re.DOTALL)
        if m:
            chat_id = int(m.group(1))
            manager_uid = int(m.group(2))
            reply_text = m.group(3).strip()
            if not reply_text:
                return "⚠️ пустой текст"

            async def _cache_outgoing(real_msg_id, sender_id_local, author_label, via):
                """Сразу пишем outgoing в кэш + emit SSE — incoming-handler outgoing не ловит."""
                try:
                    from storage import _norm_chat_id as _nrm
                    cache = storage.state.setdefault("support_msg_cache", {})
                    key = str(_nrm(chat_id))
                    arr = cache.setdefault(key, [])
                    msg_entry = {
                        "id": int(real_msg_id) if real_msg_id else int(time.time() * 1000),
                        "ts": time.time(),
                        "role": "worker",
                        "author": author_label,
                        "sender_id": int(sender_id_local or 0),
                        "text": reply_text[:4000],
                        "via": via,
                    }
                    arr.append(msg_entry)
                    if len(arr) > 200:
                        del arr[: len(arr) - 200]
                    await storage._save_unlocked()
                    _e("support-message", {
                        "chat_id": str(_nrm(chat_id)) if hasattr(__import__('storage'), '_norm_chat_id') else chat_id,
                        "raw_chat_id": chat_id,
                        "msg": msg_entry,
                    }, character="chat", severity="info")
                    return msg_entry
                except Exception as e:
                    logger.warning("cache outgoing failed: %s", e)
                    return None

            # Используем сессию менеджера если она есть, иначе PRIDE ASSISTANT.
            sent_via = "pride_assistant"
            try:
                from storage import decrypt_session
                sess_data = storage.get_worker_session(manager_uid)
                if sess_data and sess_data.get("string_session"):
                    try:
                        decrypted = decrypt_session(sess_data["string_session"])
                        if decrypted:
                            from telethon import TelegramClient
                            from telethon.sessions import StringSession
                            mgr_cli = TelegramClient(
                                StringSession(decrypted), config.API_ID, config.API_HASH,
                            )
                            await mgr_cli.connect()
                            if await mgr_cli.is_user_authorized():
                                sent = await mgr_cli.send_message(
                                    chat_id, reply_text, parse_mode="html",
                                    link_preview=False,
                                )
                                # Запоминаем sender_id и first_name для красивой подписи
                                mgr_me = None
                                try:
                                    mgr_me = await mgr_cli.get_me()
                                except Exception:
                                    pass
                                mgr_sid = (mgr_me.id if mgr_me else manager_uid)
                                mgr_name = (
                                    (getattr(mgr_me, "first_name", None) or "")
                                    + (" " + getattr(mgr_me, "last_name", "") if mgr_me and getattr(mgr_me, "last_name", None) else "")
                                ).strip() if mgr_me else "Менеджер"
                                try:
                                    await mgr_cli.disconnect()
                                except Exception:
                                    pass
                                sent_via = f"manager_{manager_uid}"
                                # КРИТИЧНО: сразу пишем в кэш — incoming handler outgoing не ловит
                                await _cache_outgoing(
                                    getattr(sent, "id", None),
                                    mgr_sid, mgr_name or "Менеджер", sent_via,
                                )
                                logger.info(
                                    "[helpdesk] manager_session=%s sent reply to chat=%s msg_id=%s",
                                    manager_uid, chat_id, getattr(sent, "id", "?"),
                                )
                                _e("support-manager-reply", {
                                    "chat_id": chat_id, "manager_uid": manager_uid,
                                    "text": reply_text[:200], "via": sent_via,
                                }, character="chat", severity="info")
                                return f"✅ отправлено через сессию менеджера в чат {chat_id}"
                            else:
                                logger.warning(
                                    "manager session %s not authorized, falling back",
                                    manager_uid,
                                )
                                try:
                                    await mgr_cli.disconnect()
                                except Exception:
                                    pass
                    except Exception as e:
                        logger.warning(
                            "manager session reply failed (%s), fallback to PRIDE ASSISTANT",
                            e,
                        )
                # Fallback: через основной userbot (PRIDE ASSISTANT)
                target = await self._resolve_chat_target(chat_id)
                sent_pa = await self.client.send_message(
                    target, reply_text, parse_mode="html", link_preview=False,
                )
                pa_sid = (self._me.id if self._me else 0)
                # КРИТИЧНО: outgoing от PRIDE ASSISTANT тоже не ловится incoming handler'ом
                await _cache_outgoing(
                    getattr(sent_pa, "id", None),
                    pa_sid, "PRIDE ASSISTANT", "pride_assistant",
                )
                logger.info(
                    "[helpdesk] PRIDE ASSISTANT (fallback) replied to chat=%s for manager=%s msg_id=%s",
                    chat_id, manager_uid, getattr(sent_pa, "id", "?"),
                )
                _e("support-manager-reply", {
                    "chat_id": chat_id, "manager_uid": manager_uid,
                    "text": reply_text[:200], "via": sent_via,
                }, character="chat", severity="info")
                return f"✅ отправлено (fallback PRIDE ASSISTANT) в чат {chat_id}"
            except Exception as e:
                logger.warning("support reply send fail: %s", e)
                return f"⚠️ send failed: {e}"

        # ===== HELPDESK: подгрузка истории сообщений из Telethon в кэш =====
        m = re.match(r"^__support_fetch_messages\s+(-?\d+)(?:\s+(\d+))?\s*$", text, re.I)
        if m:
            cid = int(m.group(1))
            lim = min(int(m.group(2) or 100), 200)
            try:
                target = await self._resolve_chat_target(cid)
                chat_info = storage.get_chat_info(cid) or {}
                client_id_x = chat_info.get("client_id") or 0
                msgs_out = []
                async for msg in self.client.iter_messages(target, limit=lim):
                    if not msg or not msg.text:
                        continue
                    sender_id_x = msg.sender_id or 0
                    if self._me and sender_id_x == self._me.id:
                        role, author = "assistant", "PRIDE ASSISTANT"
                    elif sender_id_x == client_id_x:
                        role = "client"
                        author = chat_info.get("client_name") or "Клиент"
                    else:
                        role = "worker"
                        try:
                            s = await msg.get_sender()
                            first = getattr(s, "first_name", None) or ""
                            last = getattr(s, "last_name", None) or ""
                            author = (first + (" " + last if last else "")).strip() or "Сотрудник"
                        except Exception:
                            author = "Сотрудник"
                    msgs_out.append({
                        "id": msg.id,
                        "ts": msg.date.timestamp() if msg.date else time.time(),
                        "role": role,
                        "author": author,
                        "sender_id": sender_id_x,
                        "text": (msg.text or "")[:4000],
                    })
                msgs_out.reverse()
                from storage import _norm_chat_id as _nrm
                cache = storage.state.setdefault("support_msg_cache", {})
                cache[str(_nrm(cid))] = msgs_out
                await storage._save_unlocked()
                _e("support-msgs-loaded", {
                    "chat_id": cid, "count": len(msgs_out),
                }, character="chat", severity="info")
                return f"✅ history loaded: {len(msgs_out)} msgs for {cid}"
            except Exception as e:
                logger.warning("support_fetch_messages %s fail: %s", cid, e)
                return f"⚠️ fetch failed: {e}"

        # ===== HELPDESK: уведомление клиента когда менеджер взял чат =====
        m = re.match(r"^__support_take_notify\s+(-?\d+)\s+(\d+)\s*(.*)$", text, re.I)
        if m:
            cid = int(m.group(1))
            mgr_uid = int(m.group(2))
            mgr_label = (m.group(3) or "").strip() or f"менеджер #{mgr_uid}"
            try:
                target = await self._resolve_chat_target(cid)
                notice = f"✅ <b>{mgr_label}</b> присоединился к чату.\nОн ответит вам в ближайшее время."
                sent_n = await self.client.send_message(
                    target, notice, parse_mode="html", link_preview=False,
                )
                # Кэш для дашборда
                try:
                    from storage import _norm_chat_id as _nrm
                    cache_dict = storage.state.setdefault("support_msg_cache", {})
                    arr_n = cache_dict.setdefault(str(_nrm(cid)), [])
                    msg_n = {
                        "id": getattr(sent_n, "id", int(time.time()*1000)),
                        "ts": time.time(),
                        "role": "assistant",
                        "author": "PRIDE ASSISTANT",
                        "sender_id": (self._me.id if self._me else 0),
                        "text": f"✅ {mgr_label} присоединился к чату.",
                    }
                    arr_n.append(msg_n)
                    if len(arr_n) > 200:
                        del arr_n[: len(arr_n) - 200]
                    await storage._save_unlocked()
                    _e("support-message", {
                        "chat_id": str(_nrm(cid)),
                        "raw_chat_id": cid,
                        "msg": msg_n,
                    }, character="chat", severity="info")
                except Exception as ec:
                    logger.warning("cache take-notice fail: %s", ec)
                return f"✅ take-notice отправлен в чат {cid}"
            except Exception as e:
                logger.warning("__support_take_notify %s fail: %s", cid, e)
                return f"⚠️ take-notice failed: {e}"

        # ===== HELPDESK: уведомление клиента при передаче чата =====
        m = re.match(r"^__support_transfer_notify\s+(-?\d+)\s+(\w+)\|\|\|(.+)$", text, re.I | re.DOTALL)
        if m:
            cid = int(m.group(1))
            new_dept = m.group(2).strip()
            dept_label = m.group(3).strip()
            try:
                target = await self._resolve_chat_target(cid)
                notice = (
                    f"↪️ <b>Ваш запрос передан в {dept_label}.</b>\n"
                    f"Специалист подключится в ближайшее время."
                )
                sent_t = await self.client.send_message(
                    target, notice, parse_mode="html", link_preview=False,
                )
                from storage import _norm_chat_id as _nrm
                cache_dict = storage.state.setdefault("support_msg_cache", {})
                arr_t = cache_dict.setdefault(str(_nrm(cid)), [])
                msg_t = {
                    "id": getattr(sent_t, "id", int(time.time()*1000)),
                    "ts": time.time(),
                    "role": "assistant",
                    "author": "PRIDE ASSISTANT",
                    "sender_id": (self._me.id if self._me else 0),
                    "text": f"↪️ Запрос передан в {dept_label}.",
                }
                arr_t.append(msg_t)
                if len(arr_t) > 200:
                    del arr_t[: len(arr_t) - 200]
                await storage._save_unlocked()
                _e("support-message", {
                    "chat_id": str(_nrm(cid)),
                    "raw_chat_id": cid,
                    "msg": msg_t,
                }, character="chat", severity="info")
                return f"✅ transfer-notify {cid} → {new_dept}"
            except Exception as e:
                logger.warning("__support_transfer_notify %s fail: %s", cid, e)
                return f"⚠️ transfer-notify failed: {e}"

        # ===== HELPDESK: после закрытия — прощальное сообщение клиенту =====
        m = re.match(r"^__support_after_close\s+(-?\d+)\s*$", text, re.I)
        if m:
            cid = int(m.group(1))
            try:
                from storage import _norm_chat_id as _nrm
                norm_cid = _nrm(cid)
                # МОМЕНТАЛЬНОЕ возвращение AI:
                # 1) Снимаем silent
                # 2) Сбрасываем _last_worker_ts чтобы idle прошёл сразу
                # 3) _last_client_msg_ts оставляем (нужен для логики)
                self._ai_silent_until.pop(norm_cid, None)
                self._last_worker_ts.pop(norm_cid, None)
                if hasattr(self, "_last_worker_msg_ts"):
                    self._last_worker_msg_ts.pop(norm_cid, None)
                # Сбрасываем счётчик AI-ответов — следующий ответ снова hint про оператора
                try:
                    await storage.reset_ai_reply_count(cid)
                except Exception:
                    pass
                # Прощальное сообщение клиенту
                farewell = (
                    "👋 <b>Оператор покинул чат.</b>\n\n"
                    "Желаем вам всего доброго!\n\n"
                    "💬 <i>Если захотите снова связаться с оператором — просто "
                    "напишите «Ассистент позови оператора».</i>"
                )
                try:
                    target = await self._resolve_chat_target(cid)
                    sent = await self.client.send_message(
                        target, farewell, parse_mode="html", link_preview=False,
                    )
                    # Кэш + SSE
                    try:
                        cache_dict = storage.state.setdefault("support_msg_cache", {})
                        arr_f = cache_dict.setdefault(str(_nrm(cid)), [])
                        msg_f = {
                            "id": getattr(sent, "id", int(time.time()*1000)),
                            "ts": time.time(),
                            "role": "assistant",
                            "author": "PRIDE ASSISTANT",
                            "sender_id": (self._me.id if self._me else 0),
                            "text": "👋 Оператор покинул чат. Желаем вам всего доброго! "
                                    "Если захотите снова связаться с оператором — "
                                    "напишите «Ассистент позови оператора».",
                        }
                        arr_f.append(msg_f)
                        if len(arr_f) > 200:
                            del arr_f[: len(arr_f) - 200]
                        await storage._save_unlocked()
                        _e("support-message", {
                            "chat_id": str(_nrm(cid)),
                            "raw_chat_id": cid,
                            "msg": msg_f,
                        }, character="chat", severity="info")
                    except Exception as ec:
                        logger.warning("cache farewell fail: %s", ec)
                except Exception as e:
                    logger.warning("send farewell fail: %s", e)
                _e("support-chat-closed-side", {
                    "chat_id": cid, "ai": "resumed",
                }, character="chat", severity="info")
                return f"✅ chat {cid} closed, farewell sent, AI silence cleared"
            except Exception as e:
                logger.warning("support_after_close %s fail: %s", cid, e)
                return f"⚠️ {e}"

        # ===== ОТРАБОТАН → уведомить клиента в work_chat (от exchange_request) =====
        m = re.match(r"^__notify_client_otrabotan\s+(-?\d+)\s+(lk\d+)\s*$", text, re.I)
        if m:
            wc = int(m.group(1))
            cid = m.group(2).lower()
            try:
                card = storage.get_lk_card(cid) if hasattr(storage, "get_lk_card") else None
                if not card:
                    return f"⚠️ lk_card {cid} не найден"
                method = (card.get("payment_method") or "").upper()
                if method == "USDT_TRC20":
                    action = "💸 USDT TRC20 будет переведён в ближайшее время"
                elif method == "GUARANTOR_AFTER_WORK":
                    action = "💼 Пополним сделку в Конте и отпустим"
                elif method in ("GUARANTOR_BEFORE", "GUARANTOR_AFTER"):
                    action = "💼 Сделка в Конте будет пополнена"
                else:
                    action = "💰 Оплата произведена согласно методу"
                bank = (card.get("bank") or "—").upper()
                fio = card.get("fio") or "—"
                notify_text = (
                    f"✅ <b>ЛК ОТРАБОТАН</b>\n\n"
                    f"🏦 <b>{bank}</b> / {fio}\n"
                    f"{action}.\n\n"
                    f"<i>Спасибо за работу!</i>"
                )
                target = await self._resolve_chat_target(wc)
                await self.client.send_message(target, notify_text, parse_mode="html", link_preview=False)
                _e("client-notified-otrabotan", {"chat_id": wc, "card_id": cid, "method": method}, character="lk", severity="info")
                return f"✅ notified chat={wc} card={cid}"
            except Exception as e:
                logger.warning("notify_client_otrabotan %s %s: %s", wc, cid, e)
                return f"⚠️ notify failed: {e}"

        # ===== CRM-БОТ → публикация анкеты в Группу 1 ЛК после перевязки =====
        # Команда ставится crm_bot._queue_anketa_post_via_userbot после
        # успешной перевязки. Userbot публикует карточку ЛК в lk_group_id.
        m = re.match(r"^__crm_post_anketa\s+([\w\-]+)\s*$", text, re.I)
        if m:
            drop_id = m.group(1)
            try:
                lk_gid = storage.get_lk_group_id()
                if not lk_gid:
                    logger.warning("__crm_post_anketa: lk_group_id не задан в storage")
                    return f"⚠️ lk_group_id не настроен в storage"
                drop = storage.get_crm_drop(drop_id) if hasattr(storage, "get_crm_drop") else None
                if not drop:
                    return f"⚠️ drop {drop_id} not found"
                # Получаем список ЛК этого дропа
                lks = storage.list_crm_drop_lks(drop_id=drop_id) if hasattr(storage, "list_crm_drop_lks") else {}
                # Найдём lk_card_ids для этого drop'а
                lk_card_ids = list(drop.get("lk_card_ids") or [])
                if not lk_card_ids:
                    # Fallback: ищем карточки по supplier+fio (создались только что)
                    fio = drop.get("fio") or ""
                    owner = storage.get_crm_owner(drop.get("owner_id", "")) or {}
                    supplier = owner.get("username") or ""
                    for c in (storage.list_lk_cards() or {}).values():
                        if (c.get("supplier") or "").lstrip("@").lower() == supplier.lstrip("@").lower() \
                                and (c.get("fio") or "") == fio:
                            lk_card_ids.append(c.get("card_id") or c.get("id"))
                if not lk_card_ids:
                    logger.warning("__crm_post_anketa: нет lk_card для drop=%s", drop_id)
                    return f"⚠️ no lk_card for drop {drop_id}"

                target = await self._resolve_chat_target(lk_gid)
                posted = 0
                for cid in lk_card_ids:
                    card = storage.get_lk_card(cid) if hasattr(storage, "get_lk_card") else None
                    if not card:
                        continue
                    # Текст карточки для Группы 1 ЛК (анкета)
                    bank = card.get("bank") or "—"
                    fio = card.get("fio") or "—"
                    supplier = (card.get("supplier") or "").lstrip("@")
                    price = card.get("price_usdt") or 0
                    method = card.get("payment_method") or "уточняется"
                    status = card.get("status") or "В_РАБОТЕ"
                    text_card = (
                        f"📋 <b>Карточка #{cid}</b>\n"
                        f"🏦 Банк: <b>{bank}</b>\n"
                        f"👤 ФИО: <b>{fio}</b>\n"
                        f"🤝 Поставщик: <code>@{supplier}</code>\n"
                        f"💰 Цена: <b>{price}$</b>\n"
                        f"💳 Метод оплаты: <b>{method}</b>\n"
                        f"📊 Статус: <b>{status}</b>\n"
                        f"<i>(карточка автоматически создана после перевязки)</i>"
                    )
                    try:
                        msg = await self.client.send_message(
                            target, text_card, parse_mode="html", link_preview=False,
                        )
                        # Сохраним msg_id для последующих edit'ов
                        try:
                            await storage.update_lk_card(
                                cid, lk_group_msg_id=msg.id,
                            )
                        except Exception:
                            pass
                        posted += 1
                        logger.info(
                            "[crm_post_anketa] posted lk_card=%s drop=%s msg=%s",
                            cid, drop_id, msg.id,
                        )
                        _e("lk-card-posted-to-group", {
                            "card_id": cid, "drop_id": drop_id,
                            "bank": bank, "fio": fio,
                            "msg_id": msg.id,
                        }, character="chat", severity="info")
                    except Exception as e:
                        logger.warning(
                            "[crm_post_anketa] post failed for card=%s: %s",
                            cid, e,
                        )
                return f"✅ posted {posted}/{len(lk_card_ids)} cards to lk_group for drop {drop_id}"
            except Exception as e:
                logger.exception("__crm_post_anketa failed for %s: %s", drop_id, e)
                return f"⚠️ crm_post_anketa error: {e}"

        # ===== INTERNAL: handle БЛОК_БЕЗ_ОТРАБОТКИ side-effects (от api.py) =====
        m = re.match(r"^__handle_block_no_work\s+(lk\d+)\s*$", text, re.I)
        if m:
            cid = m.group(1).lower()
            try:
                storage.reload_sync()
            except Exception:
                pass
            try:
                ok = await self._handle_block_no_work_actions(cid)
                return (
                    f"✅ #{cid}: БЛОК_БЕЗ_ОТРАБОТКИ обработан" if ok
                    else f"⚠️ #{cid}: side-effects БЛОК_БЕЗ_ОТРАБОТКИ не отработали"
                )
            except Exception as e:
                logger.warning("__handle_block_no_work %s failed: %s", cid, e)
                return f"⚠️ #{cid}: block_no_work exception {e!r}"

        return f"⚠️ unknown command: {text[:60]}"

