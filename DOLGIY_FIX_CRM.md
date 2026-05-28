# ДОЛГИЙ ФИКС ЦРМ (27-28 мая 2026)

**Назначение документа:** зафиксировать длительный бой за credit-flow в `handle_fio`. ТРИ корневые причины + урок про PowerShell, методика диагностики, ссылки на коммиты. Читать ДО любых правок `storage.py`, `fsm_persistent.py` или асинхронного кода с `_lock`.

---

## Симптом

В credit-чате юрист вводит `/clients` → нажимает «Новая анкета» → бот просит ФИО → юрист пишет ФИО → **бот молчит, анкета не появляется**. Параллельно: Railway-контейнер падал каждые 5-10 минут (healthcheck timeout), invite-бот переставал отвечать.

## Три корневые причины

Все три про **блокировку event loop**, но в разных местах.

### Причина 1: PersistentFSMStorage v1 — sync `json.dump` в event loop

Первая попытка персистентного FSM-хранилища делала синхронный `json.dump(state, f)` на каждый `set_state()`. С ростом файла `crm_fsm.json` (МБ) write занимал сотни миллисекунд. Railway healthcheck (127.0.0.1:8081) не отвечал в эти моменты → SIGTERM → контейнер перезапускался **каждые 7 минут**.

**Решение:** полностью переписан в `fsm_persistent.py` как `AsyncPersistentFSMStorage`:
- `set_state` / `set_data` — мгновенные in-memory мутации, помечают `_dirty=True`
- Фоновая задача (`_flush_loop`) каждые `flush_interval=2.0` сек проверяет `_dirty` и если да — выполняет `_flush()`
- `_flush()` делает `json.dumps()` в event loop (быстро), а write-to-disk → `loop.run_in_executor(None, self._do_write_sync, snapshot)`
- `close()` для graceful shutdown — финальный flush + cancel фоновой задачи
- Atomic write через `.tmp` + `os.replace`

8 smoke-тестов в `outputs/test_fsm_persistent.py` (mock-инжектят aiogram через `sys.modules`):
1. basic set/get
2. persistence reload (close → reopen)
3. State-объект с `.state` атрибутом
4. `set_state(None)` очищает
5. Повреждённый JSON → стартуем пустым
6. Debounce: 20 set_state → 1 disk write
7. Concurrent 5 workers × 10 ops без race
8. 100 set_state операций < 100ms (no event-loop block)

### Причина 2: `storage._save_unlocked()` — тот же sync `json.dump`

После фикса FSM контейнер перестал умирать **по расписанию**, но всё ещё умирал когда юрист вводил ФИО. Debug-логи показали что последний лог = `[add_credit_drop] saving state.json (size_estimate=N entries)`. Значит блокировка в `storage._save_unlocked()` — там был синхронный `json.dump(self.state, f)` на МБ-state.

**Решение:** разделить serialize и write:

```python
async def _save_unlocked(self):
    # Serialize в event loop — быстро (<50ms даже для МБ)
    snapshot = json.dumps(self.state, ensure_ascii=False, indent=2)
    # File I/O в thread executor — НЕ блокирует event loop
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, self._do_write_sync, snapshot)

def _do_write_sync(self, snapshot_str: str):
    """Sync atomic write в thread executor. .tmp → os.replace + .bak."""
    tmp = self.path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(snapshot_str)
    if os.path.exists(self.path):
        try: os.replace(self.path, self.path + ".bak")
        except Exception: pass
    os.replace(tmp, self.path)
```

Это безопасно потому что `_save_unlocked` всегда вызывается под `async with _lock` (по контракту имени). Пока мы под локом, никакой другой корутин не может мутировать `self.state`.

Коммит: **`b3d1c68`** `perf(storage+fsm): async I/O through executor`

### Причина 3: Дедлок — `bump_*_manager_stat` под `_lock`

Контейнер перестал падать, `state saved` теперь логировался за 103 ms (отлично). Но всё равно карточка не создавалась. Последний лог стал `[add_credit_drop] state saved, bumping stat` → тишина.

Корень: `add_credit_drop` сидит под `async with _lock`, потом зовёт `await self.bump_credit_manager_stat(...)`. А `bump_credit_manager_stat` САМ пытается взять `async with _lock`. **asyncio.Lock не реентерабельный** → ждёт вечно тот лок которым уже владеет.

