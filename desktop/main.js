// PRIDE J.A.R.V.I.S. Desktop — Electron main process
// Обёртка над workchat-bot веб-дашбордом.
//
// Фичи:
//   • Native окно без браузерной строки
//   • Tray-иконка (свернуть → в трей, не закрывается)
//   • Hotkeys: Cmd/Ctrl+Q quit, Cmd/Ctrl+R reload, F11 fullscreen, Cmd/Ctrl+Shift+J toggle окна
//   • Native push-уведомления (через SSE → notifications API)
//   • Auto-update через GitHub Releases с UI-баннером прогресса

const {
  app, BrowserWindow, Tray, Menu, shell, ipcMain, Notification,
  globalShortcut, nativeImage, dialog, net, session,
} = require("electron");
const path = require("path");
const log = require("electron-log");

// === Squirrel installer hooks ===
// ВАЖНО: должно сработать ДО любых других app.on / globalShortcut вызовов.
// При --squirrel-install / --squirrel-firstrun / --squirrel-updated / --squirrel-uninstall
// модуль вызывает app.quit() и возвращает true — в этом случае выходим немедленно.
if (require("electron-squirrel-startup")) {
  app.quit();
  process.exit(0);
}

// Single instance lock
const gotTheLock = app.requestSingleInstanceLock();
if (!gotTheLock) {
  app.quit();
  process.exit(0);
}

// === Logging ===
log.transports.file.level = "info";

const DASHBOARD_URL = process.env.PRIDE_URL ||
  "https://workchat-bot-production.up.railway.app/";

let mainWindow = null;
let splashWindow = null;
let tray = null;
let isQuitting = false;
let updateState = { status: "idle", percent: 0, version: null };

function createSplash() {
  splashWindow = new BrowserWindow({
    width: 320,
    height: 320,
    frame: false,
    transparent: true,
    alwaysOnTop: true,
    resizable: false,
    movable: true,
    skipTaskbar: true,
    backgroundColor: "#00000000",
    hasShadow: false,
    show: false,
    webPreferences: {
      contextIsolation: true,
      nodeIntegration: false,
    },
  });
  splashWindow.loadFile(path.join(__dirname, "splash.html"));
  splashWindow.once("ready-to-show", () => {
    if (splashWindow && !splashWindow.isDestroyed()) splashWindow.show();
  });
}

function destroySplash() {
  if (splashWindow && !splashWindow.isDestroyed()) {
    try { splashWindow.close(); } catch (e) { /* silent */ }
  }
  splashWindow = null;
}

