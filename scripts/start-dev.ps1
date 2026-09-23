$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$corePath = Join-Path $projectRoot "services\core"
$knowledgePath = Join-Path $projectRoot "services\knowledge"
$ragPath = Join-Path $projectRoot "services\rag"
$webPath = Join-Path $projectRoot "apps\web"
$wechatCollectorPath = Join-Path $projectRoot "services\collectors\wechat"
$nginxPath = Join-Path $projectRoot "gateway\nginx"
$nginxRuntime = "C:\info-agent-nginx"

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

function Stop-PortProcess([int]$Port) {
    $listeners = Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue
    foreach ($listener in $listeners) {
        $process = Get-Process -Id $listener.OwningProcess -ErrorAction SilentlyContinue
        if ($process) {
            Write-Host "Stopping existing project listener on port $Port (PID $($process.Id))..."
            Stop-Process -Id $process.Id -Force
        }
    }
}

function Stop-RagWorkerChain([string]$RagDirectory, [string]$RagInterpreter) {
    $processes = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue)
    if (-not $processes) { return }

    $ragMarker = [IO.Path]::GetFullPath($RagDirectory).TrimEnd('\')
    $interpreterMarker = [IO.Path]::GetFullPath($RagInterpreter)
    $workerIDs = New-Object 'System.Collections.Generic.HashSet[int]'
    foreach ($process in $processes) {
        $commandLine = [string]$process.CommandLine
        $executable = [string]$process.ExecutablePath
        $isWorker = $commandLine -match '(?i)(^|[\s\\/])worker\.py([\s"'']|$)'
        $isProjectWorker = $isWorker -and (
            $commandLine.IndexOf($ragMarker, [StringComparison]::OrdinalIgnoreCase) -ge 0 -or
            $executable.Equals($interpreterMarker, [StringComparison]::OrdinalIgnoreCase)
        )
        if ($isProjectWorker) { [void]$workerIDs.Add([int]$process.ProcessId) }
    }
    if ($workerIDs.Count -eq 0) { return }

    # Include the worker descendants (for example the system-Python child
    # created by the virtualenv launcher). Do not walk upward to a terminal
    # parent: doing so would also include unrelated sibling services.
    $stopIDs = New-Object 'System.Collections.Generic.HashSet[int]'
    foreach ($workerID in $workerIDs) {
        [void]$stopIDs.Add($workerID)
    }
    $changed = $true
    while ($changed) {
        $changed = $false
        foreach ($process in $processes) {
            if ($stopIDs.Contains([int]$process.ParentProcessId) -and $stopIDs.Add([int]$process.ProcessId)) { $changed = $true }
        }
    }
    $targets = $processes | Where-Object { $stopIDs.Contains([int]$_.ProcessId) } | Sort-Object { $_.ParentProcessId } -Descending
    foreach ($target in $targets) {
        $live = Get-Process -Id $target.ProcessId -ErrorAction SilentlyContinue
        if ($live) {
            Write-Host "Stopping existing RAG worker process (PID $($target.ProcessId))..."
            Stop-Process -Id $target.ProcessId -Force -ErrorAction SilentlyContinue
        }
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
$uv = Find-Tool 'uv' @(
    (Join-Path $env:LOCALAPPDATA 'Programs\uv\uv.exe'),
    (Join-Path $env:APPDATA 'Python\Python311\Scripts\uv.exe'),
    (Join-Path $env:USERPROFILE '.local\bin\uv.exe')
)
$nginx = Find-Tool 'nginx'

if (-not $go) { throw "Go not found. Install Go first." }
if (-not $npm) { throw "npm not found. Install Node.js first." }
if (-not $uv) { throw "uv not found. Install uv first." }

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

Write-Host 'Synchronizing RAG virtual environment...'
& $uv sync --project $ragPath
if ($LASTEXITCODE -ne 0) { throw "RAG uv sync failed with exit code $LASTEXITCODE." }

Write-Host 'Synchronizing WeChat Collector virtual environment...'
& $uv sync --project $wechatCollectorPath
if ($LASTEXITCODE -ne 0) { throw "WeChat Collector uv sync failed with exit code $LASTEXITCODE." }

# Run each Python service with its project-local interpreter directly.  Using
# `uv run` for the long-lived Windows processes can leave a launcher process
# that respawns the service under the system Python interpreter.
$ragPython = Join-Path $ragPath '.venv\Scripts\python.exe'
$wechatPython = Join-Path $wechatCollectorPath '.venv\Scripts\python.exe'
if (-not (Test-Path $ragPython)) { throw "RAG virtualenv interpreter not found: $ragPython" }
if (-not (Test-Path $wechatPython)) { throw "WeChat Collector virtualenv interpreter not found: $wechatPython" }

Stop-RagWorkerChain $ragPath $ragPython

Import-EnvFile (Join-Path $corePath '.env')
Import-EnvFile (Join-Path $knowledgePath '.env')
Import-EnvFile (Join-Path $ragPath '.env')
if (-not $env:KNOWLEDGE_INTERNAL_SERVICE_TOKEN) { $env:KNOWLEDGE_INTERNAL_SERVICE_TOKEN = 'local-development-only' }
if (-not $env:COLLECTOR_INTERNAL_TOKEN) { $env:COLLECTOR_INTERNAL_TOKEN = 'local-development-only' }
if (-not $env:KNOWLEDGE_CORE_SERVICE_TOKEN) { $env:KNOWLEDGE_CORE_SERVICE_TOKEN = 'local-development-only' }
if (-not $env:CORE_KNOWLEDGE_AUTHZ_TOKEN) { $env:CORE_KNOWLEDGE_AUTHZ_TOKEN = $env:KNOWLEDGE_CORE_SERVICE_TOKEN }
if (-not $env:RAG_KNOWLEDGE_API_TOKEN) { $env:RAG_KNOWLEDGE_API_TOKEN = $env:KNOWLEDGE_INTERNAL_SERVICE_TOKEN }
if (-not $env:RAG_KNOWLEDGE_BASE_URL) { $env:RAG_KNOWLEDGE_BASE_URL = 'http://127.0.0.1:8090' }
if (-not $env:RAG_DATABASE_URL) { $env:RAG_DATABASE_URL = $env:KNOWLEDGE_DATABASE_URL }
if (-not $env:RAG_AUTHZ_BASE_URL) { $env:RAG_AUTHZ_BASE_URL = 'http://127.0.0.1:8080' }
if (-not $env:RAG_AUTHZ_API_TOKEN) { $env:RAG_AUTHZ_API_TOKEN = $env:CORE_RAG_AUTHZ_TOKEN }
if (-not $env:RAG_MINIO_ENDPOINT) { $env:RAG_MINIO_ENDPOINT = $env:KNOWLEDGE_MINIO_ENDPOINT }
if (-not $env:RAG_MINIO_ACCESS_KEY) { $env:RAG_MINIO_ACCESS_KEY = $env:KNOWLEDGE_MINIO_ACCESS_KEY }
if (-not $env:RAG_MINIO_SECRET_KEY) { $env:RAG_MINIO_SECRET_KEY = $env:KNOWLEDGE_MINIO_SECRET_KEY }
if (-not $env:RAG_MINIO_SOURCE_BUCKET) { $env:RAG_MINIO_SOURCE_BUCKET = $env:KNOWLEDGE_MINIO_BUCKET }
if (-not $env:RAG_MINIO_SECURE) { $env:RAG_MINIO_SECURE = if ($env:KNOWLEDGE_MINIO_USE_SSL) { $env:KNOWLEDGE_MINIO_USE_SSL } else { 'false' } }
if (-not $env:RAG_REDIS_URL) { $env:RAG_REDIS_URL = $env:KNOWLEDGE_REDIS_URL }
if (-not $env:RAG_REDIS_DATABASE) { $env:RAG_REDIS_DATABASE = '1' }
if (-not $env:RAG_REDIS_INBOUND_STREAM) { $env:RAG_REDIS_INBOUND_STREAM = $env:KNOWLEDGE_REDIS_OUTBOUND_STREAM }

# Restart the complete local stack.  The previous script only stopped the
# WeChat collector, leaving stale Core/Knowledge/RAG/Web/Nginx processes on
# their ports and causing the frontend to report a unavailable Knowledge API.
foreach ($port in @(80, 5173, 8000, 8080, 8090, 8091)) {
    Stop-PortProcess $port
}

Start-ServiceWindow 'info-agent core :8080' $corePath "& '$go' run ./cmd/server"
Start-ServiceWindow 'info-agent knowledge :8090' $knowledgePath "& '$go' run ./cmd/server"
Start-ServiceWindow 'info-agent rag :8000' $ragPath "& '$ragPython' -m uvicorn app.main:app --host 0.0.0.0 --port 8000"
if ($env:RAG_REDIS_URL) {
    Start-ServiceWindow 'info-agent rag-worker' $ragPath "& '$ragPython' worker.py"
}
Start-ServiceWindow 'info-agent web :5173' $webPath "& '$npm' run dev -- --host 0.0.0.0"
Stop-PortProcess 8091
Start-ServiceWindow 'info-agent wechat collector :8091' $projectRoot "& '$wechatPython' -m services.collectors.wechat.main"

if ($nginx) {
    New-Item -ItemType Directory -Force -Path (Join-Path $nginxRuntime 'conf.d') | Out-Null
    New-Item -ItemType Directory -Force -Path (Join-Path $nginxRuntime 'logs') | Out-Null
    New-Item -ItemType Directory -Force -Path (Join-Path $nginxRuntime 'temp\client_body_temp'), (Join-Path $nginxRuntime 'temp\proxy_temp'), (Join-Path $nginxRuntime 'temp\fastcgi_temp'), (Join-Path $nginxRuntime 'temp\uwsgi_temp'), (Join-Path $nginxRuntime 'temp\scgi_temp') | Out-Null
Copy-Item (Join-Path $nginxPath 'nginx.conf') (Join-Path $nginxRuntime 'nginx.conf') -Force
# The repository default.conf targets Docker Compose service names. Use the
# explicit localhost upstreams for this Windows single-host launcher.
Copy-Item (Join-Path $nginxPath 'conf.d\local.default.conf') (Join-Path $nginxRuntime 'conf.d\default.conf') -Force
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
