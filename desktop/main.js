// PRIDE J.A.R.V.I.S. Desktop — Electron main process (v2.x)
// Тулчейн: electron-builder + electron-updater (NSIS installer).
//
// Фичи:
//   • Native окно без браузерной строки
//   • Tray-иконка (свернуть → в трей, не закрывается)
//   • Hotkey Cmd/Ctrl+Shift+J — toggle окна
//   • Splash-экран на старте (чёрный пульсирующий квадрат)
//   • Native push-уведомления (через SSE → notifications API)
//   • TG OAuth popup открывается ВНУТРИ приложения (cookies-shared partition)
//   • Cookie listener: автоматический reload main после успешной авторизации
//   • SILENT auto-update через electron-updater + electron-builder
//     - Проверка обновлений каждые 30 минут
//     - Скачивание в фоне (без UI)
//     - Установка молча при следующем рестарте (как Chrome)

const {
  app, BrowserWindow, Tray, Menu, shell, ipcMain, Notification,
  globalShortcut, nativeImage, dialog, session, desktopCapturer,
} = require("electron");
const path = require("path");
const log = require("electron-log");
const { autoUpdater } = require("electron-updater");

// Single instance lock
const gotTheLock = app.requestSingleInstanceLock();
if (!gotTheLock) {
  app.quit();
  process.exit(0);
}

// === Logging ===
log.transports.file.level = "info";
log.transports.console.level = "info";
autoUpdater.logger = log;

// === Auto-updater config (silent install) ===
autoUpdater.autoDownload = true;             // качаем сразу в фоне
autoUpdater.autoInstallOnAppQuit = true;     // ставим при выходе
autoUpdater.allowDowngrade = false;
autoUpdater.allowPrerelease = false;

const DASHBOARD_URL = process.env.PRIDE_URL ||
  "https://workchat-bot-production.up.railway.app/";

let mainWindow = null;
let splashWindow = null;
let tray = null;
let isQuitting = false;
let updateState = { status: "idle", percent: 0, version: null };

// === Splash window (показывается на старте) ===
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

