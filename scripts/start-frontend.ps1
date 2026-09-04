$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$webPath = Join-Path $projectRoot 'apps\web'
$npm = Get-Command npm -ErrorAction SilentlyContinue
if (-not $npm) { throw 'npm not found. Install Node.js first.' }
Set-Location -LiteralPath $webPath
if (-not (Test-Path (Join-Path $webPath 'node_modules'))) {
  & $npm.Source install
  if ($LASTEXITCODE -ne 0) { throw "npm install failed with exit code $LASTEXITCODE." }
}
& $npm.Source run dev -- --host 0.0.0.0