function createMainWindow() {
  mainWindow = new BrowserWindow({
    width: 1440,
    height: 900,
    minWidth: 1100,
    minHeight: 700,
    title: "PRIDE J.A.R.V.I.S.",
    icon: path.join(__dirname, "icon.png"),
    backgroundColor: "#0a0e1a",
    autoHideMenuBar: true,
    show: false, // покажем только после did-finish-load (когда дашборд загрузился)
    webPreferences: {
      preload: path.join(__dirname, "preload.js"),
      contextIsolation: true,
      nodeIntegration: false,
      partition: "persist:pride",
    },
  });

  mainWindow.loadURL(DASHBOARD_URL);

  // Когда дашборд полностью загрузился — закрыть splash и показать главное окно
  const revealMain = () => {
    if (mainWindow && !mainWindow.isDestroyed() && !mainWindow.isVisible()) {
      mainWindow.show();
      mainWindow.focus();
    }
    destroySplash();
  };
  mainWindow.webContents.once("did-finish-load", revealMain);
  mainWindow.webContents.once("did-fail-load", revealMain);
  // Аварийный fallback: если за 15 секунд ничего не загрузилось — всё равно показать
  setTimeout(revealMain, 15_000);

  mainWindow.on("close", (event) => {
    if (!isQuitting) {
      event.preventDefault();
      mainWindow.hide();
      if (process.platform === "darwin") app.dock.hide();
      if (!global.hiddenOnce) {
        global.hiddenOnce = true;
        showNotification("PRIDE свёрнут в трей", "Кликни иконку в трее чтобы открыть.");
      }
    }
  });

  mainWindow.webContents.setWindowOpenHandler(({ url }) => {
    log.info("window.open requested:", url);
    // Сам дашборд — открываем как новое окно внутри Electron
    if (url.startsWith(DASHBOARD_URL)) return { action: "allow" };
    // Telegram OAuth widget: открываем как popup ВНУТРИ приложения.
    // ВАЖНО: НЕ ставим modal:true — это иногда ломает window.opener в Electron.
    const tgHosts = [
      "https://oauth.telegram.org",
      "https://my.telegram.org",
      "https://telegram.org",
      "https://web.telegram.org",
      "https://t.me",
    ];
    if (tgHosts.some(h => url.startsWith(h))) {
      return {
        action: "allow",
        overrideBrowserWindowOptions: {
          width: 520,
          height: 720,
          parent: mainWindow,
          autoHideMenuBar: true,
          backgroundColor: "#ffffff",
          title: "Авторизация Telegram",
          webPreferences: {
            partition: "persist:pride",      // те же куки что у main!
            contextIsolation: true,
            nodeIntegration: false,
          },
        },
      };
    }
    // Всё остальное — наружу в системный браузер
    shell.openExternal(url);
    return { action: "deny" };
  });

  // Лог навигации child-окон (для отладки) + fallback на reload main
  mainWindow.webContents.on("did-create-window", (childWindow, details) => {
    log.info("child window created:", details.url);
    childWindow.webContents.on("did-navigate", (_e, navUrl) => {
      log.info("popup navigated to:", navUrl);
    });
    childWindow.on("closed", () => {
      log.info("popup closed — checking if main needs reload");
      // Если popup закрылся (юзер либо подтвердил, либо отменил), а main всё ещё на /login —
      // перезагружаем main: при успешной авторизации cookie уже стоит, /login сам сделает 302→/
      if (mainWindow && !mainWindow.isDestroyed()) {
        const url = mainWindow.webContents.getURL();
        if (url.includes("/login") || url === DASHBOARD_URL || url === DASHBOARD_URL.replace(/\/$/, "")) {
          setTimeout(() => {
            if (mainWindow && !mainWindow.isDestroyed()) {
              mainWindow.loadURL(DASHBOARD_URL);
            }
          }, 400);
        }
      }
    });
  });

  // Когда renderer готов — отправим текущее update состояние
  mainWindow.webContents.on("did-finish-load", () => {
    sendUpdateState();
  });
}

function createTray() {
  let icon;
  try {
    const trayPath = path.join(__dirname, "tray-icon.png");
    icon = nativeImage.createFromPath(trayPath);
    if (icon.isEmpty()) {
      icon = nativeImage.createFromPath(path.join(__dirname, "icon.png"));
    }
  } catch (e) {
    log.error("tray icon load fail:", e);
    return;
  }
  if (process.platform === "darwin") {
    icon = icon.resize({ width: 16, height: 16 });
  }
  tray = new Tray(icon);
  tray.setToolTip(`PRIDE J.A.R.V.I.S. v${app.getVersion()}`);

  const menu = Menu.buildFromTemplate([
    {
      label: "Открыть J.A.R.V.I.S.",
      click: () => showMainWindow(),
    },
    { type: "separator" },
    { label: "🤝 Партнёры", click: () => navigateTo("#crm") },
    { label: "💰 Выплаты", click: () => navigateTo("#payouts") },
    { label: "📋 ЛК Отдел", click: () => navigateTo("#lk") },
    { type: "separator" },
    {
      label: `Проверить обновления (текущая ${app.getVersion()})`,
      click: () => {
        checkForUpdates(/*manual=*/ true).catch((e) => {
          log.error("manual update check failed:", e);
          dialog.showErrorBox("Ошибка обновления", String(e));
        });
      },
    },
    { label: "↻ Перезагрузить", click: () => mainWindow && mainWindow.reload() },
    {
      label: "Открыть в браузере",
      click: () => shell.openExternal(DASHBOARD_URL),
    },
    { type: "separator" },
    {
      label: "Выйти",
      click: () => { isQuitting = true; app.quit(); },
    },
  ]);
  tray.setContextMenu(menu);

  tray.on("click", () => {
    if (!mainWindow) return;
    if (mainWindow.isVisible()) mainWindow.hide();
    else showMainWindow();
  });
}

