const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const test = require("node:test");

const { DesktopTaskRunner } = require("../src/desktop-task-runner");

test("desktop task runner reports local form browser results", async () => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), "info-agent-task-"));
  const statePath = path.join(directory, "wechat-collector.json");
  fs.writeFileSync(
    statePath,
    JSON.stringify({
      device: { device_id: "device-1", device_key: "secret-key" },
    }),
  );
  const calls = [];
  const runner = new DesktopTaskRunner({
    apiBaseUrl: "https://api.example.test",
    statePath,
    formBrowser: {
      async execute(operation, payload) {
        calls.push({ operation, payload });
        return { ok: true };
      },
    },
  });
  runner.request = async (method, requestPath, payload) => {
    calls.push({ method, requestPath, payload });
    if (requestPath.startsWith("/api/agent/v1/desktop/tasks?")) {
      return {
        desktop_task_id: "task-1",
        operation: "read_form",
        request_payload: { session_id: "s1" },
        side_effect_state: "none",
      };
    }
    return { status: "completed" };
  };
  const worked = await runner.runOne();
  assert.equal(worked, true);
  const localCall = calls.find((call) => call.operation === "read_form");
  const resultCall = calls.find((call) => call.payload?.status === "completed");
  assert.deepEqual(localCall, {
    operation: "read_form",
    payload: { session_id: "s1" },
  });
  assert.equal(resultCall.payload.result.ok, true);
});
