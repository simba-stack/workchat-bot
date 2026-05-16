// Picker preload — мост между picker.html и main.js
const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("pridepicker", {
  onSources: (cb) => {
    ipcRenderer.on("picker-sources", (_e, sources) => cb(sources));
    // Сразу запросить если уже готовы
    ipcRenderer.send("picker-ready");
  },
  confirm: (sourceId) => ipcRenderer.send("picker-confirm", sourceId),
  cancel: () => ipcRenderer.send("picker-cancel"),
});