function showMainWindow() {
  if (!mainWindow) return;
  mainWindow.show();
  mainWindow.focus();
  if (process.platform === "darwin") app.dock.show();
}

function navigateTo(hash) {
  if (!mainWindow) return;
  showMainWindow();
  mainWindow.webContents.executeJavaScript(`location.hash = '${hash}'`).catch(() => {});
}

function showNotification(title, body, onClick) {
  if (!Notification.isSupported()) return;
  const n = new Notification({
    title, body,
    icon: path.join(__dirname, "icon.png"),
    silent: false,
  });
  if (onClick) n.on("click", onClick);
  n.show();
}

// === IPC: renderer pushes native notifications ===
ipcMain.on("pride-notify", (_e, payload) => {
  const { title, body, hash } = payload || {};
  if (!title) return;
  showNotification(title, body || "", () => {
    if (mainWindow) {
      showMainWindow();
      if (hash) {
        mainWindow.webContents.executeJavaScript(`location.hash = '${hash}'`).catch(() => {});
      }
    }
  });
});

// === IPC: renderer asks current update state (banner) ===
ipcMain.handle("pride-get-update-state", () => updateState);

// === IPC: renderer clicked "Скачать" в баннере — открыть .exe в браузере ===
ipcMain.on("pride-install-update", () => {
  log.info("user clicked download — opening installer in browser");
  if (updateState && updateState.downloadUrl) {
    shell.openExternal(updateState.downloadUrl);
  } else {
    shell.openExternal(DASHBOARD_URL + "desktop");
  }
});

// === IPC: renderer clicked "Check for updates" manually ===
ipcMain.on("pride-check-updates", () => {
  checkForUpdates(/*manual=*/ true).catch((e) => log.error("manual check:", e));
});

// === Простой кастомный updater через наш /api/desktop/manifest ===
function sendUpdateState() {
  if (mainWindow && !mainWindow.isDestroyed()) {
    mainWindow.webContents.send("pride-update-state", updateState);
  }
}

function compareVersions(a, b) {
  // "1.0.10" > "1.0.9" — числовое сравнение
  const pa = String(a).replace(/^v/, "").split(".").map(n => parseInt(n) || 0);
  const pb = String(b).replace(/^v/, "").split(".").map(n => parseInt(n) || 0);
  for (let i = 0; i < Math.max(pa.length, pb.length); i++) {
    const x = pa[i] || 0, y = pb[i] || 0;
    if (x > y) return 1;
    if (x < y) return -1;
  }
  return 0;
}

async function fetchManifest() {
  return new Promise((resolve, reject) => {
    const url = DASHBOARD_URL.replace(/\/$/, "") + "/api/desktop/manifest?refresh=1";
    const req = net.request({ url, method: "GET" });
    let body = "";
    req.on("response", (res) => {
      res.on("data", (chunk) => { body += chunk.toString(); });
      res.on("end", () => {
        try { resolve(JSON.parse(body)); }
        catch (e) { reject(new Error("invalid JSON: " + body.substring(0, 100))); }
      });
      res.on("error", reject);
    });
    req.on("error", reject);
    req.end();
  });
}

