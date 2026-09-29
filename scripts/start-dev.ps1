$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$corePath = Join-Path $projectRoot "services\core"
$knowledgePath = Join-Path $projectRoot "services\knowledge"
$ragPath = Join-Path $projectRoot "services\rag"
$webPath = Join-Path $projectRoot "apps\web"
$wechatCollectorPath = Join-Path $projectRoot "services\collectors\wechat"
$agentPath = Join-Path $projectRoot "services\agent"
$intentPath = Join-Path $projectRoot "services\intent"
$nginxPath = Join-Path $projectRoot "gateway\nginx"
$nginxRuntime = "C:\info-agent-nginx"

# Go needs these variables when services are launched in detached Windows windows.
if (-not $env:LOCALAPPDATA) { $env:LOCALAPPDATA = Join-Path $env:USERPROFILE 'AppData\Local' }
if (-not $env:GOCACHE) { $env:GOCACHE = Join-Path $env:LOCALAPPDATA 'go-build' }

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

function Wait-IntentHealthy([string]$BaseUrl, [int]$TimeoutSeconds = 60) {
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        try {
            $health = Invoke-RestMethod -Uri "$BaseUrl/health" -TimeoutSec 3
            if ($health.status -eq "ok") { return $true }
        } catch {}
        Start-Sleep -Seconds 2
    }
    return $false
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

# The Agent owns an HTTP API plus two queue consumers (worker.py and
# knowledge_worker.py) that run from its own virtualenv. They are matched by
# the Agent interpreter so the RAG worker -- also a worker.py -- is untouched.
function Stop-AgentServiceChain([string]$AgentDirectory, [string]$AgentInterpreter) {
    $processes = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue)
    if (-not $processes) { return }

    $agentRoot = [IO.Path]::GetFullPath($AgentDirectory).TrimEnd('\')
    $interpreterMarker = [IO.Path]::GetFullPath($AgentInterpreter)
    $targetIDs = New-Object 'System.Collections.Generic.HashSet[int]'
    foreach ($process in $processes) {
        $commandLine = [string]$process.CommandLine
        $executable = [string]$process.ExecutablePath
        $ownsInterpreter = $executable.Equals($interpreterMarker, [StringComparison]::OrdinalIgnoreCase)
        $isAgentEntry = $commandLine -match '(?i)(^|[\s\\/])(worker|knowledge_worker)\.py'
        if ($ownsInterpreter -and ($isAgentEntry -or $commandLine.IndexOf($agentRoot, [StringComparison]::OrdinalIgnoreCase) -ge 0)) {
            [void]$targetIDs.Add([int]$process.ProcessId)
        }
    }
    if ($targetIDs.Count -eq 0) { return }

    # Include the system-Python child spawned by the virtualenv launcher, but
    # never walk upward to the terminal that launched the process.
    $stopIDs = New-Object 'System.Collections.Generic.HashSet[int]'
    foreach ($targetID in $targetIDs) { [void]$stopIDs.Add($targetID) }
    $changed = $true
    while ($changed) {
        $changed = $false
        foreach ($process in $processes) {
            if ($stopIDs.Contains([int]$process.ParentProcessId) -and $stopIDs.Add([int]$process.ProcessId)) { $changed = $true }
        }
    }
    $targets = $processes | Where-Object { $stopIDs.Contains([int]$_.ProcessId) } | Sort-Object { $_.ParentProcessId } -Descending
    foreach ($target in $targets) {
        if (Get-Process -Id $target.ProcessId -ErrorAction SilentlyContinue) {
            Write-Host "Stopping existing Agent process (PID $($target.ProcessId))..."
            Stop-Process -Id $target.ProcessId -Force -ErrorAction SilentlyContinue
        }
    }
}


