const DESKTOP_PROTOCOL = "infoagent:";

function findDeepLink(args = []) {
  return args.find(
    (value) => typeof value === "string" && value.startsWith(DESKTOP_PROTOCOL),
  );
}

function parseDesktopLink(value) {
  let parsed;
  try {
    parsed = new URL(String(value || ""));
  } catch {
    return null;
  }
  if (parsed.protocol !== DESKTOP_PROTOCOL) {
    return null;
  }
  const route = `${parsed.hostname}${parsed.pathname}`.replace(/\/+$/, "");
  if (route === "oauth/callback") {
    return {
      type: "oauth",
      provider: String(parsed.searchParams.get("provider") || ""),
      state: String(parsed.searchParams.get("state") || ""),
      status: String(parsed.searchParams.get("status") || ""),
      errorCode: String(parsed.searchParams.get("error_code") || ""),
    };
  }
  if (route === "organization-invitation") {
    return {
      type: "organization-invitation",
      token: String(parsed.searchParams.get("token") || ""),
    };
  }
  return { type: "unknown", url: String(value || "") };
}

module.exports = {
  DESKTOP_PROTOCOL,
  findDeepLink,
  parseDesktopLink,
};
