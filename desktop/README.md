# PRIDE J.A.R.V.I.S. Desktop

Native desktop клиент для PRIDE CRM (Electron-обёртка над веб-дашбордом).

## Что внутри

- 🖥️ **Native окно** без браузерной строки
- 📌 **Tray иконка** с быстрым доступом к вкладкам
- ❌ **Свернуть в трей** при клике на крест (не закрывается)
- 🔔 **Native push** уведомления о событиях (новый клиент / ЛК отработан / блок)
- ⌨️ **Hotkey** Ctrl+Shift+J — toggle окна
- 🔄 **Auto-update** через GitHub Releases с UI-баннером прогресса
- 🍪 **Cookies сохраняются** (Telegram OAuth не нужно повторно)

## 📥 Где скачать готовый .exe

После того как я залью первую сборку на GitHub Releases — ссылка появится тут:

**https://github.com/simba-stack/workchat-bot/releases/latest**

На странице релиза будет файл `PRIDE-JARVIS-Setup-1.0.0.exe` — двойной клик, и приложение установится.

## 🔄 Auto-update

Когда мы пушим новую версию (через git tag → GitHub Release), приложение:
1. **Через 10 секунд после старта** проверяет обновления
2. Если есть новая — показывает **синий баннер сверху**: «⬇️ Загружаю v1.0.1 — 23%»
3. После загрузки баннер: «✅ Готово к установке» + кнопка **Установить**
4. Клик на «Установить» → приложение перезапустится с новой версией
5. Дополнительно проверяет обновления **каждые 30 минут**

Можно вручную проверить через **Tray → Проверить обновления**.

---

## 🛠️ Сборка из исходников (для разработчика)

Требуется **Node.js 20+** + **npm**.

### Установка зависимостей
```cmd
cd C:\Users\sycev\workchat-bot\desktop
npm install
```

### Dev-запуск
```cmd
npm start
```
Откроется окно с дашбордом для тестирования.

### Локальная сборка .exe (Windows)
```cmd
npm run make
```
Готовый installer: `out/make/squirrel.windows/x64/PRIDE-JARVIS-Setup-1.0.0.exe`

### Публикация на GitHub Releases (auto-update)
```cmd
set GITHUB_TOKEN=ghp_твой_токен_с_правами_repo
npm run publish
```
После этого приложение у всех пользователей увидит новую версию через 10 секунд / 30 минут.

**Где взять токен:** https://github.com/settings/tokens → New token (classic) → права `repo` → Generate.

### Обновить версию
1. В `package.json` поднимай `version` (1.0.0 → 1.0.1)
2. `npm run publish` — Electron Forge соберёт .exe и зальёт в GitHub Releases
3. Все клиенты получат обновление в течение часа

---

## 🤖 Автоматическая сборка через GitHub Actions

Если положить файл `.github/workflows/desktop-build.yml` в репо (см. ниже),
то при push'е git tag вида `v1.0.0` GitHub сам соберёт .exe / .dmg / .deb
и положит в Release.

Так SIMBA не нужно ничего собирать локально — просто:
```cmd
cd C:\Users\sycev\workchat-bot\desktop
REM поднять version в package.json
cd ..
git add -A
git commit -m "desktop: v1.0.1"
git tag v1.0.1
git push origin main --tags
```
И через 5 минут на странице Releases появится новый installer.

---

## 🎨 Иконки

Положи в эту папку:
- `icon.ico` (Windows installer)
- `icon.png` (Linux + tray)
- `icon.icns` (macOS .dmg)
- `tray-icon.png` (опционально — отдельная иконка для трея 16x16)

Сделать иконки из PNG:
- https://convertio.co/png-ico/
- https://cloudconvert.com/png-to-icns

---

## ⚙️ Конфигурация

- **URL дашборда:** env-переменная `PRIDE_URL` или прямо в `main.js`
- **Логи:** `%APPDATA%\PRIDE J.A.R.V.I.S.\logs\main.log` (Windows)