# A collected message only becomes a Task if both consumers are alive. When
# they are missing nothing errors anywhere: the to-do list simply stays empty,
# which is impossible to diagnose from the UI. Verify them after launch.
function Test-AgentConsumers([string]$AgentDirectory, [string]$AgentInterpreter) {
    $interpreter = [IO.Path]::GetFullPath($AgentInterpreter)
    $missing = @()
    foreach ($entry in @(
        @{ Name = 'knowledge_worker.py'; Pattern = '(^|[\s\\/])knowledge_worker\.py(\s|$)' },
        @{ Name = 'worker.py';           Pattern = '(^|[\s\\/])worker\.py(\s|$)' }
    )) {
        $found = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
            $_.ExecutablePath -and
            ([IO.Path]::GetFullPath($_.ExecutablePath)).Equals($interpreter, [StringComparison]::OrdinalIgnoreCase) -and
            $_.CommandLine -match $entry.Pattern
        })
        if ($found.Count -eq 0) { $missing += $entry.Name }
    }
    return $missing
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
# Join-Path throws when the underlying environment variable is unset, so the
# fallback candidates are assembled defensively.
$uvCandidates = @()
if ($env:LOCALAPPDATA) { $uvCandidates += (Join-Path $env:LOCALAPPDATA 'Programs\uv\uv.exe') }
if ($env:APPDATA) { $uvCandidates += (Join-Path $env:APPDATA 'Python\Python311\Scripts\uv.exe') }
if ($env:USERPROFILE) { $uvCandidates += (Join-Path $env:USERPROFILE '.local\bin\uv.exe') }
$uv = Find-Tool 'uv' $uvCandidates
$nginx = Find-Tool 'nginx'

if (-not $go) { throw "Go not found. Install Go first." }
if (-not $npm) { throw "npm not found. Install Node.js first." }
if (-not $uv) {
    Write-Warning 'uv not found. This script will reuse the existing .venv folders; install uv to refresh dependencies automatically.'
}

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

# Read the Agent settings before dependency sync so a normal AGENT_LLM-only run
# does not install Torch and the Laya model stack.
Import-EnvFile (Join-Path $agentPath '.env')
$configuredUnderstanding = [string]$env:AGENT_UNDERSTANDING_PROVIDER
$intentEnabled = @('laya', 'hybrid') -contains $configuredUnderstanding.ToLowerInvariant()

if ($uv) {
    Write-Host 'Synchronizing RAG virtual environment...'
    & $uv sync --project $ragPath
    if ($LASTEXITCODE -ne 0) { throw "RAG uv sync failed with exit code $LASTEXITCODE." }

    Write-Host 'Synchronizing WeChat Collector virtual environment...'
    & $uv sync --project $wechatCollectorPath
    if ($LASTEXITCODE -ne 0) { throw "WeChat Collector uv sync failed with exit code $LASTEXITCODE." }

    Write-Host 'Synchronizing Agent virtual environment...'
    & $uv sync --project $agentPath
    if ($LASTEXITCODE -ne 0) { throw "Agent uv sync failed with exit code $LASTEXITCODE." }

    if ($intentEnabled) {
        Write-Host 'Synchronizing Laya intent virtual environment...'
        & $uv sync --project $intentPath
        if ($LASTEXITCODE -ne 0) { throw "Laya intent uv sync failed with exit code $LASTEXITCODE." }
    }
}

# Run each Python service with its project-local interpreter directly.  Using
# `uv run` for the long-lived Windows processes can leave a launcher process
# that respawns the service under the system Python interpreter.
$ragPython = Join-Path $ragPath '.venv\Scripts\python.exe'
$wechatPython = Join-Path $wechatCollectorPath '.venv\Scripts\python.exe'
$agentPython = Join-Path $agentPath '.venv\Scripts\python.exe'
$intentPython = Join-Path $intentPath '.venv\Scripts\python.exe'
if (-not (Test-Path $ragPython)) { throw "RAG virtualenv interpreter not found: $ragPython" }
if (-not (Test-Path $wechatPython)) { throw "WeChat Collector virtualenv interpreter not found: $wechatPython" }
if (-not (Test-Path $agentPython)) { throw "Agent virtualenv interpreter not found: $agentPython. Run 'uv sync --project services\agent' first." }
if ($intentEnabled -and -not (Test-Path $intentPython)) { throw "Laya intent virtualenv interpreter not found: $intentPython. Run 'uv sync --project services\intent' first." }

Stop-RagWorkerChain $ragPath $ragPython
Stop-AgentServiceChain $agentPath $agentPython

Import-EnvFile (Join-Path $corePath '.env')
Import-EnvFile (Join-Path $knowledgePath '.env')
Import-EnvFile (Join-Path $ragPath '.env')
Import-EnvFile (Join-Path $agentPath '.env')
if ($intentEnabled) {
    Import-EnvFile (Join-Path $intentPath '.env')
}

