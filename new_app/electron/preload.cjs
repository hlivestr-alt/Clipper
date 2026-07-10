const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("clipperDesktop", {
  getStatus: () => ipcRenderer.invoke("desktop:get-status"),
  windowControl: (action) => ipcRenderer.invoke("desktop:window-control", action),
  restartApp: () => ipcRenderer.invoke("desktop:restart-app")
});
