const path = require("node:path");
const crypto = require("node:crypto");
const {
  app,
  BrowserWindow,
  ipcMain,
  net,
  session,
  shell,
} = require("electron");

const { loadConfig } = require("./config");
const { findDeepLink, parseDesktopLink } = require("./desktop-links");
const { DesktopTaskRunner } = require("./desktop-task-runner");
const { LocalFormBrowserClient } = require("./form-browser-local-client");
const { SidecarManager } = require("./sidecar-manager");

let mainWindow = null;
let sidecarManager = null;
let taskRunner = null;
let desktopConfig = null;
let pendingDeepLink = null;
let refreshCookie = "";

function localSidecarToken() {
  return crypto.randomBytes(32).toString("hex");
}

const gotSingleInstanceLock = app.requestSingleInstanceLock();
if (!gotSingleInstanceLock) {
  app.quit();
}

function registerProtocol() {
  if (process.defaultApp && process.argv.length >= 2) {
    app.setAsDefaultProtocolClient("infoagent", process.execPath, [
      path.resolve(process.argv[1]),
    ]);
    return;
  }
  app.setAsDefaultProtocolClient("infoagent");
}

function dispatchDeepLink(value) {
  const link = parseDesktopLink(value);
  if (!link) {
    return;
  }
  if (!mainWindow || mainWindow.isDestroyed()) {
    pendingDeepLink = link;
    return;
  }
  if (!mainWindow.isVisible()) {
    mainWindow.show();
  }
  mainWindow.focus();
  mainWindow.webContents.send("info-agent:deep-link", link);
}

function dispatchFromArgs(args) {
  const value = findDeepLink(args);
  if (value) {
    dispatchDeepLink(value);
  }
}

const fs = require("node:fs");

function resolveWebEntry(resourcesPath) {
  const bundled = path.join(resourcesPath, "web", "index.html");
  if (fs.existsSync(bundled)) {
    return bundled;
  }
  return path.resolve(__dirname, "../../web/dist/index.html");
}

function responseCookieValues(headers = {}) {
  for (const [name, value] of Object.entries(headers)) {
    if (name.toLowerCase() !== "set-cookie") continue;
    return Array.isArray(value) ? value : [String(value)];
  }
  return [];
}

function rememberRefreshCookie(headers) {
  for (const value of responseCookieValues(headers)) {
    const pair = String(value).split(";", 1)[0];
    const separator = pair.indexOf("=");
    if (separator < 0) continue;
    const name = pair.slice(0, separator).trim();
    if (name !== "info_agent_refresh") continue;
    refreshCookie = pair.slice(separator + 1).trim();
  }
}

function parseResponseBody(raw) {
  if (!raw) return null;
  try {
    return JSON.parse(raw);
  } catch {
    return raw;
  }
}

function requestCoreRefresh(config) {
  return new Promise((resolve, reject) => {
    if (!refreshCookie) {
      resolve({
        status: 401,
        body: {
          code: "AUTH_UNAUTHENTICATED",
          message: "authentication required",
        },
      });
      return;
    }
    const request = net.request({
      method: "POST",
      url: `${config.apiBaseUrl}/api/core/auth/refresh`,
      session: session.defaultSession,
      credentials: "omit",
      headers: {
        Accept: "application/json",
        "Content-Type": "application/json",
        Cookie: `info_agent_refresh=${refreshCookie}`,
      },
    });
    const chunks = [];
    request.on("response", (response) => {
      response.on("data", (chunk) => chunks.push(Buffer.from(chunk)));
      response.on("end", () => {
        rememberRefreshCookie(response.headers);
        resolve({
          status: response.statusCode,
          body: parseResponseBody(Buffer.concat(chunks).toString("utf8")),
        });
      });
    });
    request.on("error", reject);
    request.end();
  });
}

function installCoreAuthCookieBridge(config) {
  const authURLPattern = `${config.apiBaseUrl}/api/core/auth/*`;
  session.defaultSession.webRequest.onHeadersReceived(
    { urls: [authURLPattern] },
    (details, callback) => {
      rememberRefreshCookie(details.responseHeaders);
      callback({ responseHeaders: details.responseHeaders });
    },
  );
}

async function createWindow(config) {
  mainWindow = new BrowserWindow({
    width: 1440,
    height: 960,
    minWidth: 1100,
    minHeight: 720,
    webPreferences: {
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
      preload: path.join(__dirname, "preload.js"),
      additionalArguments: [
        `--info-agent-api=${config.apiBaseUrl}`,
        `--info-agent-web=${config.webBaseUrl}`,
        `--info-agent-invite=${config.invitationBaseUrl}`,
      ],
    },
  });
  await mainWindow.loadFile(resolveWebEntry(process.resourcesPath));
  if (pendingDeepLink) {
    mainWindow.webContents.send("info-agent:deep-link", pendingDeepLink);
    pendingDeepLink = null;
  }
}

async function start(config) {
  config.collectorToken ||= localSidecarToken();
  config.formBrowserToken ||= localSidecarToken();
  const sidecarResourcesPath = app.isPackaged
    ? process.resourcesPath
    : path.resolve(__dirname, "..");
  sidecarManager = new SidecarManager({
    resourcesPath: sidecarResourcesPath,
    userDataPath: app.getPath("userData"),
  });
  sidecarManager.startConfigured(config);
  taskRunner = new DesktopTaskRunner({
    apiBaseUrl: config.apiBaseUrl,
    statePath: path.join(app.getPath("userData"), "wechat-collector.json"),
    formBrowser: new LocalFormBrowserClient({
      baseUrl: config.formBrowserUrl,
      token: config.formBrowserToken,
    }),
  });
  taskRunner.start();
  await createWindow(config);
}

if (!gotSingleInstanceLock) {
  app.quit();
} else {
  app.on("second-instance", (_event, argv) => {
    dispatchFromArgs(argv);
  });

  app.on("open-url", (event, url) => {
    event.preventDefault();
    dispatchDeepLink(url);
  });

  app.whenReady().then(async () => {
    registerProtocol();
    desktopConfig = loadConfig({
      app,
      resourcesPath: process.resourcesPath,
    });
    installCoreAuthCookieBridge(desktopConfig);
    ipcMain.handle("info-agent:status", () => ({
      sidecars: sidecarManager ? sidecarManager.status() : {},
    }));
    ipcMain.handle("info-agent:core-refresh", () =>
      requestCoreRefresh(desktopConfig),
    );
    ipcMain.handle("info-agent:open-external", async (_event, url) => {
      await shell.openExternal(String(url || ""));
    });
    ipcMain.handle("info-agent:local-agent-request", async (_event, requestPath, init) => {
      const headers = {
        Accept: "application/json",
        ...(init && init.body ? { "Content-Type": "application/json" } : {}),
      };
      if (desktopConfig.collectorToken) {
        headers["X-Collector-Token"] = desktopConfig.collectorToken;
      }
      const response = await fetch(
        `${desktopConfig.collectorUrl}${String(requestPath || "")}`,
        {
          method: init?.method || "GET",
          headers,
          body: init?.body,
        },
      );
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) {
        throw new Error(
          payload.detail || payload.message || `local agent failed (${response.status})`,
        );
      }
      return payload;
    });
    dispatchFromArgs(process.argv);
    await start(desktopConfig);
  });
}

app.on("window-all-closed", () => {
  if (process.platform !== "darwin") {
    app.quit();
  }
});

app.on("before-quit", () => {
  if (taskRunner) {
    taskRunner.stop();
  }
  if (sidecarManager) {
    sidecarManager.stop();
  }
});
