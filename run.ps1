<#
.SYNOPSIS
    Start the CricketApp API locally with auto-reload.

.DESCRIPTION
    The application package lives in app/, so the canonical import target is
    app.main:app. A thin root main.py also exists so the shorter
    "uvicorn main:app" command works, but app.main:app is what Docker runs.

    This script also changes to the repository root before starting, because
    the SQLite database (./cricket.db) and the local uploads directory
    (./uploads) are resolved relative to the current working directory. Running
    uvicorn from app/ would silently create a second database there.

.PARAMETER Port
    Port to listen on. Defaults to 8000.

.PARAMETER HostAddress
    Address to bind. Defaults to 127.0.0.1 (loopback only). Named
    HostAddress because $Host is a PowerShell automatic variable.

.PARAMETER NoReload
    Disable the file watcher.

.EXAMPLE
    .\run.ps1

.EXAMPLE
    .\run.ps1 -Port 8001 -NoReload

.NOTES
    If PowerShell blocks the script ("running scripts is disabled on this
    system"), either run it once with
        powershell -ExecutionPolicy Bypass -File .\run.ps1
    or allow local scripts for your user:
        Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
#>
[CmdletBinding()]
param(
    [int]$Port = 8000,
    [string]$HostAddress = "127.0.0.1",
    [switch]$NoReload
)

$ErrorActionPreference = "Stop"

# Always start from the repository root, never from the caller's directory.
Set-Location -LiteralPath $PSScriptRoot

# Prefer the project virtualenv so the script works without activation.
$venvPython = Join-Path $PSScriptRoot "venv\Scripts\python.exe"
if (Test-Path -LiteralPath $venvPython) {
    $python = $venvPython
}
else {
    $python = "python"
    Write-Warning "venv\Scripts\python.exe not found; falling back to 'python' on PATH."
}

# Fail early with an actionable message instead of uvicorn's import error.
$null = & $python -c "import uvicorn" 2>&1
if ($LASTEXITCODE -ne 0) {
    throw "uvicorn is not installed for '$python'. Run: $python -m pip install -r requirements.txt"
}

if (-not (Test-Path -LiteralPath (Join-Path $PSScriptRoot ".env"))) {
    Write-Warning ".env not found; copy .env.example to .env first (defaults point at SQLite)."
}

$uvicornArgs = @(
    "-m", "uvicorn", "app.main:app",
    "--host", $HostAddress,
    "--port", $Port
)
if (-not $NoReload) {
    $uvicornArgs += "--reload"
}

Write-Host ""
Write-Host "Starting CricketApp"
Write-Host "  API:   http://${HostAddress}:$Port"
Write-Host "  Docs:  http://${HostAddress}:$Port/docs"
Write-Host "  Health http://${HostAddress}:$Port/health"
Write-Host "Press Ctrl+C to stop."
Write-Host ""

& $python @uvicornArgs
exit $LASTEXITCODE
