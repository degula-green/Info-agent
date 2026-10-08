const fs = require("node:fs");
const path = require("node:path");

function readJson(filePath) {
  try {
    return JSON.parse(fs.readFileSync(filePath, "utf8"));
  } catch (error) {
    if (error.code === "ENOENT") {
      return {};
    }
    throw error;
  }
}

function loadConfig({ app, resourcesPath }) {
  const userConfig = readJson(path.join(app.getPath("userData"), "desktop.config.json"));
  const bundledConfig = {
    ...readJson(path.join(resourcesPath, "desktop.config.json")),
    ...readJson(path.join(app.getAppPath(), "desktop.config.example.json")),
  };
  const config = { ...bundledConfig, ...userConfig };
  const apiBaseUrl = String(
    process.env.DESKTOP_API_BASE_URL || config.apiBaseUrl || "http://localhost",
  ).replace(/\/$/, "");
  const knowledgeBaseUrl = String(
    process.env.DESKTOP_KNOWLEDGE_BASE_URL ||
      config.knowledgeBaseUrl ||
      apiBaseUrl,
  ).replace(/\/$/, "");
  const webBaseUrl = String(
    process.env.DESKTOP_WEB_BASE_URL || config.webBaseUrl || apiBaseUrl,
  ).replace(/\/$/, "");
  const invitationBaseUrl = String(
    process.env.DESKTOP_INVITATION_BASE_URL ||
      config.invitationBaseUrl ||
      `${webBaseUrl}/invite`,
  ).replace(/\/$/, "");
  return {
    apiBaseUrl,
    knowledgeBaseUrl,
    webBaseUrl,
    invitationBaseUrl,
    collectorCommand: String(config.collectorCommand || ""),
    collectorArgs: Array.isArray(config.collectorArgs) ? config.collectorArgs : [],
    collectorUrl: String(
      config.collectorUrl || "http://127.0.0.1:8091",
    ).replace(/\/$/, ""),
    collectorToken: String(config.collectorToken || ""),
    formBrowserCommand: String(config.formBrowserCommand || ""),
    formBrowserArgs: Array.isArray(config.formBrowserArgs) ? config.formBrowserArgs : [],
    formBrowserUrl: String(
      config.formBrowserUrl || "http://127.0.0.1:8500",
    ).replace(/\/$/, ""),
    formBrowserToken: String(config.formBrowserToken || ""),
    formBrowserHeadless: Boolean(config.formBrowserHeadless),
  };
}

module.exports = { loadConfig, readJson };