// === Main window ===
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
    show: false,
    webPreferences: {
      preload: path.join(__dirname, "preload.js"),
      contextIsolation: true,
      nodeIntegration: false,
      partition: "persist:pride",
    },
  });

  mainWindow.loadURL(DASHBOARD_URL);

  const revealMain = () => {
    if (mainWindow && !mainWindow.isDestroyed() && !mainWindow.isVisible()) {
      mainWindow.show();
      mainWindow.focus();
    }
    destroySplash();
  };
  mainWindow.webContents.once("did-finish-load", revealMain);
  mainWindow.webContents.once("did-fail-load", revealMain);
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

  // === TG OAuth popup внутри Electron (общие cookies persist:pride) ===
  mainWindow.webContents.setWindowOpenHandler(({ url }) => {
    log.info("window.open requested:", url);
    if (url.startsWith(DASHBOARD_URL)) return { action: "allow" };
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
            partition: "persist:pride",
            contextIsolation: true,
            nodeIntegration: false,
          },
        },
      };
    }
    shell.openExternal(url);
    return { action: "deny" };
  });

  mainWindow.webContents.on("did-create-window", (childWindow, details) => {
    log.info("child window created:", details.url);
    childWindow.webContents.on("did-navigate", (_e, navUrl) => {
      log.info("popup navigated to:", navUrl);
    });
    childWindow.on("closed", () => {
      log.info("popup closed — checking if main needs reload");
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

  mainWindow.webContents.on("did-finish-load", () => {
    sendUpdateState();
  });
}

// === Tray ===
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
    { label: "Открыть J.A.R.V.I.S.", click: () => showMainWindow() },
    { type: "separator" },
    { label: "🤝 Партнёры", click: () => navigateTo("#crm") },
    { label: "💰 Выплаты", click: () => navigateTo("#payouts") },
    { label: "📋 ЛК Отдел", click: () => navigateTo("#lk") },
    { type: "separator" },
    {
      label: `Проверить обновления (текущая ${app.getVersion()})`,
      click: () => {
        autoUpdater.checkForUpdates().catch((e) => {
          log.error("manual update check failed:", e);
          dialog.showErrorBox("Ошибка обновления", String(e));
        });
      },
    },
    { label: "↻ Перезагрузить", click: () => mainWindow && mainWindow.reload() },
    { label: "Открыть в браузере", click: () => shell.openExternal(DASHBOARD_URL) },
    { type: "separator" },
    { label: "Выйти", click: () => { isQuitting = true; app.quit(); } },
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

// === IPC ===
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

ipcMain.handle("pride-get-update-state", () => updateState);

ipcMain.on("pride-install-update", () => {
  // Юзер кликнул «Установить сейчас» в баннере — заставляем установить немедленно.
  log.info("user requested immediate update install");
  if (updateState && updateState.status === "ready") {
    isQuitting = true;
    autoUpdater.quitAndInstall(true /* silent */, true /* forceRunAfter */);
  }
});

ipcMain.on("pride-check-updates", () => {
  autoUpdater.checkForUpdates().catch((e) => log.error("manual check:", e));
});

// === Auto-update events (electron-updater) ===
function sendUpdateState() {
  if (mainWindow && !mainWindow.isDestroyed()) {
    mainWindow.webContents.send("pride-update-state", updateState);
  }
}

autoUpdater.on("checking-for-update", () => {
  log.info("[updater] checking for update");
});

autoUpdater.on("update-available", (info) => {
  log.info("[updater] update available:", info.version);
  updateState = {
    status: "downloading",
    percent: 0,
    version: info.version,
  };
  sendUpdateState();
});

autoUpdater.on("update-not-available", () => {
  log.info("[updater] no update");
  updateState = { status: "uptodate", percent: 0, version: app.getVersion() };
  sendUpdateState();
});

autoUpdater.on("download-progress", (progress) => {
  updateState = {
    status: "downloading",
    percent: Math.round(progress.percent || 0),
    version: updateState.version,
    bytesPerSecond: progress.bytesPerSecond,
    transferred: progress.transferred,
    total: progress.total,
  };
  sendUpdateState();
});

autoUpdater.on("update-downloaded", (info) => {
  log.info("[updater] downloaded:", info.version);
  updateState = { status: "ready", percent: 100, version: info.version };
  sendUpdateState();
  // Тихая native-нотификация. НЕ форсим popup — пусть юзер сам перезапустит когда удобно.
  // При выходе приложения NSIS-апдейтер сам молча применит обновление (autoInstallOnAppQuit).
  showNotification(
    `Обновление до v${info.version} готово`,
    "Будет установлено автоматически при следующем запуске.",
    () => showMainWindow(),
  );
});

autoUpdater.on("error", (err) => {
  log.error("[updater] error:", err);
  updateState = { status: "error", percent: 0, version: null, error: String(err) };
  sendUpdateState();
});

// === Lifecycle ===
app.whenReady().then(() => {
  // === Screen share (getDisplayMedia) handler ===
  // Electron блокирует getDisplayMedia по умолчанию. Регистрируем хэндлер
  // который через desktopCapturer выдаёт первый экран. На Windows 11 / macOS
  // useSystemPicker=true вызовет нативный OS-пикер.
  try {
    const sess = session.fromPartition("persist:pride");
    sess.setDisplayMediaRequestHandler(async (request, callback) => {
      try {
        log.info("[screen-share] requested by:", request.frame?.url);
        const sources = await desktopCapturer.getSources({
          types: ["screen", "window"],
          thumbnailSize: { width: 0, height: 0 },
          fetchWindowIcons: false,
        });
        if (!sources || sources.length === 0) {
          log.warn("[screen-share] no sources");
          callback({}); // отказ
          return;
        }
        // Берём первый источник (обычно — основной экран). Для multi-screen UI
        // можно показать кастомный пикер, но для начала достаточно автоматики.
        callback({ video: sources[0], audio: "loopback" });
        log.info("[screen-share] granted source:", sources[0].name);
      } catch (e) {
        log.error("[screen-share] handler error:", e);
        try { callback({}); } catch (_) {}
      }
    }, { useSystemPicker: true });
    log.info("setDisplayMediaRequestHandler registered");
  } catch (e) {
    log.error("setDisplayMediaRequestHandler setup failed:", e);
  }

  // Cookie listener: jarvis_session появилась → авто-reload main (для TG OAuth)
  try {
    const sess = session.fromPartition("persist:pride");
    sess.cookies.on("changed", (_event, cookie, cause, removed) => {
      if (removed) return;
      if (cookie && cookie.name === "jarvis_session") {
        log.info("AUTH cookie set, reloading main window");
        if (mainWindow && !mainWindow.isDestroyed()) {
          mainWindow.loadURL(DASHBOARD_URL);
        }
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

  // Первая проверка обновлений через 10 секунд, потом каждые 30 минут.
  // Скачивание идёт в фоне (autoDownload=true), установка при следующем
  // выходе из приложения (autoInstallOnAppQuit=true) — silent.
  setTimeout(() => {
    autoUpdater.checkForUpdatesAndNotify()
      .catch((e) => log.error("startup update check:", e));
  }, 10_000);
  setInterval(() => {
    autoUpdater.checkForUpdates()
      .catch((e) => log.error("interval update check:", e));
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
  if (app.isReady()) {
    try { globalShortcut.unregisterAll(); } catch (e) { log.error("unregisterAll:", e); }
  }
});

app.on("will-quit", () => {
  if (app.isReady()) {
    try { globalShortcut.unregisterAll(); } catch (e) { /* silent */ }
  }
});
