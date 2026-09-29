# Intent Service

This sidecar serves `convaiinnovations/laya-multilingual` through the
`laya-serve` HTTP API. The Agent service calls it over the TypeSafe-compatible
`POST /v1/systemone` endpoint.

## Local run

```powershell
uv sync --project services/intent
Copy-Item services/intent/.env.example services/intent/.env
Get-Content services/intent/.env | ForEach-Object {
    if ($_ -and -not $_.StartsWith("#") -and $_.Contains("=")) {
        $parts = $_.Split("=", 2)
        [Environment]::SetEnvironmentVariable($parts[0], $parts[1], "Process")
    }
}
services/intent/.venv/Scripts/python.exe app.py
```

The server reads these environment variables:

| Variable | Purpose |
|---|---|
| `LAYA_HOST` | Bind address |
| `LAYA_PORT` | Bind port |
| `LAYA_DEVICE` | `auto`, `cpu`, `cuda`, or another Torch device |
| `LAYA_PRELOAD` | Load the checkpoint during startup |
| `LAYA_MODELS` | Keep this set to `multilingual` |
| `LAYA_REVISION` | Optional Hub commit pin |
| `LAYA_MODEL_PATH` | Optional local fine-tuned checkpoint directory |
| `LAYA_MAX_CONCURRENT` | Admission limit for concurrent requests |
| `LAYA_API_KEY` | Optional bearer token required from callers |

Health check:

```powershell
Invoke-RestMethod http://127.0.0.1:8110/health
```

The Agent service uses `AGENT_LAYAYA_BASE_URL` to reach this process. It does
not install Torch, Transformers, or model weights into `services/agent`.

See [BASELINE.md](BASELINE.md) for the measured stock-checkpoint result and the
reason the LLM fallback must remain enabled until a domain checkpoint is
trained.