# services\agent\.env owns the port; the health hints and the stale-listener
# sweep below follow whatever it declares.
$agentHttpPort = 8095
if ($env:AGENT_HTTP_PORT) { $agentHttpPort = [int]$env:AGENT_HTTP_PORT }
$intentHttpPort = 8110
if ($env:LAYA_PORT) { $intentHttpPort = [int]$env:LAYA_PORT }
if ($agentHttpPort -eq $intentHttpPort) {
    throw "AGENT_HTTP_PORT and LAYA_PORT cannot both be $agentHttpPort."
}
if (-not $env:AGENT_LAYAYA_BASE_URL) {
    $env:AGENT_LAYAYA_BASE_URL = "http://127.0.0.1:$intentHttpPort"
}
if (-not $env:KNOWLEDGE_INTERNAL_SERVICE_TOKEN) { $env:KNOWLEDGE_INTERNAL_SERVICE_TOKEN = 'local-development-only' }
if (-not $env:COLLECTOR_INTERNAL_TOKEN) { $env:COLLECTOR_INTERNAL_TOKEN = 'local-development-only' }
if (-not $env:KNOWLEDGE_CORE_SERVICE_TOKEN) {
    # Core's authorization endpoint uses the same shared token as RAG in local dev.
    # Prefer the already loaded CORE_KNOWLEDGE_AUTHZ_TOKEN/CORE_RAG_AUTHZ_TOKEN
    # before falling back to the dev token; otherwise Knowledge receives a token
    # that Core will reject with 403 on organization membership checks.
    $env:KNOWLEDGE_CORE_SERVICE_TOKEN = if ($env:CORE_KNOWLEDGE_AUTHZ_TOKEN) { $env:CORE_KNOWLEDGE_AUTHZ_TOKEN } elseif ($env:CORE_RAG_AUTHZ_TOKEN) { $env:CORE_RAG_AUTHZ_TOKEN } else { 'local-development-only' }
}
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
$portsToStop = @(80, 5173, 8000, 8080, 8090, 8091, $agentHttpPort)
if ($intentEnabled) { $portsToStop += $intentHttpPort }
foreach ($port in $portsToStop) {
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
if ($intentEnabled) {
    if (-not $env:LAYA_HOST) { $env:LAYA_HOST = '127.0.0.1' }
    if (-not $env:LAYA_MODELS) { $env:LAYA_MODELS = 'multilingual' }
    Start-ServiceWindow "info-agent intent :$intentHttpPort" $intentPath "& '$intentPython' app.py"
    if (Wait-IntentHealthy "http://127.0.0.1:$intentHttpPort") {
        Write-Host "Laya intent sidecar ready on http://127.0.0.1:$intentHttpPort"
    } else {
        Write-Warning "Laya intent sidecar did not become healthy on port $intentHttpPort. Hybrid calls will fall back to the LLM."
    }
}

# The Agent consumes the Knowledge outbox; without these processes collected
# messages never become Tasks. The task worker is required even when the API
# creates the Task, because planning and execution happen in the worker.
Start-ServiceWindow "info-agent agent :$agentHttpPort" $agentPath "& '$agentPython' -m uvicorn app.main:app --host 0.0.0.0 --port $agentHttpPort"
if ($env:agent_REDIS_URL -or $env:AGENT_REDIS_URL) {
    Start-ServiceWindow 'info-agent agent-knowledge-worker' $agentPath "& '$agentPython' knowledge_worker.py"
    Start-ServiceWindow 'info-agent agent-task-worker' $agentPath "& '$agentPython' worker.py"
} else {
    Write-Warning 'Agent Redis URL missing. The Agent API was started, but the two Agent workers were skipped.'
}


$agentConsumersMissing = Test-AgentConsumers $agentPath $agentPython
if ($agentConsumersMissing.Count -gt 0) {
    Write-Warning ('Agent consumers are not running: ' + ($agentConsumersMissing -join ', ') + '. Collected messages will not become to-dos; check their windows for errors.')
} else {
    Write-Host 'Agent consumers running: knowledge_worker.py, worker.py'
}
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
    Write-Warning 'nginx not found. Core, Knowledge, RAG, Web, WeChat Collector and Agent were started; gateway was skipped.'
    Write-Host 'Install nginx and add it to PATH, then run this file again.'
}

Write-Host 'Core: http://localhost:8080/health'
Write-Host 'Knowledge: http://localhost:8090/health'
Write-Host 'RAG:  http://localhost:8000/health'
Write-Host 'Web:  http://localhost:5173'
Write-Host "Agent: http://localhost:$agentHttpPort/health"
if ($intentEnabled) { Write-Host "Intent: http://localhost:$intentHttpPort/health" }
Read-Host 'Press Enter to close this launcher'
