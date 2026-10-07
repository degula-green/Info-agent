# Desktop sidecars

Build outputs are placed here before packaging:

```text
resources/
  wechat-collector/
    wechat-collector.exe
  form-browser/
    form-browser.exe
```

Development builds can set `collectorCommand` and `formBrowserCommand` in
`desktop.config.json` instead of using packaged binaries.
