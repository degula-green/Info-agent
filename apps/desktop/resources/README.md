# Desktop sidecars

Build outputs are placed here before packaging:

```text
resources/
  wechat-collector/
    wechat-collector.exe
  form-browser/
    form-browser.exe
    ms-playwright/
      chromium_headless_shell-<version>/
```

Development builds can set `collectorCommand` and `formBrowserCommand` in
`desktop.config.json` instead of using packaged binaries.

Build both sidecars from `apps/desktop`:

```powershell
npm run build:sidecars
```

The form-browser bundle includes Playwright's headless Chromium shell. A full
Chromium install is intentionally not shipped because the desktop command flow
currently runs the sidecar in headless mode.