async function checkForUpdates(manual = false) {
  log.info("checking for updates (manual=" + manual + ")...");
  updateState = { status: "checking", percent: 0, version: null };
  sendUpdateState();
  try {
    const data = await fetchManifest();
    if (!data || !data.ok) throw new Error(data && data.error ? data.error : "no manifest");
    const remote = String(data.version || "").replace(/^v/, "");
    const local = app.getVersion();
    log.info(`version check: local=${local} remote=${remote}`);
    if (compareVersions(remote, local) > 0) {
      const winAsset = (data.assets || []).find(a => a.platform === "win");
      const downloadUrl = winAsset
        ? DASHBOARD_URL.replace(/\/$/, "") + winAsset.url
        : DASHBOARD_URL.replace(/\/$/, "") + "/desktop";
      updateState = { status: "ready", percent: 100, version: remote, downloadUrl };
      sendUpdateState();
      showNotification(
        `🎉 Доступна версия ${remote}`,
        "Нажми «Скачать» в баннере вверху приложения чтобы обновиться.",
        () => showMainWindow(),
      );
    } else {
      updateState = { status: "uptodate", percent: 0, version: local };
      sendUpdateState();
      if (manual) {
        showNotification(
          "✅ У тебя свежая версия",
          `Установлена ${local} — обновлений нет.`,
        );
      }
    }
  } catch (e) {
    log.error("update check error:", e);
    updateState = { status: "error", percent: 0, version: null, error: String(e) };
    sendUpdateState();
  }
}

// === Lifecycle ===
app.whenReady().then(() => {
  // КРИТИЧНО: слушаем изменение cookie jarvis_session.
  // Когда после TG-OAuth сервер выставляет cookie — мы сразу перезагружаем main
  // (не полагаемся на window.opener.location.href = '/' из popup, который может не сработать).
  try {
    const sess = session.fromPartition("persist:pride");
    sess.cookies.on("changed", (_event, cookie, cause, removed) => {
      if (removed) return;
      if (cookie && cookie.name === "jarvis_session") {
        log.info("AUTH cookie set, reloading main window", { cause, domain: cookie.domain });
        if (mainWindow && !mainWindow.isDestroyed()) {
          // Грузим / напрямую, потому что страница /login сама редиректит при наличии cookie
          mainWindow.loadURL(DASHBOARD_URL);
        }
        // Закрываем все child-окна (popup)
        for (const w of BrowserWindow.getAllWindows()) {
          if (w !== mainWindow && w !== splashWindow && !w.isDestroyed()) {
            try { w.close(); } catch (e) { /* silent */ }
          }
        }
      }
    });
  } catch (e) {
    log.error("cookie listener setup failed:", e);
  }

  createSplash();
  createMainWindow();
  createTray();

  globalShortcut.register("CommandOrControl+Shift+J", () => {
    if (!mainWindow) return;
    if (mainWindow.isVisible()) mainWindow.hide();
    else showMainWindow();
  });

  // Проверка обновлений через 10 сек после старта + каждые 30 минут
  setTimeout(() => {
    checkForUpdates(false).catch((e) => log.error("startup check:", e));
  }, 10_000);
  setInterval(() => {
    checkForUpdates(false).catch((e) => log.error("interval check:", e));
  }, 30 * 60 * 1000);
});

app.on("second-instance", () => {
  if (mainWindow) {
    if (mainWindow.isMinimized()) mainWindow.restore();
    showMainWindow();
  }
});

app.on("window-all-closed", () => {
  if (process.platform !== "darwin" && isQuitting) app.quit();
});

app.on("activate", () => {
  if (BrowserWindow.getAllWindows().length === 0) createMainWindow();
  else if (mainWindow) showMainWindow();
});

app.on("before-quit", () => {
  isQuitting = true;
  // globalShortcut можно дёргать только если app был ready.
  // app.isReady() добавлен в Electron 5+.
  if (app.isReady()) {
    try { globalShortcut.unregisterAll(); } catch (e) { log.error("unregisterAll:", e); }
  }
});

// will-quit отрабатывает позже before-quit и тоже может вызваться до ready
app.on("will-quit", () => {
  if (app.isReady()) {
    try { globalShortcut.unregisterAll(); } catch (e) { /* silent */ }
  }
});
