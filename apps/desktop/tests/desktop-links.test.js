const assert = require("node:assert/strict");
const test = require("node:test");

const { findDeepLink, parseDesktopLink } = require("../src/desktop-links");

test("desktop oauth callback is parsed without credentials", () => {
  const link = parseDesktopLink(
    "infoagent://oauth/callback?provider=feishu&state=state-1&status=active",
  );
  assert.deepEqual(link, {
    type: "oauth",
    provider: "feishu",
    state: "state-1",
    status: "active",
    errorCode: "",
  });
});

test("desktop link is found in Windows startup arguments", () => {
  assert.equal(
    findDeepLink([
      "C:\\Info Agent\\Info Agent.exe",
      "infoagent://oauth/callback?provider=feishu&status=error&error_code=oauth_denied",
    ]),
    "infoagent://oauth/callback?provider=feishu&status=error&error_code=oauth_denied",
  );
});

test("organization invitation link carries only its token", () => {
  assert.deepEqual(
    parseDesktopLink(
      "infoagent://organization-invitation?token=invite-token-1",
    ),
    {
      type: "organization-invitation",
      token: "invite-token-1",
    },
  );
});
