<#
.SYNOPSIS
  Static AstroPhoto launcher for Windows (PowerShell).

.DESCRIPTION
  Starts the FastAPI backend with Uvicorn from the project virtualenv.
  backend\.venv must already exist; run scripts\install.ps1 or
  scripts\update.ps1 first if it does not.

  If the chosen port is already serving this backend, the script detects it,
  prints the access URLs, and exits successfully instead of failing with
  "only one usage of each socket address".

.EXAMPLE
  .\scripts\run.ps1

.EXAMPLE
  .\scripts\run.ps1 -Port 8001
#>
[CmdletBinding()]
param(
    [int]$Port = 8000,
    [string]$BindHost = '0.0.0.0',
    [switch]$Stop,
    [switch]$Restart
)

$ErrorActionPreference = 'Stop'

if ($Stop -and $Restart) {
    Write-Host '[run][ERROR] Use either -Stop or -Restart, not both.' -ForegroundColor Red
    exit 2
}

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

function Write-Urls {
    param([int]$ListenPort)

    Write-Host ''
    Write-Host 'Access URLs:' -ForegroundColor Cyan
    Write-Host "  http://localhost:$ListenPort"

    try {
        $lanIps = @(Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
            Where-Object {
                $_.IPAddress -notlike '127.*' -and
                $_.IPAddress -notlike '169.254.*' -and
                $_.IPAddress -notlike '0.0.0.0'
            } |
            Select-Object -ExpandProperty IPAddress -Unique)
        foreach ($ip in $lanIps) {
            Write-Host "  http://${ip}:$ListenPort"
        }
    } catch {
        Write-Host '  (LAN IP detection skipped)' -ForegroundColor DarkGray
    }

    Write-Host ''
}

# Repository version (from backend/app/__init__.py), used to detect a stale
# backend process that is still serving the previous version of the code.
$repoVersion = ''
$initFile = Join-Path $BackendDir 'app\__init__.py'
if (Test-Path $initFile) {
    try {
        $initContent = Get-Content -Raw -Path $initFile
        if ($initContent -match 'APP_VERSION\s*=\s*"([^"]+)"') {
            $repoVersion = $Matches[1]
        }
    } catch {
        $repoVersion = ''
    }
}

# If something is already listening on the chosen port, check whether it is
# this backend. If it is healthy, reuse it instead of failing with WinError 10048.
$existingConnections = @()
try {
    $existingConnections = @(Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue)
} catch {
    $existingConnections = @()
}

if ($existingConnections.Count -gt 0) {
    $alreadyRunning = $false
    $backendVersion = ''
    $backendCamera = ''

    try {
        $health = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/api/health" -TimeoutSec 3 -ErrorAction Stop
        if ($health.ok -eq $true) {
            $alreadyRunning = $true
            $backendVersion = [string]$health.data.version
            $backendCamera = [string]$health.data.camera_driver
        }
    } catch {
        $alreadyRunning = $false
    }

    if ($alreadyRunning) {
        $backendPids = @($existingConnections |
            Select-Object -ExpandProperty OwningProcess -Unique |
            Where-Object { $_ -gt 0 })
        $pidList = ($backendPids -join ', ')

        if ($Stop -or $Restart) {
            foreach ($procId in $backendPids) {
                try {
                    Stop-Process -Id $procId -Force -ErrorAction Stop
                } catch {
                    Write-Host "[run][WARN] Could not stop PID $procId: $($_.Exception.Message)" -ForegroundColor Yellow
                }
            }

            $portFree = $false
            for ($i = 0; $i -lt 10; $i++) {
                Start-Sleep -Milliseconds 500
                $stillListening = @(Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue)
                if ($stillListening.Count -eq 0) {
                    $portFree = $true
                    break
                }
            }

            if (-not $portFree) {
                Write-Host "[run][ERROR] Port $Port is still in use after stopping PID(s): $pidList." -ForegroundColor Red
                exit 1
            }

            if ($Stop) {
                Write-Host "[run] Stopped backend on port $Port (PID $pidList)"
                exit 0
            }

            Write-Host "[run] Stopped old backend (PID $pidList), restarting…"
            # Fall through to the normal startup code below.
        }
        elseif ($repoVersion -and $backendVersion -and ($backendVersion -ne $repoVersion)) {
            Write-Host "[run][WARN] A backend is already running on port $Port, but its version is $backendVersion while the repository version is $repoVersion." -ForegroundColor Yellow
            Write-Host '[run][WARN] It is still serving the old code. Run with -Restart to stop it and start this version, or -Stop to stop it without starting.' -ForegroundColor Yellow
            Write-Urls -ListenPort $Port
            exit 1
        }
        else {
            Write-Host "[run] Backend is already running on port $Port (version $backendVersion, camera_driver=$backendCamera)." -ForegroundColor Green
            Write-Urls -ListenPort $Port
            exit 0
        }
    }
    else {
        $pids = (($existingConnections |
            Select-Object -ExpandProperty OwningProcess -Unique) -join ', ')

        Write-Host "[run][ERROR] Port $Port is already in use by another process (PID: $pids)." -ForegroundColor Red
        Write-Host ''
        Write-Host 'Stop that process and try again, or start on another port:' -ForegroundColor Yellow
        Write-Host "  .\scripts\run.ps1 -Port 8001"
        Write-Host ''
        Write-Host 'If this is the Static AstroPhoto backend but its /api/health endpoint is unavailable, use -Restart or -Stop only after confirming it is safe to stop.' -ForegroundColor DarkGray
        Write-Host ''
        exit 1
    }
}

if ($Stop) {
    Write-Host "[run] No backend is running on port $Port (nothing to stop)."
    exit 0
}

Write-Host "[run] Starting Static AstroPhoto backend from $BackendDir" -ForegroundColor Cyan
Write-Urls -ListenPort $Port

Push-Location $BackendDir
try {
    & $VenvPython -m uvicorn app.main:app --host $BindHost --port $Port
    if ($LASTEXITCODE -ne 0) {
        Write-Host "[run][ERROR] Uvicorn exited with code $LASTEXITCODE." -ForegroundColor Red
        exit $LASTEXITCODE
    }
} finally {
    Pop-Location
}
