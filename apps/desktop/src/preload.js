const { contextBridge, ipcRenderer } = require("electron");

const apiArgument = process.argv.find((value) =>
  value.startsWith("--info-agent-api="),
);
const webArgument = process.argv.find((value) =>
  value.startsWith("--info-agent-web="),
);
const invitationArgument = process.argv.find((value) =>
  value.startsWith("--info-agent-invite="),
);
const apiBaseUrl = apiArgument
  ? apiArgument.slice("--info-agent-api=".length)
  : "";
const webBaseUrl = webArgument
  ? webArgument.slice("--info-agent-web=".length)
  : "";
const invitationBaseUrl = invitationArgument
  ? invitationArgument.slice("--info-agent-invite=".length)
  : "";

contextBridge.exposeInMainWorld("infoAgentDesktop", {
  apiBaseUrl,
  webBaseUrl,
  invitationBaseUrl,
  getStatus: () => ipcRenderer.invoke("info-agent:status"),
  refreshCoreSession: () => ipcRenderer.invoke("info-agent:core-refresh"),
  openExternal: (url) => ipcRenderer.invoke("info-agent:open-external", url),
  onDeepLink: (callback) => {
    const listener = (_event, link) => callback(link);
    ipcRenderer.on("info-agent:deep-link", listener);
    return () => ipcRenderer.removeListener("info-agent:deep-link", listener);
  },
  localAgentRequest: (requestPath, init) =>
    ipcRenderer.invoke("info-agent:local-agent-request", requestPath, init),
});
