$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$corePath = Join-Path $projectRoot "services\core"
$knowledgePath = Join-Path $projectRoot "services\knowledge"
$ragPath = Join-Path $projectRoot "services\rag"
$webPath = Join-Path $projectRoot "apps\web"
$wechatCollectorPath = Join-Path $projectRoot "services\collectors\wechat"
$nginxPath = Join-Path $projectRoot "gateway\nginx"
$nginxRuntime = "C:\info-agent-nginx"
$ragRuntime = Join-Path $ragPath ".runtime\python"
$ragRequirements = Join-Path $ragPath "requirements.txt"
$ragRequirementsStamp = Join-Path $ragRuntime ".requirements.sha256"
$wechatRuntime = Join-Path $wechatCollectorPath ".runtime\python"
$wechatRequirements = Join-Path $wechatCollectorPath "requirements.txt"
$wechatRequirementsStamp = Join-Path $wechatRuntime ".requirements.sha256"

function Find-Tool([string]$Name, [string[]]$Candidates = @()) {
    $command = Get-Command $Name -ErrorAction SilentlyContinue
    if ($command) { return $command.Source }
    foreach ($candidate in $Candidates) { if (Test-Path $candidate) { return $candidate } }
    return $null
}

function Start-ServiceWindow([string]$Title, [string]$WorkingDirectory, [string]$Command) {
    $safeTitle = $Title.Replace("'", "''")
    $safeDir = $WorkingDirectory.Replace("'", "''")
    $script = "`$Host.UI.RawUI.WindowTitle = '$safeTitle'; Set-Location -LiteralPath '$safeDir'; $Command"
    $encoded = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($script))
    if (Get-Command wt.exe -ErrorAction SilentlyContinue) {
        Start-Process wt.exe -ArgumentList @(
            '-w', '0', 'new-tab',
            'powershell.exe', '-NoLogo', '-NoExit', '-ExecutionPolicy', 'Bypass',
            '-EncodedCommand', $encoded
        ) | Out-Null
    } else {
        Start-Process powershell.exe -ArgumentList @(
            '-NoLogo', '-NoExit', '-ExecutionPolicy', 'Bypass', '-EncodedCommand', $encoded
        ) | Out-Null
    }
}

function Import-EnvFile([string]$Path) {
    if (-not (Test-Path $Path)) { return }
    foreach ($line in Get-Content -LiteralPath $Path) {
        $text = $line.Trim()
        if ($text -and -not $text.StartsWith('#') -and $text.Contains('=')) {
            $parts = $text.Split('=', 2)
            [Environment]::SetEnvironmentVariable($parts[0].Trim(), $parts[1].Trim().Trim('"').Trim("'"), 'Process')
        }
    }
}

$go = Find-Tool 'go' @('C:\Program Files\Go\bin\go.exe')
$npm = Find-Tool 'npm'
$uv = Find-Tool 'uv'
$python = Find-Tool 'python' @('C:\Program Files\Python311\python.exe')
$nginx = Find-Tool 'nginx'

if (-not $go) { throw "Go not found. Install Go first." }
if (-not $npm) { throw "npm not found. Install Node.js first." }
if (-not $uv) { throw "uv not found. Install uv first." }
if (-not $python) { throw "Python not found. Install Python 3.11 first." }

if (-not (Test-Path (Join-Path $webPath 'node_modules'))) {
    Write-Host 'Installing frontend dependencies...'
    Push-Location $webPath
    try {
        & $npm ci
        if ($LASTEXITCODE -ne 0) { throw "npm ci failed with exit code $LASTEXITCODE." }
    } finally {
        Pop-Location
    }
}

$requirementsHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $ragRequirements).Hash
$installedHash = if (Test-Path $ragRequirementsStamp) {
    (Get-Content -Raw -LiteralPath $ragRequirementsStamp).Trim()
} else {
    ''
}
if (-not (Test-Path (Join-Path $ragRuntime 'uvicorn')) -or $installedHash -ne $requirementsHash) {
    Write-Host 'Installing RAG dependencies with uv...'
    New-Item -ItemType Directory -Force -Path $ragRuntime | Out-Null
    & $uv pip install --python $python --target $ragRuntime --link-mode copy --upgrade --requirement $ragRequirements
    if ($LASTEXITCODE -ne 0) { throw "uv pip install failed with exit code $LASTEXITCODE." }
    Set-Content -LiteralPath $ragRequirementsStamp -Value $requirementsHash -Encoding ascii
}

$wechatRequirementsHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $wechatRequirements).Hash
$wechatInstalledHash = if (Test-Path $wechatRequirementsStamp) {
    (Get-Content -Raw -LiteralPath $wechatRequirementsStamp).Trim()
} else {
    ''
}
if (-not (Test-Path (Join-Path $wechatRuntime 'uvicorn')) -or $wechatInstalledHash -ne $wechatRequirementsHash) {
    Write-Host 'Installing WeChat Collector dependencies with uv...'
    New-Item -ItemType Directory -Force -Path $wechatRuntime | Out-Null
    & $uv pip install --python $python --target $wechatRuntime --link-mode copy --upgrade --requirement $wechatRequirements
    if ($LASTEXITCODE -ne 0) { throw "WeChat Collector dependency installation failed with exit code $LASTEXITCODE." }
    Set-Content -LiteralPath $wechatRequirementsStamp -Value $wechatRequirementsHash -Encoding ascii
}

Import-EnvFile (Join-Path $corePath '.env')
Import-EnvFile (Join-Path $knowledgePath '.env')
Import-EnvFile (Join-Path $ragPath '.env')
if (-not $env:KNOWLEDGE_INTERNAL_SERVICE_TOKEN) { $env:KNOWLEDGE_INTERNAL_SERVICE_TOKEN = 'local-development-only' }
if (-not $env:COLLECTOR_INTERNAL_TOKEN) { $env:COLLECTOR_INTERNAL_TOKEN = 'local-development-only' }

Start-ServiceWindow 'info-agent core :8080' $corePath "& '$go' run ./cmd/server"
Start-ServiceWindow 'info-agent knowledge :8090' $knowledgePath "& '$go' run ./cmd/server"
Start-ServiceWindow 'info-agent rag :8000' $ragPath "`$env:PYTHONPATH = '$ragRuntime'; & '$python' -m uvicorn app.main:app --host 0.0.0.0 --port 8000"
Start-ServiceWindow 'info-agent web :5173' $webPath "& '$npm' run dev -- --host 0.0.0.0"
if ($python) {
    Start-ServiceWindow 'info-agent wechat collector :8091' $projectRoot "`$env:PYTHONPATH = '$wechatRuntime;$projectRoot'; & '$python' -m services.collectors.wechat.main"
}

if ($nginx) {
    New-Item -ItemType Directory -Force -Path (Join-Path $nginxRuntime 'conf.d') | Out-Null
    New-Item -ItemType Directory -Force -Path (Join-Path $nginxRuntime 'logs') | Out-Null
    New-Item -ItemType Directory -Force -Path (Join-Path $nginxRuntime 'temp\client_body_temp'), (Join-Path $nginxRuntime 'temp\proxy_temp'), (Join-Path $nginxRuntime 'temp\fastcgi_temp'), (Join-Path $nginxRuntime 'temp\uwsgi_temp'), (Join-Path $nginxRuntime 'temp\scgi_temp') | Out-Null
    Copy-Item (Join-Path $nginxPath 'nginx.conf') (Join-Path $nginxRuntime 'nginx.conf') -Force
    Copy-Item (Join-Path $nginxPath 'conf.d\default.conf') (Join-Path $nginxRuntime 'conf.d\default.conf') -Force
    Start-ServiceWindow 'info-agent nginx :80' $nginxRuntime "& '$nginx' -p '$nginxRuntime' -c nginx.conf -g 'daemon off;'"
    Write-Host 'Gateway started on http://localhost:80'
} else {
    Write-Warning 'nginx not found. Core, Knowledge, RAG and Web were started; gateway was skipped.'
    Write-Host 'Install nginx and add it to PATH, then run this file again.'
}

Write-Host 'Core: http://localhost:8080/health'
Write-Host 'Knowledge: http://localhost:8090/health'
Write-Host 'RAG:  http://localhost:8000/health'
Write-Host 'Web:  http://localhost:5173'
Read-Host 'Press Enter to close this launcher'
