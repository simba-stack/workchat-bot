"""System-wide health checker.

Run on:
  - Every bot startup (auto-post в HEALTH_CHAT_ID если задан в env)
  - По команде /healthcheck в админ-чате
  - Через REST: GET /api/health/full

Каждая проверка возвращает {name, status, message, fix_hint}:
  status:
    'ok'   — всё работает
    'warn' — работает но есть нюансы (не критично)
    'fail' — поломано, нужен фикс

Главный entrypoint: HealthChecker.run_all() → list of dict.
"""
import os
import json
import asyncio
import logging
import importlib
import inspect
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)


class HealthChecker:
    """Прогоняет все системы и собирает структурный отчёт."""

    def __init__(self):
        self.results: list[dict] = []

    def _add(self, name: str, status: str, message: str = "", fix_hint: str = ""):
        self.results.append({
            "name": name,
            "status": status,  # 'ok' | 'warn' | 'fail'
            "message": message,
            "fix_hint": fix_hint,
        })

    # ========== Проверки ==========

    def check_python_modules(self):
        """Проверка что все ключевые python-модули импортируются."""
        modules = [
            "config", "storage", "brain", "memory", "accounting2",
            "userbot", "bot", "api", "event_bus", "learn", "leo",
            "admin_router", "crm_bot",
        ]
        for mod_name in modules:
            try:
                importlib.import_module(mod_name)
                self._add(f"Модуль {mod_name}.py", "ok", "импортируется")
            except Exception as e:
                self._add(
                    f"Модуль {mod_name}.py", "fail",
                    f"импорт упал: {e!r}",
                    fix_hint=(
                        f"Открой `{mod_name}.py` и проверь синтаксис "
                        f"(`python -m py_compile {mod_name}.py`). "
                        f"Обычно проблема в truncation файла или missing import."
                    ),
                )

    def check_userbot_methods(self):
        """UserbotService должен иметь все ключевые методы."""
        required = [
            "start", "stop", "create_work_chat",
            "_send_welcome", "_handle_chat_action",
            "_dashboard_command_worker", "_execute_dashboard_command",
            "_tool_set_payment_method", "_tool_record_deal",
            "_tool_create_lk_card", "_tool_add_partner_to_crm",
            "_mark_usdt_paid", "_mark_guarantor_released",
            "_notify_client_status_change", "_handle_block_no_work_actions",
            "_create_lk_card_from_perevyaz", "_request_post_work_deal",
        ]
        try:
            from userbot import UserbotService
        except Exception as e:
            self._add(
                "UserbotService class", "fail",
                f"не импортируется: {e!r}",
                fix_hint="Userbot.py поломан — проверь синтаксис файла.",
            )
            return
        missing = []
        for m in required:
            if not hasattr(UserbotService, m):
                missing.append(m)
        if missing:
            self._add(
                "UserbotService методы", "fail",
                f"отсутствуют: {', '.join(missing)}",
                fix_hint=(
                    "Метод был удалён при рефакторинге. Восстанови из git: "
                    f"`git log --all -S 'async def {missing[0]}' --oneline` "
                    f"→ найди коммит → `git show <hash>:userbot.py` → скопируй обратно."
                ),
            )
        else:
            self._add(
                "UserbotService методы", "ok",
                f"все {len(required)} методов на месте",
            )

    def check_storage_methods(self):
        """Storage должен иметь все методы используемые в коде."""
        required = [
            "list_lk_cards", "update_lk_card", "set_lk_card_status",
            "add_payout", "list_payouts", "find_payout_by_card", "find_payout_by_deal",
            "update_payout", "remove_payout", "dedupe_payouts",
            "get_workers", "get_worker_role", "list_worker_roles", "set_worker_role",
            "register_chat", "remove_managed_chat",
            "get_lk_card", "add_lk_card", "list_deals",
            "enqueue_dashboard_command", "get_pending_dashboard_commands",
            "mark_dashboard_command_done",
        ]
        try:
            from storage import storage
        except Exception as e:
            self._add(
                "Storage instance", "fail",
                f"не импортируется: {e!r}",
                fix_hint="storage.py поломан — проверь синтаксис.",
            )
            return
        missing = [m for m in required if not hasattr(storage, m)]
        if missing:
            self._add(
                "Storage методы", "fail",
                f"отсутствуют: {', '.join(missing)}",
                fix_hint=(
                    "Метод storage был удалён или переименован. "
                    f"Поиск: `git log -S 'def {missing[0]}' --all --oneline storage.py`"
                ),
            )
        else:
            self._add("Storage методы", "ok", f"все {len(required)} методов на месте")

    def check_brain_tools(self):
        """brain.py должен иметь список tools для AI (ALL_TOOLS / TOOLS / любое)."""
        try:
            import brain
        except Exception as e:
            self._add("brain tools", "fail", f"импорт упал: {e!r}",
                      fix_hint="brain.py поломан")
            return
        # Поддержка разных имён: ALL_TOOLS (текущее), TOOLS (старое), CLAUDE_TOOLS
        tools = None
        chosen_name = None
        for var in ("ALL_TOOLS", "TOOLS", "CLAUDE_TOOLS"):
            v = getattr(brain, var, None)
            if isinstance(v, list) and v:
                tools = v
                chosen_name = var
                break
        if not tools:
            self._add(
                "brain.ALL_TOOLS", "fail",
                "список tools не найден (нет ALL_TOOLS/TOOLS/CLAUDE_TOOLS)",
                fix_hint="brain.py должен экспортировать ALL_TOOLS = [...] со схемами tool-ов для Claude.",
            )
            return
        names = [t.get("name", "?") for t in tools if isinstance(t, dict)]
        critical = {"set_payment_method", "create_lk_card", "record_deal"}
        missing = critical - set(names)
        if missing:
            self._add(
                f"brain.{chosen_name}", "fail",
                f"критичные tools отсутствуют: {', '.join(missing)}",
                fix_hint=(
                    "AI не сможет работать без этих tools. "
                    f"Поиск: git log -S 'name: {list(missing)[0]}' --all --oneline brain.py"
                ),
            )
        else:
            preview = ", ".join(names[:5]) + ("..." if len(names) > 5 else "")
            self._add(
                f"brain.{chosen_name}", "ok",
                f"{len(tools)} tools: {preview}",
            )

    def check_env_vars(self):
        """Проверка env-переменных."""
        critical = ["BOT_TOKEN", "API_ID", "API_HASH", "ANTHROPIC_API_KEY"]
        for var in critical:
            val = os.getenv(var, "")
            if not val or val == "0":
                self._add(
                    f"env: {var}", "fail",
                    "не задан",
                    fix_hint=f"Railway → Variables → {var} = ... → Redeploy.",
                )
            else:
                self._add(f"env: {var}", "ok",
                          f"задан ({len(val)} chars, начинается с {val[:5]}…)")

        optional = {
            "OPENAI_API_KEY": "Голосовой режим LEO не будет работать",
            "STRING_SESSION": "Userbot не сможет войти (нужен для Telethon)",
            "DASHBOARD_USER": "Basic auth для дашборда отключен",
            "DASHBOARD_PASS": "Basic auth для дашборда отключен",
        }
        for var, hint in optional.items():
            val = os.getenv(var, "")
            if not val:
                self._add(f"env: {var}", "warn", "не задан", fix_hint=hint)
            else:
                self._add(f"env: {var}", "ok", "задан")

    def check_storage_state(self):
        """state.json должен быть валидным и иметь все секции."""
        try:
            from storage import storage
            storage.reload_sync()
            state = storage.state
        except Exception as e:
            self._add(
                "Storage state", "fail",
                f"не загружается: {e!r}",
                fix_hint=(
                    "Файл storage.state.json (или /app/data/state.json на Railway) "
                    "сломан или недоступен. Проверь права записи."
                ),
            )
            return
        required_keys = ["managed_chats", "workers", "lk_cards"]
        missing = [k for k in required_keys if k not in state]
        if missing:
            self._add(
                "Storage state", "warn",
                f"в state нет ключей: {missing} (создадутся при первом обращении)",
                fix_hint="Это нормально для fresh deploy. Если повторяется — проверь миграции.",
            )
        else:
            mc = len(state.get("managed_chats", {}))
            lk = len(state.get("lk_cards", {}))
            w = len(state.get("workers", []))
            self._add(
                "Storage state", "ok",
                f"managed_chats={mc}, lk_cards={lk}, workers={w}",
            )

    def check_workers_config(self):
        """Должен быть список работников и роли для них."""
        try:
            from storage import storage
            workers = storage.get_workers() or []
            roles = storage.list_worker_roles() or {}
        except Exception as e:
            self._add("Работники", "fail", f"чтение упало: {e!r}",
                      fix_hint="storage.workers сломан")
            return
        if not workers:
            self._add(
                "Работники", "warn",
                "список пустой — берём DEFAULT_WORKERS из config",
                fix_hint="В админке → Работники → добавь команду.",
            )
            return
        no_role = [w for w in workers if w.lstrip("@").lower() not in roles]
        if no_role:
            self._add(
                "Роли работников", "warn",
                f"без роли: {', '.join(no_role)}",
                fix_hint=(
                    "В админке → 🎭 Роли работников → каждому выставь rank "
                    "(Перевяз+проверка / Менеджер чата / Выплаты+Контроль / Руководство)."
                ),
            )
        else:
            self._add(
                "Роли работников", "ok",
                f"{len(workers)} работников, у всех заданы роли",
            )

    def check_tg_admins(self):
        """TG_ADMINS — кто может зайти в дашборд."""
        try:
            from api import TG_ADMINS
        except Exception as e:
            self._add("TG_ADMINS", "fail", f"не загружается: {e!r}",
                      fix_hint="api.py поломан")
            return
        if not TG_ADMINS:
            self._add(
                "TG_ADMINS", "fail",
                "пустой — никто не сможет зайти в дашборд через TG",
                fix_hint="Railway → Variables → TG_ADMINS = id1,id2,id3 → Redeploy.",
            )
        else:
            self._add(
                "TG_ADMINS", "ok",
                f"{len(TG_ADMINS)} админов",
            )

    async def check_tg_bot_reachable(self):
        """getMe чтобы убедиться что бот работает."""
        import config
        if not config.BOT_TOKEN:
            self._add("TG Bot (getMe)", "fail", "BOT_TOKEN не задан",
                      fix_hint="Railway → Variables → BOT_TOKEN")
            return
        try:
            import httpx
            async with httpx.AsyncClient(timeout=8.0) as c:
                r = await c.get(
                    f"https://api.telegram.org/bot{config.BOT_TOKEN}/getMe"
                )
                if r.status_code != 200:
                    raise Exception(f"HTTP {r.status_code}: {r.text[:200]}")
                data = r.json()
                if not data.get("ok"):
                    raise Exception(data.get("description", "no description"))
                me = data.get("result", {})
                self._add(
                    "TG Bot (getMe)", "ok",
                    f"@{me.get('username')} (id={me.get('id')})",
                )
        except Exception as e:
            self._add(
                "TG Bot (getMe)", "fail",
                f"запрос упал: {e}",
                fix_hint=(
                    "Telegram API недоступен или BOT_TOKEN неверный. "
                    "Проверь @BotFather → /mybots → токен."
                ),
            )

    async def check_anthropic_api(self):
        """Pinger Anthropic API чтобы AI работал."""
        import config
        if not config.ANTHROPIC_API_KEY:
            self._add("Anthropic API", "fail", "ANTHROPIC_API_KEY не задан",
                      fix_hint="Railway → Variables → ANTHROPIC_API_KEY")
            return
        try:
            import httpx
            async with httpx.AsyncClient(timeout=10.0) as c:
                r = await c.post(
                    "https://api.anthropic.com/v1/messages",
                    headers={
                        "x-api-key": config.ANTHROPIC_API_KEY,
                        "anthropic-version": "2023-06-01",
                        "content-type": "application/json",
                    },
                    json={
                        "model": "claude-haiku-4-5-20251001",
                        "max_tokens": 8,
                        "messages": [{"role": "user", "content": "ping"}],
                    },
                )
                if r.status_code == 200:
                    self._add("Anthropic API", "ok", "отвечает (claude-haiku)")
                elif r.status_code == 401:
                    self._add(
                        "Anthropic API", "fail",
                        "401 Unauthorized — ANTHROPIC_API_KEY невалидный",
                        fix_hint="Сгенерируй новый ключ на console.anthropic.com",
                    )
                elif r.status_code == 429:
                    self._add(
                        "Anthropic API", "warn",
                        "429 Rate Limit — лимиты исчерпаны",
                        fix_hint="Залей кредитов на console.anthropic.com или жди отката лимита.",
                    )
                else:
                    self._add(
                        "Anthropic API", "warn",
                        f"HTTP {r.status_code}: {r.text[:150]}",
                    )
        except Exception as e:
            self._add(
                "Anthropic API", "warn",
                f"недоступен: {e}",
                fix_hint="Сеть Railway не пускает api.anthropic.com — проверь egress.",
            )

    async def check_openai_realtime(self):
        """Проверка что OpenAI Realtime ключ работает (для голосового LEO)."""
        key = os.getenv("OPENAI_API_KEY", "")
        if not key:
            self._add(
                "OpenAI Realtime", "warn",
                "OPENAI_API_KEY не задан → голосовой LEO не работает",
                fix_hint="Railway → Variables → OPENAI_API_KEY = sk-...",
            )
            return
        try:
            import httpx
            async with httpx.AsyncClient(timeout=8.0) as c:
                r = await c.get(
                    "https://api.openai.com/v1/models",
                    headers={"Authorization": f"Bearer {key}"},
                )
                if r.status_code == 200:
                    self._add("OpenAI Realtime", "ok", "ключ работает")
                elif r.status_code == 401:
                    self._add(
                        "OpenAI Realtime", "fail",
                        "401 — ключ невалидный",
                        fix_hint="platform.openai.com/api-keys → пересоздай ключ",
                    )
                else:
                    self._add("OpenAI Realtime", "warn", f"HTTP {r.status_code}")
        except Exception as e:
            self._add("OpenAI Realtime", "warn", f"недоступен: {e}")

    async def check_github_releases(self):
        """Проверка что desktop-релизы доступны на GitHub."""
        try:
            import httpx
            async with httpx.AsyncClient(timeout=8.0) as c:
                r = await c.get(
                    "https://api.github.com/repos/simba-stack/workchat-bot/releases/latest"
                )
                if r.status_code == 200:
                    data = r.json()
                    ver = data.get("tag_name", "?")
                    assets = len(data.get("assets", []))
                    self._add(
                        "GitHub Releases (desktop)", "ok",
                        f"latest={ver}, assets={assets}",
                    )
                elif r.status_code == 404:
                    self._add(
                        "GitHub Releases (desktop)", "warn",
                        "релизов нет — первый ещё не собран",
                        fix_hint="Запушь тег `v2.0.x` чтобы запустить Actions.",
                    )
                else:
                    self._add(
                        "GitHub Releases (desktop)", "warn",
                        f"HTTP {r.status_code}",
                    )
        except Exception as e:
            self._add("GitHub Releases (desktop)", "warn", f"недоступен: {e}")

    def check_dashboard_html(self):
        """dashboard/jarvis.html должен существовать и иметь основные секции."""
        path = Path(__file__).parent / "dashboard" / "jarvis.html"
        if not path.exists():
            self._add(
                "Dashboard jarvis.html", "fail",
                "файл отсутствует",
                fix_hint="Restore из git: `git checkout HEAD -- dashboard/jarvis.html`",
            )
            return
        try:
            content = path.read_text(encoding="utf-8")
        except Exception as e:
            self._add("Dashboard jarvis.html", "fail",
                      f"не читается: {e!r}")
            return
        required_markers = [
            "currentView === 'payouts'",
            "currentView === 'crm'",
            "currentView === 'discord'",
            "connectSSE",
        ]
        missing = [m for m in required_markers if m not in content]
        if missing:
            self._add(
                "Dashboard jarvis.html", "fail",
                f"в HTML отсутствуют секции: {missing}",
                fix_hint="HTML был повреждён. Restore из git.",
            )
        else:
            kb = len(content) // 1024
            self._add("Dashboard jarvis.html", "ok", f"{kb} KB, все секции на месте")

    # ========== Главный entrypoint ==========

    async def run_all(self) -> list[dict]:
        """Прогоняет ВСЕ проверки и возвращает список результатов."""
        self.results = []
        # Sync checks
        for chk in [
            self.check_python_modules,
            self.check_userbot_methods,
            self.check_storage_methods,
            self.check_brain_tools,
            self.check_env_vars,
            self.check_storage_state,
            self.check_workers_config,
            self.check_tg_admins,
            self.check_dashboard_html,
        ]:
            try:
                chk()
            except Exception as e:
                self._add(chk.__name__, "fail",
                          f"проверка упала: {e!r}",
                          fix_hint="Баг в health_check.py — посмотри логи")
        # Async checks
        for chk in [
            self.check_tg_bot_reachable,
            self.check_anthropic_api,
            self.check_openai_realtime,
            self.check_github_releases,
        ]:
            try:
                await chk()
            except Exception as e:
                self._add(chk.__name__, "fail",
                          f"проверка упала: {e!r}",
                          fix_hint="Баг в health_check.py")
        return self.results

    def format_telegram_message(self, max_len: int = 4000) -> str:
        """Форматирует результаты в TG-сообщение (HTML, ≤4000 chars).
        ВАЖНО: все динамические строки прогоняются через html.escape() —
        TG строго парсит HTML и валит запрос если встречает `<word>` который
        не является валидным тегом (b/i/code/pre/a и т.п.)."""
        from html import escape as _esc

        n_ok = sum(1 for r in self.results if r["status"] == "ok")
        n_warn = sum(1 for r in self.results if r["status"] == "warn")
        n_fail = sum(1 for r in self.results if r["status"] == "fail")

        if n_fail:
            head_emoji = "🔴"
            head_status = f"<b>СБОЙ в {n_fail} системе(ах)</b>"
        elif n_warn:
            head_emoji = "🟡"
            head_status = f"<b>OK с предупреждениями ({n_warn})</b>"
        else:
            head_emoji = "🟢"
            head_status = f"<b>ВСЕ {n_ok} СИСТЕМ В НОРМЕ</b>"

        from datetime import datetime
        ts = datetime.now().strftime("%Y-%m-%d %H:%M")
        lines = [
            f"{head_emoji} <b>HEALTHCHECK</b> · {ts}",
            head_status,
            f"<i>OK: {n_ok} · WARN: {n_warn} · FAIL: {n_fail}</i>",
            "",
        ]
        # Сначала fail, потом warn, потом ok
        for status_filter, emoji in [("fail", "❌"), ("warn", "⚠️"), ("ok", "✅")]:
            block = [r for r in self.results if r["status"] == status_filter]
            if not block:
                continue
            for r in block:
                name = _esc(str(r.get("name", "")))
                message = _esc(str(r.get("message", "")))
                fix_hint = _esc(str(r.get("fix_hint", "")))
                msg = f"{emoji} <b>{name}</b>"
                if message:
                    msg += f" — {message}"
                lines.append(msg)
                if r["status"] in ("fail", "warn") and fix_hint:
                    lines.append(f"   <i>↳ {fix_hint}</i>")
            lines.append("")
        text = "\n".join(lines).strip()
        if len(text) > max_len:
            text = text[: max_len - 30] + "\n\n... (отчёт обрезан)"
        return text


async def run_and_report(send_fn=None) -> dict:
    """Один вызов: прогоняет checks, форматирует, опционально шлёт.

    send_fn — async function(text: str) → отправит итог куда нужно.
    Возвращает: {results: [...], message: str, summary: {ok, warn, fail}}
    """
    h = HealthChecker()
    results = await h.run_all()
    text = h.format_telegram_message()
    summary = {
        "ok": sum(1 for r in results if r["status"] == "ok"),
        "warn": sum(1 for r in results if r["status"] == "warn"),
        "fail": sum(1 for r in results if r["status"] == "fail"),
    }
    if send_fn is not None:
        try:
            await send_fn(text)
        except Exception as e:
            logger.warning("healthcheck send failed: %s", e)
    return {"results": results, "message": text, "summary": summary}
