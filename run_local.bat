@echo off
chcp 65001 >nul
cd /d "%~dp0"

REM ============================================================================
REM   ЛОКАЛЬНЫЙ ЗАПУСК workchat-bot на Windows (когда Railway лежит)
REM
REM   КАК ПОЛЬЗОВАТЬСЯ:
REM   1) Открой этот файл в Блокноте (правый клик → Изменить)
REM   2) Вставь свои токены в раздел НАСТРОЙКИ ниже
REM   3) Сохрани (Ctrl+S)
REM   4) Двойной клик на run_local.bat
REM
REM   ВАЖНО: значения берутся из Railway → workchat-bot → Variables
REM   Если Railway лежит — посмотри в своём менеджере паролей / заметках.
REM ============================================================================

REM ╔═══════════════════════ НАСТРОЙКИ — заполни ═══════════════════════╗

REM --- Main aiogram bot (PRIDE INVITE WORK / создание чатов) ---
set "BOT_TOKEN="
set "ADMIN_ID="

REM --- Userbot (Telethon — создаёт work-чаты и постит анкеты) ---
set "API_ID="
set "API_HASH="
set "USERBOT_PHONE="
set "STRING_SESSION="

REM --- AI (без него ассистент молчит, бот /start работает без него) ---
set "ANTHROPIC_API_KEY="

REM --- CRM бот (опционально, можно пусто чтобы только основной поднять) ---
set "CRM_BOT_TOKEN="

REM ╚═══════════════════════════════════════════════════════════════════╝

REM --- Локальные пути (НЕ ТРОГАЙ) ---
set "STORAGE_PATH=%~dp0data\state.json"
set "PORT=8000"

REM --- Проверка Python ---
where python >nul 2>nul
if errorlevel 1 (
    echo.
    echo [ERROR] Python не установлен.
    echo Скачай и установи с https://www.python.org/downloads/
    echo При установке поставь галочку "Add Python to PATH".
    echo.
    pause
    exit /b 1
)

REM --- Проверка обязательных переменных ---
if "%BOT_TOKEN%"=="" (
    echo.
    echo [ERROR] BOT_TOKEN пустой. Открой run_local.bat в Блокноте и вставь токен.
    echo.
    pause
    exit /b 1
)
if "%API_ID%"=="" (
    echo.
    echo [ERROR] API_ID пустой. Без этого userbot не создаст чаты.
    echo.
    pause
    exit /b 1
)
if "%STRING_SESSION%"=="" (
    echo.
    echo [ERROR] STRING_SESSION пустой. Без этого userbot не залогинится.
    echo.
    pause
    exit /b 1
)

REM --- Создаём venv при первом запуске ---
if not exist venv (
    echo.
    echo [setup] Первый запуск — создаю виртуальное окружение Python...
    python -m venv venv
    if errorlevel 1 (
        echo [ERROR] Не удалось создать venv.
        pause
        exit /b 1
    )
    echo [setup] Устанавливаю зависимости (может занять 2-3 минуты)...
    call venv\Scripts\activate
    python -m pip install --upgrade pip
    pip install -r requirements.txt
    if errorlevel 1 (
        echo [ERROR] Не удалось установить зависимости.
        pause
        exit /b 1
    )
) else (
    call venv\Scripts\activate
)

REM --- Создаём папку data для state.json если её нет ---
if not exist data mkdir data

REM --- Запуск ---
echo.
echo =============================================================
echo  workchat-bot стартует ЛОКАЛЬНО на твоём ПК
echo  STORAGE_PATH = %STORAGE_PATH%
echo  Для остановки: закрой окно или Ctrl+C
echo =============================================================
echo.

python bot.py

echo.
echo Бот остановлен. Нажми любую клавишу.
pause >nul