Проверка кода нашла 8 таких мест (credit + outsource bump_*_manager_stat под локом).

**Решение:** split каждой публичной bump-функции на:
- `_bump_credit_manager_stat_unlocked(username, key, delta)` — чистая логика без захвата лока. Для вызова ИЗ-ПОД уже захваченного `_lock`. **Не делает save** — caller отвечает.
- `bump_credit_manager_stat(...)` — публичная обёртка: берёт `_lock`, зовёт `_unlocked`, сохраняет state. Для внешних вызовов.

Аналогично для outsource. Все 8 внутренних call-sites переключены на `_unlocked`. Порядок внутри caller-функций: **bump → save** (иначе stat не персистится, потому что `_unlocked` сам не сохраняет).

Сейчас в коде:
- 0 дедлок-точек
- 0 BAD ORDER (save до bump)
- 10 безопасных `_unlocked` вызовов под `_lock`

---

## Методика диагностики (что сработало)

1. **Debug-логи на каждый `await`** в подозрительной функции. Минимум: вход в функцию, до/после lock acquire, до/после I/O, перед return. С `logger.info(...)` не `print`, чтобы Railway увидел.
2. **Смотреть Railway "Deploy Logs"** в фильтре по ключевому слову. Например `add_credit_drop` → видны все шаги. **Последний лог = точка зависания.**
3. **Не верить "вот сейчас точно починили"** — между gut и реальностью большая разница. Поэтому пушить → ждать ACTIVE → тестировать → читать логи → итерировать.
4. **Запасной debug-лог `_show_drop`** обёрнут в `try/except` чтобы не терять exception silently.

---

## PowerShell траблы (как НЕ писать большие файлы)

Параллельно с основным фиксом, файлы в репо несколько раз ломались — на Windows side PowerShell `Set-Content`, `WriteAllText` и подобные обрезали `api.py` / `crm_bot.py` / `fsm_persistent.py` посреди UTF-8 emoji или подкладывали null-байты (6758 шт. в api.py однажды). Восстанавливали через `git show HEAD:file > /tmp/...` → копирование обратно.

**ПРАВИЛО для файлов > 200 KB или с UTF-8 emoji:**

1. ❌ НЕ использовать `Set-Content`, `Out-File`, `WriteAllText` напрямую с большими блоками текста, особенно с emoji.
2. ✅ Использовать **Python-патчер** в `/tmp/patcher.py`:
   - Прочитать файл
   - Точечно `str.replace(old_block, new_block, 1)` с проверкой `if old not in content: sys.exit(1)`
   - `ast.parse(new_content)` — syntax check
   - Сравнить размер: если файл сократился >100 байт без причины → abort
   - Atomic write: `.tmp` → `os.replace`
   - Verify: прочитать обратно с диска, `ast.parse` ещё раз
3. ✅ Использовать `Edit` tool — он точечно меняет блок, не переписывает весь файл.
4. ✅ ВСЕГДА ДО `git commit`:
   ```bash
   python3 -c "import ast; ast.parse(open('FILE.py').read()); print('AST OK')"
   wc -l FILE.py    # сравнить с предыдущим размером
   ```

---

## Финальные коммиты в проде

- `b3d1c68` — `perf(storage+fsm): async I/O through executor — fix event loop blocking that killed Railway healthcheck during add_credit_drop`
- следующий — fix deadlock в bump_*_manager_stat

## Файлы которые трогали

- `storage.py` — async `_save_unlocked` + `_do_write_sync` + split `bump_*_manager_stat` на `_unlocked`/публичную
- `fsm_persistent.py` — `AsyncPersistentFSMStorage` с debounced flush
- `crm_bot.py` — debug-логи в `handle_fio` + try/except вокруг `_show_drop`

## Не трогать без чтения этого документа

- ❌ Любое `await self._save_unlocked()` внутри `async with _lock` блока — must be _unlocked-only path
- ❌ Добавление нового `bump_*` или `add_*` метода без `_unlocked` варианта если он будет вызываться из других методов под `_lock`
- ❌ Sync `json.dump` / `json.load` на `self.state` внутри event loop без `run_in_executor`
- ❌ Любой PowerShell `Set-Content` / `WriteAllText` на файлы из репо без Python-патчера

---

*Документ ведёт Claude. Обновлять при следующем серьёзном инциденте в storage/FSM/async-зоне.*
