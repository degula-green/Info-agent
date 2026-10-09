const { loadDeviceIdentity, signedHeaders } = require("./device-auth");

class DesktopTaskRunner {
  constructor({
    apiBaseUrl,
    statePath,
    formBrowser,
    logger = console,
    pollSeconds = 1,
  }) {
    this.apiBaseUrl = String(apiBaseUrl || "").replace(/\/$/, "");
    this.statePath = statePath;
    this.formBrowser = formBrowser;
    this.logger = logger;
    this.pollSeconds = Math.max(1, Number(pollSeconds) || 1);
    this.running = false;
    this.timer = null;
  }

  async request(method, requestPath, payload) {
    const body = payload === undefined ? Buffer.alloc(0) : Buffer.from(JSON.stringify(payload));
    let device;
    try {
      device = loadDeviceIdentity(this.statePath);
    } catch (error) {
      this.logger.warn(`desktop task runner idle: ${error.message}`);
      return null;
    }
    const headers = {
      Accept: "application/json",
      ...signedHeaders(device, method, requestPath.split("?")[0], body),
    };
    if (body.length > 0) {
      headers["Content-Type"] = "application/json";
    }
    const response = await fetch(`${this.apiBaseUrl}${requestPath}`, {
      method,
      headers,
      body: body.length > 0 ? body : undefined,
    });
    if (response.status === 204) {
      return null;
    }
    if (!response.ok) {
      throw new Error(`desktop API failed (${response.status})`);
    }
    return response.json();
  }

  async claim() {
    const requestPath = "/api/agent/v1/desktop/tasks";
    return this.request("GET", `${requestPath}?wait_seconds=20`);
  }

  async report(task, status, result, errorCode = "", errorMessage = "") {
    return this.request(
      "POST",
      `/api/agent/v1/desktop/tasks/${encodeURIComponent(task.desktop_task_id)}/result`,
      {
        status,
        result: result || {},
        error_code: errorCode,
        error_message: errorMessage,
        side_effect_state:
          status === "completed" && task.side_effect_state === "pending"
            ? "committed"
            : task.side_effect_state,
      },
    );
  }

  async runOne() {
    const task = await this.claim();
    if (!task) {
      return false;
    }
    try {
      const result = await this.formBrowser.execute(
        task.operation,
        task.request_payload || {},
      );
      const waitingLogin = Boolean(result && result.login_required);
      await this.report(
        task,
        waitingLogin ? "waiting_login" : "completed",
        result,
      );
    } catch (error) {
      this.logger.error(`desktop task ${task.desktop_task_id} failed: ${error.message}`);
      await this.report(task, "failed", {}, "desktop_operation_failed", error.message);
    }
    return true;
  }

  async tick() {
    if (!this.running) {
      return;
    }
    try {
      await this.runOne();
    } catch (error) {
      this.logger.warn(`desktop task poll failed: ${error.message}`);
    } finally {
      if (this.running) {
        this.timer = setTimeout(() => this.tick(), this.pollSeconds * 1000);
      }
    }
  }

  start() {
    if (this.running) {
      return;
    }
    this.running = true;
    this.tick();
  }

  stop() {
    this.running = false;
    if (this.timer) {
      clearTimeout(this.timer);
      this.timer = null;
    }
  }
}

module.exports = { DesktopTaskRunner };
