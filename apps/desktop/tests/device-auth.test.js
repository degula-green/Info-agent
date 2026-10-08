const assert = require("node:assert/strict");
const crypto = require("node:crypto");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const test = require("node:test");

const { loadDeviceIdentity, signedHeaders } = require("../src/device-auth");

test("desktop requests are signed with the paired device key", () => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), "info-agent-device-"));
  const statePath = path.join(directory, "wechat-collector.json");
  fs.writeFileSync(
    statePath,
    JSON.stringify({
      device: { device_id: "device-1", device_key: "secret-key" },
    }),
  );
  const device = loadDeviceIdentity(statePath);
  const body = Buffer.from(JSON.stringify({ status: "running" }));
  const headers = signedHeaders(
    device,
    "POST",
    "/api/agent/v1/desktop/tasks/task-1/result",
    body,
  );
  const signed = `${headers["X-Agent-Timestamp"]}\nPOST\n/api/agent/v1/desktop/tasks/task-1/result\n${headers["X-Agent-Payload-Hash"]}`;
  const expected = crypto
    .createHmac("sha256", "secret-key")
    .update(signed)
    .digest("hex");
  assert.equal(headers["X-Agent-Device-ID"], "device-1");
  assert.equal(headers["X-Agent-Signature"], expected);
});
