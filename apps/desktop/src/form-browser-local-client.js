class LocalFormBrowserClient {
  constructor({ baseUrl, token }) {
    this.baseUrl = String(baseUrl || "").replace(/\/$/, "");
    this.token = String(token || "");
  }

  async request(method, requestPath, body, raw = false) {
    const headers = { Accept: "application/json" };
    if (body !== undefined) {
      headers["Content-Type"] = "application/json";
    }
    if (this.token) {
      headers.Authorization = `Bearer ${this.token}`;
    }
    const response = await fetch(`${this.baseUrl}${requestPath}`, {
      method,
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    if (!response.ok) {
      let detail = {};
      try {
        detail = await response.json();
      } catch {
        detail = {};
      }
      throw new Error(
        detail.message || detail.detail || `form browser failed (${response.status})`,
      );
    }
    if (raw) {
      return Buffer.from(await response.arrayBuffer()).toString("base64");
    }
    return response.json();
  }

  async execute(operation, payload) {
    const sessionId = String(payload.session_id || "");
    switch (operation) {
      case "create_session":
        return this.request("POST", "/sessions");
      case "close_session":
        return this.request("DELETE", `/sessions/${encodeURIComponent(sessionId)}`);
      case "open":
        return this.request("POST", `/sessions/${encodeURIComponent(sessionId)}/open`, {
          url: payload.url,
        });
      case "read_grid":
        return this.request("GET", `/sessions/${encodeURIComponent(sessionId)}/grid`);
      case "write_grid":
        return this.request("POST", `/sessions/${encodeURIComponent(sessionId)}/grid/write`, {
          start_cell: payload.start_cell,
          values: payload.values,
        });
      case "clear_range":
        return this.request("POST", `/sessions/${encodeURIComponent(sessionId)}/grid/clear`, {
          range: payload.range,
        });
      case "read_form":
        return this.request("GET", `/sessions/${encodeURIComponent(sessionId)}/form`);
      case "fill_form":
        return this.request("POST", `/sessions/${encodeURIComponent(sessionId)}/form/fill`, {
          values: payload.values,
        });
      case "submit_form":
        return this.request("POST", `/sessions/${encodeURIComponent(sessionId)}/form/submit`, {
          ref: payload.ref || "",
        });
      case "screenshot":
        return {
          screenshot_base64: await this.request(
            "GET",
            `/sessions/${encodeURIComponent(sessionId)}/screenshot`,
            undefined,
            true,
          ),
        };
      case "send_input":
        return this.request("POST", `/sessions/${encodeURIComponent(sessionId)}/input`, payload.input);
      default:
        throw new Error(`unsupported desktop operation: ${operation}`);
    }
  }
}

module.exports = { LocalFormBrowserClient };
