const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("clipperDesktop", {
  getStatus: () => ipcRenderer.invoke("desktop:get-status"),
  windowControl: (action) => ipcRenderer.invoke("desktop:window-control", action),
  openOAuth: (targetUrl) => ipcRenderer.invoke("desktop:open-oauth", targetUrl),
  restartApp: () => ipcRenderer.invoke("desktop:restart-app")
});
