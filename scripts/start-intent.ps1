<#
.SYNOPSIS
	Start the Laya intent sidecar with the settings from services/intent/.env.

.DESCRIPTION
	Loads services/intent/.env, optionally overrides the served checkpoint and
	enables the intent-contract check. Use it to run the migrated
	laya-intent-v6 checkpoint without starting the whole dev stack.

.EXAMPLE
	./scripts/start-intent.ps1 -ModelPath services/intent/models/laya-intent-v6 -RequireIntentContract
#>
param(
	[string]$ModelPath = '',
	[switch]$RequireIntentContract,
	[int]$Port = 0
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$intentPath = Join-Path $projectRoot 'services\intent'
$python = Join-Path $intentPath '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) {
	throw "Laya intent interpreter not found: $python. Run 'uv sync --project services/intent' first."
}

$envFile = Join-Path $intentPath '.env'
if (Test-Path -LiteralPath $envFile) {
	Get-Content -LiteralPath $envFile | ForEach-Object {
		$line = $_.Trim()
		if (-not $line -or $line.StartsWith('#') -or -not $line.Contains('=')) { return }
		$parts = $line.Split('=', 2)
		Set-Item -Path ('Env:' + $parts[0].Trim()) -Value $parts[1].Trim()
	}
}

if ($ModelPath) {
	$resolved = Resolve-Path -LiteralPath $ModelPath
	$env:LAYA_MODEL_PATH = $resolved.Path
}
if ($RequireIntentContract) {
	$env:LAYA_REQUIRE_INTENT_CONTRACT = '1'
}
if ($Port -gt 0) {
	$env:LAYA_PORT = "$Port"
}

Push-Location $intentPath
try {
	& $python app.py
}
finally {
	Pop-Location
}
