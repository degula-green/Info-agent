const crypto = require("node:crypto");
const fs = require("node:fs");

function loadDeviceIdentity(statePath) {
  const payload = JSON.parse(fs.readFileSync(statePath, "utf8"));
  const device = payload.device || {};
  const deviceId = String(device.device_id || "").trim();
  const deviceKey = String(device.device_key || "").trim();
  if (!deviceId || !deviceKey) {
    throw new Error("desktop device is not paired");
  }
  return { deviceId, deviceKey };
}

function signedHeaders(device, method, requestPath, body = Buffer.alloc(0)) {
  const timestamp = String(Math.floor(Date.now() / 1000));
  const payloadHash = crypto.createHash("sha256").update(body).digest("hex");
  const signed = `${timestamp}\n${method.toUpperCase()}\n${requestPath}\n${payloadHash}`;
  const signature = crypto
    .createHmac("sha256", device.deviceKey)
    .update(signed)
    .digest("hex");
  return {
    "X-Agent-Device-ID": device.deviceId,
    "X-Agent-Device-Key": device.deviceKey,
    "X-Agent-Timestamp": timestamp,
    "X-Agent-Payload-Hash": payloadHash,
    "X-Agent-Signature": signature,
  };
}

module.exports = { loadDeviceIdentity, signedHeaders };
