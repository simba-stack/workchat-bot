// Preload — мост между renderer (веб-страница) и main процессом.
// Тут же инжектируется UI-баннер обновлений поверх дашборда.

const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("pride", {
  notify: (title, body, hash) => ipcRenderer.send("pride-notify", { title, body, hash }),
  installUpdate: () => ipcRenderer.send("pride-install-update"),
  checkUpdates: () => ipcRenderer.send("pride-check-updates"),
  getUpdateState: () => ipcRenderer.invoke("pride-get-update-state"),
  version: () => process.versions.electron,
  platform: () => process.platform,
});

// === UI: Update banner ===
// Слушаем события об обновлении и инжектируем фиксированный баннер вверху страницы

function injectUpdateBanner() {
  if (document.getElementById("pride-update-banner")) return;
  const style = document.createElement("style");
  style.textContent = `
    #pride-update-banner {
      position: fixed; top: 0; left: 0; right: 0;
      z-index: 999999;
      background: linear-gradient(90deg, #1a3a5c 0%, #2a5a8c 50%, #1a3a5c 100%);
      color: #fff;
      padding: 8px 16px;
      font-family: system-ui, -apple-system, sans-serif;
      font-size: 13px;
      display: flex; align-items: center; gap: 12px;
      box-shadow: 0 2px 12px rgba(0,229,255,0.3);
      border-bottom: 1px solid rgba(0,229,255,0.4);
      transform: translateY(-100%);
      transition: transform 0.3s ease;
    }
    #pride-update-banner.visible {
      transform: translateY(0);
    }
    #pride-update-banner .pub-icon { font-size: 16px; }
    #pride-update-banner .pub-text { flex: 1; }
    #pride-update-banner .pub-progress {
      width: 200px; height: 6px;
      background: rgba(255,255,255,0.15);
      border-radius: 3px; overflow: hidden;
    }
    #pride-update-banner .pub-bar {
      height: 100%; background: #00e5ff;
      width: 0%; transition: width 0.3s ease;
      box-shadow: 0 0 8px #00e5ff;
    }
    #pride-update-banner .pub-btn {
      padding: 4px 14px;
      background: #00e5ff; color: #001824;
      border: none; border-radius: 4px;
      font-weight: 700; cursor: pointer;
      font-size: 12px;
      transition: filter 0.15s;
    }
    #pride-update-banner .pub-btn:hover { filter: brightness(1.1); }
    #pride-update-banner .pub-close {
      cursor: pointer; opacity: 0.6; padding: 0 4px;
    }
    #pride-update-banner .pub-close:hover { opacity: 1; }
  `;
  document.head.appendChild(style);

  const banner = document.createElement("div");
  banner.id = "pride-update-banner";
  banner.innerHTML = `
    <span class="pub-icon">⬇️</span>
    <span class="pub-text" id="pride-update-text">Проверяю обновления...</span>
    <div class="pub-progress" id="pride-update-progress" style="display:none;">
      <div class="pub-bar" id="pride-update-bar"></div>
    </div>
    <button class="pub-btn" id="pride-update-btn" style="display:none;">Установить</button>
    <span class="pub-close" id="pride-update-close" title="Скрыть">✕</span>
  `;
  document.body.appendChild(banner);

  document.getElementById("pride-update-close").onclick = () => {
    banner.classList.remove("visible");
  };
  document.getElementById("pride-update-btn").onclick = () => {
    if (window.pride && window.pride.installUpdate) {
      window.pride.installUpdate();
    }
  };
}

function renderUpdateState(state) {
  const banner = document.getElementById("pride-update-banner");
  if (!banner) return;
  const text = document.getElementById("pride-update-text");
  const progressBox = document.getElementById("pride-update-progress");
  const bar = document.getElementById("pride-update-bar");
  const btn = document.getElementById("pride-update-btn");

  if (!state || state.status === "idle" || state.status === "uptodate") {
    banner.classList.remove("visible");
    return;
  }
  if (state.status === "checking") {
    banner.classList.add("visible");
    text.textContent = "🔍 Проверяю обновления...";
    progressBox.style.display = "none";
    btn.style.display = "none";
    return;
  }
  if (state.status === "downloading") {
    banner.classList.add("visible");
    const mb = state.total ? ` (${Math.round((state.transferred || 0) / 1e6)} / ${Math.round(state.total / 1e6)} MB)` : "";
    text.textContent = `⬇️ Загружаю v${state.version || "?"} — ${state.percent || 0}%${mb}`;
    progressBox.style.display = "block";
    bar.style.width = (state.percent || 0) + "%";
    btn.style.display = "none";
    return;
  }
  if (state.status === "ready") {
    banner.classList.add("visible");
    text.textContent = `✅ Обновление v${state.version || ""} загружено — готово к установке`;
    progressBox.style.display = "none";
    btn.style.display = "inline-block";
    return;
  }
  if (state.status === "error") {
    banner.classList.add("visible");
    text.textContent = `⚠️ Ошибка обновления: ${state.error || "unknown"}`;
    progressBox.style.display = "none";
    btn.style.display = "none";
    setTimeout(() => banner.classList.remove("visible"), 8000);
    return;
  }
}

// === SSE → native notifications ===
window.addEventListener("DOMContentLoaded", () => {
  console.log("[PRIDE Desktop] preload ready, platform:", process.platform);
  injectUpdateBanner();

  // Запросим текущее состояние сразу при загрузке
  ipcRenderer.invoke("pride-get-update-state").then(renderUpdateState).catch(() => {});
  // Подписка на изменения
  ipcRenderer.on("pride-update-state", (_e, state) => renderUpdateState(state));

  // SSE для native push'ей
  try {
    const es = new EventSource("/api/events/stream");
    es.onmessage = (ev) => {
      try {
        const event = JSON.parse(ev.data);
        const type = event.type || "";
        const p = event.payload || {};
        let title = null, body = "", hash = "";

        if (type === "crm.drop.accepted") {
          title = "✅ Принят новый клиент";
          body = `${p.fio || ""} — карточки в работу`;
          hash = "#crm";
        } else if (type === "crm.drop.done") {
          title = "🏁 ЛК полностью готов";
          body = `Клиент ${p.fio || ""} — все ЛК отработаны`;
          hash = "#crm";
        } else if (type === "lk-status-changed" && p.new_status === "ОТРАБОТАН") {
          title = "✅ ЛК отработан";
          body = `#${p.card_id || ""} ${p.bank || ""} — оформи выплату`;
          hash = "#payouts";
        } else if (type === "lk-status-changed" && p.new_status === "БЛОК_БЕЗ_ОТРАБОТКИ") {
          title = "⛔ Блок без отработки";
          body = `#${p.card_id || ""} ${p.bank || ""} — проверь`;
          hash = "#lk";
        }

        if (title) window.pride.notify(title, body, hash);
      } catch (e) { /* silent */ }
    };
  } catch (e) {
    console.error("[PRIDE Desktop] SSE init failed:", e);
  }
});
