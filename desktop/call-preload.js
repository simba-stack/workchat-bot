// Preload для call-popout.html — IPC мост между popout-окном и main процессом.
// Popout НЕ держит WebRTC / WebSocket — это чисто UI-зеркало.
// Состояние приходит из main-jarvis-окна через main process.

const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("prideCall", {
  // Подписка на изменения state (вызывается из call-popout.html)
  onState: (cb) => {
    ipcRenderer.on("call:state", (_e, state) => {
      try { cb(state); } catch (e) { console.error("onState cb:", e); }
    });
  },
  // Запросить текущее состояние сразу (на загрузке popout)
  requestState: () => ipcRenderer.send("call:request-state"),
  // Действия из popout → main-jarvis: 'mute' | 'deafen' | 'leave'
  action: (type, payload) => ipcRenderer.send("call:action", { type, payload }),
  // Закрыть popout
  close: () => ipcRenderer.send("call:close-popout"),
  // Свернуть popout (минимизировать окно — Electron native)
  minimize: () => ipcRenderer.send("call:minimize-popout"),
});
