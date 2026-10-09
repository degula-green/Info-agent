param(
    [string]$OutputRoot = "",
    [switch]$SkipBrowserInstall
)

$ErrorActionPreference = "Stop"

$desktopRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
$repoRoot = Resolve-Path (Join-Path $desktopRoot "..\..")
if (-not $OutputRoot) {
    $OutputRoot = Join-Path $desktopRoot "resources"
}

$buildRoot = Join-Path $env:TEMP "info-agent-sidecar-build"
$wechatRoot = Join-Path $repoRoot "services\collectors\wechat"
$formBrowserRoot = Join-Path $repoRoot "services\form-browser"
$wechatVenv = Join-Path $buildRoot "wechat-venv"
$formBrowserVenv = Join-Path $buildRoot "form-browser-venv"
$wechatOutput = Join-Path $OutputRoot "wechat-collector"
$formBrowserOutput = Join-Path $OutputRoot "form-browser"
$playwrightBrowserOutput = Join-Path $formBrowserOutput "ms-playwright"

function New-BuildVenv {
    param([string]$Path)

    if (-not (Test-Path (Join-Path $Path "Scripts\python.exe"))) {
        python -m venv $Path
    }
    $python = Join-Path $Path "Scripts\python.exe"
    & $python -m pip install --upgrade pip | Out-Host
    return $python
}

function Build-WechatCollector {
    $python = New-BuildVenv -Path $wechatVenv
    & $python -m pip install --upgrade pyinstaller
    & $python -m pip install -r (Join-Path $wechatRoot "requirements.txt")

    $distRoot = Join-Path $buildRoot "wechat-dist"
    $specRoot = Join-Path $buildRoot "wechat-spec"
    Remove-Item -LiteralPath $distRoot, $specRoot -Recurse -Force -ErrorAction SilentlyContinue

    Push-Location $wechatRoot
    try {
        & $python -m PyInstaller `
            --noconfirm `
            --clean `
            --onedir `
            --name wechat-collector `
            --paths $wechatRoot `
            --distpath $distRoot `
            --workpath (Join-Path $buildRoot "wechat-work") `
            --specpath $specRoot `
            --collect-all wechatauto `
            --collect-submodules wechatauto `
            --hidden-import pythoncom `
            --hidden-import pywintypes `
            --hidden-import win32timezone `
            (Join-Path $wechatRoot "main.py")
        if ($LASTEXITCODE -ne 0) {
            throw "wechat-collector PyInstaller build failed"
        }
    } finally {
        Pop-Location
    }

    Remove-Item -LiteralPath $wechatOutput -Recurse -Force -ErrorAction SilentlyContinue
    New-Item -ItemType Directory -Path $wechatOutput -Force | Out-Null
    Copy-Item -Path (Join-Path $distRoot "wechat-collector\*") `
        -Destination $wechatOutput `
        -Recurse `
        -Force
}

function Build-FormBrowser {
    $python = New-BuildVenv -Path $formBrowserVenv
    & $python -m pip install --upgrade pyinstaller
    & $python -m pip install `
        "fastapi==0.115.6" `
        "uvicorn[standard]==0.34.0" `
        "playwright==1.63.0" `
        "pydantic>=2.7,<3"

    $distRoot = Join-Path $buildRoot "form-browser-dist"
    $specRoot = Join-Path $buildRoot "form-browser-spec"
    Remove-Item -LiteralPath $distRoot, $specRoot -Recurse -Force -ErrorAction SilentlyContinue

    Push-Location $formBrowserRoot
    try {
        & $python -m PyInstaller `
            --noconfirm `
            --clean `
            --onedir `
            --name form-browser `
            --paths $formBrowserRoot `
            --distpath $distRoot `
            --workpath (Join-Path $buildRoot "form-browser-work") `
            --specpath $specRoot `
            --collect-all playwright `
            --collect-submodules app `
            --collect-data app `
            (Join-Path $formBrowserRoot "run_sidecar.py")
        if ($LASTEXITCODE -ne 0) {
            throw "form-browser PyInstaller build failed"
        }
    } finally {
        Pop-Location
    }

    Remove-Item -LiteralPath $formBrowserOutput -Recurse -Force -ErrorAction SilentlyContinue
    New-Item -ItemType Directory -Path $formBrowserOutput -Force | Out-Null
    Copy-Item -Path (Join-Path $distRoot "form-browser\*") `
        -Destination $formBrowserOutput `
        -Recurse `
        -Force

    if (-not $SkipBrowserInstall) {
        $env:PLAYWRIGHT_BROWSERS_PATH = $playwrightBrowserOutput
        & $python -m playwright install chromium-headless-shell
        if ($LASTEXITCODE -ne 0) {
            throw "Playwright Chromium install failed"
        }
    }
}

New-Item -ItemType Directory -Path $buildRoot -Force | Out-Null
New-Item -ItemType Directory -Path $OutputRoot -Force | Out-Null

Build-WechatCollector
Build-FormBrowser

Write-Host "Sidecars built:"
Write-Host "  $(Join-Path $wechatOutput 'wechat-collector.exe')"
Write-Host "  $(Join-Path $formBrowserOutput 'form-browser.exe')"
Write-Host "  Playwright Chromium: $playwrightBrowserOutput"
