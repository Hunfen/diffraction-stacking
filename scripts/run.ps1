<#
.SYNOPSIS
  Static AstroPhoto launcher for Windows (PowerShell).

.DESCRIPTION
  Starts the FastAPI backend with Uvicorn from the project virtualenv.
  backend\.venv must already exist; run scripts\install.ps1 or
  scripts\update.ps1 first if it does not.

.EXAMPLE
  .\scripts\run.ps1
#>
[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'

$RootDir     = Split-Path -Parent $PSScriptRoot
$BackendDir  = Join-Path $RootDir 'backend'
$VenvDir     = Join-Path $BackendDir '.venv'
$VenvPython  = Join-Path $VenvDir 'Scripts\python.exe'

if (-not (Test-Path $VenvPython)) {
    Write-Host '[run][ERROR] backend\.venv was not found.' -ForegroundColor Red
    Write-Host ''
    Write-Host 'Run one of these first from the repository root:' -ForegroundColor Yellow
    Write-Host '  .\scripts\install.ps1              # first-time setup'
    Write-Host '  .\scripts\install.ps1 -WithCamera  # first-time setup with real-camera support'
    Write-Host '  .\scripts\update.ps1               # update an existing installation'
    Write-Host ''
    exit 1
}

Write-Host "[run] Starting Static AstroPhoto backend from $BackendDir" -ForegroundColor Cyan

Push-Location $BackendDir
try {
    & $VenvPython -m uvicorn app.main:app --host 0.0.0.0 --port 8000
    if ($LASTEXITCODE -ne 0) {
        Write-Host "[run][ERROR] Uvicorn exited with code $LASTEXITCODE." -ForegroundColor Red
        exit $LASTEXITCODE
    }
} finally {
    Pop-Location
}
