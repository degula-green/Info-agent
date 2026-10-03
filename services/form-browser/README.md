# form-browser

Stateful browser service backing the Agent's form-filling capability.

Chromium lives here rather than inside the Agent, for the same reason the
renderer does: a browser is a large, frequently-updated attack surface, and a
crash here must degrade one capability instead of the Agent service.

## Operations

| method | path | purpose |
| --- | --- | --- |
| POST | `/sessions` | open a browser session |
| DELETE | `/sessions/{id}` | close it |
| POST | `/sessions/{id}/open` | navigate and classify the page |
| GET | `/sessions/{id}/grid` | read a spreadsheet as header + rows |
| POST | `/sessions/{id}/grid/write` | paste a block, read back, verify |
| POST | `/sessions/{id}/grid/clear` | clear a range |
| GET | `/sessions/{id}/screenshot` | PNG of the current view |
| GET | `/sessions/{id}/takeover` | whether the owner must sign in |

Every endpoint is protected by `FORM_BROWSER_API_TOKEN` when it is set; with no
token the service is reachable only from inside its own network, matching the
Crawl4AI sidecar's behaviour.

## Canvas grids

kdocs/WPS, Tencent Docs and similar editors paint the sheet to `<canvas>`, so
there are no cell elements to query. Two stable handles are used instead:

- the **name box** (`input.edit-box`) accepts `C12` or `A1:J20` and moves the
  selection;
- the **clipboard** carries the selected block as TSV in both directions.

Reads are "select, copy, parse"; writes are "select, paste, read back".
Every write is read back before it is reported, so a silent miss cannot pass
for a successful fill.

## Login and takeover

A document can be readable while refusing edits until the owner signs in. When
that is detected, `open` reports `login_required` and `grid/write` refuses with
`login_required` instead of writing blindly. That is the signal the Agent turns
into a takeover request.

Set `FORM_BROWSER_STORAGE_STATE` to a Playwright storage-state file to reuse an
already-authenticated session.

## Running

```bash
uv run uvicorn app.main:app --host 0.0.0.0 --port 8500
```

The container image bundles Chromium via the official Playwright base image.
