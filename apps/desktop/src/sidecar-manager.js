const { spawn } = require("node:child_process");
const { EventEmitter } = require("node:events");
const fs = require("node:fs");
const path = require("node:path");

class SidecarManager extends EventEmitter {
  constructor({ resourcesPath, userDataPath, logger = console }) {
    super();
    this.resourcesPath = resourcesPath;
    this.userDataPath = userDataPath;
    this.logger = logger;
    this.processes = new Map();
    this.stopping = false;
  }

  resolve(command, fallbackName) {
    const configured = String(command || "").trim();
    if (configured) {
      return configured;
    }
    const suffix = process.platform === "win32" ? ".exe" : "";
    const candidates = [
      path.join(this.resourcesPath, "resources", fallbackName, `${fallbackName}${suffix}`),
      path.join(this.resourcesPath, fallbackName, `${fallbackName}${suffix}`),
    ];
    return candidates.find((candidate) => fs.existsSync(candidate)) || "";
  }

  start(name, command, args = [], env = {}) {
    if (!command) {
      this.emit("status", { name, status: "not_configured" });
      return false;
    }
    const child = spawn(command, args, {
      cwd: path.dirname(command),
      env: { ...process.env, ...env },
      windowsHide: true,
      stdio: ["ignore", "pipe", "pipe"],
    });
    this.processes.set(name, child);
    child.stdout.on("data", (chunk) => this.logger.info(`[${name}] ${chunk.toString().trim()}`));
    child.stderr.on("data", (chunk) => this.logger.error(`[${name}] ${chunk.toString().trim()}`));
    child.on("spawn", () => this.emit("status", { name, status: "running", pid: child.pid }));
    child.on("exit", (code, signal) => {
      this.processes.delete(name);
      this.emit("status", { name, status: "exited", code, signal });
      if (!this.stopping && code !== 0) {
        setTimeout(() => this.start(name, command, args, env), 2000);
      }
    });
    return true;
  }

  startConfigured(config) {
    const collectorCommand = this.resolve(
      config.collectorCommand,
      "wechat-collector",
    );
    const formBrowserCommand = this.resolve(
      config.formBrowserCommand,
      "form-browser",
    );
    this.start("wechat-collector", collectorCommand, config.collectorArgs, {
      WECHAT_COLLECTOR_STATE_FILE: path.join(
        this.userDataPath,
        "wechat-collector.json",
      ),
      KNOWLEDGE_BASE_URL: config.knowledgeBaseUrl,
      WECHAT_COLLECTOR_PORT: "8091",
    });
    this.start("form-browser", formBrowserCommand, config.formBrowserArgs, {
      FORM_BROWSER_PORT: "8500",
      FORM_BROWSER_HOST: "127.0.0.1",
      FORM_BROWSER_API_TOKEN: config.formBrowserToken,
      FORM_BROWSER_HEADLESS: config.formBrowserHeadless ? "true" : "false",
    });
  }

  stop() {
    this.stopping = true;
    for (const child of this.processes.values()) {
      child.kill();
    }
    this.processes.clear();
  }

  status() {
    return Object.fromEntries(
      [...this.processes.entries()].map(([name, child]) => [
        name,
        { status: "running", pid: child.pid },
      ]),
    );
  }
}

module.exports = { SidecarManager };
