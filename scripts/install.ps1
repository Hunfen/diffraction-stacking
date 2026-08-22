<#
.SYNOPSIS
  Static AstroPhoto one-command installer for Windows (PowerShell).

.DESCRIPTION
  Idempotent installer. Creates backend\.venv (reusing it if present), installs
  backend\requirements.txt, installs and builds the frontend, and creates the
  runtime data directories. Use -WithCamera to additionally install the
  imagingcontrol4 Python bindings for real-camera (DMK 33GX249e) mode.

.PARAMETER WithCamera
  Additionally install imagingcontrol4 into backend\.venv for real-camera mode.

.EXAMPLE
  .\scripts\install.ps1
  .\scripts\install.ps1 -WithCamera
#>
[CmdletBinding()]
param(
    [switch]$WithCamera
)

$ErrorActionPreference = 'Stop'

$RootDir     = Split-Path -Parent $PSScriptRoot
$BackendDir  = Join-Path $RootDir 'backend'
$FrontendDir = Join-Path $RootDir 'frontend'
$VenvDir     = Join-Path $BackendDir '.venv'
$VenvPython  = Join-Path $VenvDir 'Scripts\python.exe'

function Write-Step {
    param([string]$Message)
    Write-Host "[install] $Message" -ForegroundColor Cyan
}

function Write-Ok {
    param([string]$Message)
    Write-Host "[install] $Message" -ForegroundColor Green
}

function Write-WarnMsg {
    param([string]$Message)
    Write-Host "[install] $Message" -ForegroundColor Yellow
}

function Write-Fail {
    param([string]$Message)
    Write-Host "[install][ERROR] $Message" -ForegroundColor Red
    exit 1
}

function Test-ExternalExit {
    param([string]$Step)
    if ($LASTEXITCODE -ne 0) {
        Write-Fail "$Step failed (exit code $LASTEXITCODE)."
    }
}

function Test-Python310 {
    # Returns true when `<launcher> [<arg>] -c ...` runs on Python >= 3.10.
    param([string]$Launcher, [string]$LauncherArg)
    try {
        if ([string]::IsNullOrWhiteSpace($LauncherArg)) {
            $null = & $Launcher -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>$null
        } else {
            $null = & $Launcher $LauncherArg -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>$null
        }
        return ($LASTEXITCODE -eq 0)
    } catch {
        return $false
    }
}

Write-Step "Static AstroPhoto one-command installer"
Write-Step "Project root: $RootDir"

# ------------------------------------------------------------------ Python
# Prefer the Python Launcher's 3.12 runtime (py -3.12), fall back to `python`.
$PyLauncher = $null
$PyArg      = $null

if (Test-Python310 -Launcher 'py' -LauncherArg '-3.12') {
    $PyLauncher = 'py'
    $PyArg      = '-3.12'
} elseif (Test-Python310 -Launcher 'python' -LauncherArg '') {
    $PyLauncher = 'python'
    $PyArg      = $null
}

if ($null -eq $PyLauncher) {
    Write-Fail @'
No Python 3.10+ interpreter found (tried `py -3.12`, then `python`).
Install Python 3.12 from https://www.python.org/downloads/ (check
"Add python.exe to PATH") or run:
  winget install Python.Python.3.12
then re-run this script.
'@
}

$PyVersion = ''
if ($null -eq $PyArg) {
    $PyVersion = (& $PyLauncher -c 'import platform; print(platform.python_version())').Trim()
} else {
    $PyVersion = (& $PyLauncher $PyArg -c 'import platform; print(platform.python_version())').Trim()
}
Write-Step "Using Python $PyLauncher $PyArg ($PyVersion)"

# ------------------------------------------------------------------ venv
if (Test-Path $VenvPython) {
    Write-Step "Reusing existing virtualenv: $VenvDir"
} else {
    Write-Step "Creating virtualenv: $VenvDir"
    if ($null -eq $PyArg) {
        & $PyLauncher -m venv $VenvDir
    } else {
        & $PyLauncher $PyArg -m venv $VenvDir
    }
    Test-ExternalExit -Step "Creating the Python virtualenv"

    Write-Step "Upgrading pip in the virtualenv"
    & $VenvPython -m pip install --upgrade pip
    Test-ExternalExit -Step "Upgrading pip"

    Write-Step "Installing backend dependencies from backend\requirements.txt"
    & $VenvPython -m pip install -r (Join-Path $BackendDir 'requirements.txt')
    Test-ExternalExit -Step "Installing backend requirements"
}

# ------------------------------------------------------------------ real-camera extras
if ($WithCamera) {
    Write-Step "WithCamera: ensuring imagingcontrol4 is installed"
    $null = & $VenvPython -m pip show imagingcontrol4 2>$null
    if ($LASTEXITCODE -ne 0) {
        & $VenvPython -m pip install imagingcontrol4
        Test-ExternalExit -Step "Installing imagingcontrol4"
        Write-Ok "imagingcontrol4 installed (real-camera mode)"
    } else {
        Write-Step "imagingcontrol4 already installed"
    }
}

# ------------------------------------------------------------------ Node / npm
$nodeCmd = $null
$npmCmd  = $null
try {
    $nodeCmd = Get-Command node -ErrorAction Stop
    $npmCmd  = Get-Command npm  -ErrorAction Stop
} catch {
    Write-Fail @'
Node.js and npm are required but not found on PATH.
Install Node.js LTS (22.x) with winget:
  winget install OpenJS.NodeJS.LTS
Open a new terminal after installing, then re-run this script.
'@
}

try {
    $NodeMajor = [int](node -p 'parseInt(process.versions.node, 10)')
} catch {
    $NodeMajor = 0
}
if ($NodeMajor -lt 20) {
    Write-Fail @"
Node.js 20 or newer is required (22+ recommended), found major version $NodeMajor.
Upgrade with:  winget upgrade OpenJS.NodeJS.LTS
"@
}
Write-Step "Using Node.js $(node --version) / npm $(npm --version)"

# ------------------------------------------------------------------ frontend
$NpmLog = Join-Path $env:TEMP ("static-astrophoto-npm-{0}.log" -f ([guid]::NewGuid().ToString('N')))

Push-Location $FrontendDir
try {
    Write-Step "Running npm install in frontend\"
    & npm install *>&1 | Tee-Object -FilePath $NpmLog | ForEach-Object { Write-Host $_ }
    if ($LASTEXITCODE -ne 0) {
        $logText = Get-Content -Path $NpmLog -Raw -ErrorAction SilentlyContinue
        if ($logText -and $logText -match 'cache|EACCES|ENOTEMPTY|ECONNRESET|cb\(\) never called') {
            Write-WarnMsg @'
npm install failed with what looks like an npm cache or permission problem. Try:

  1. npm cache clean --force
  2. Remove-Item -Recurse -Force frontend\.npm-cache
  3. Re-run .\scripts\install.ps1
'@
        }
        Write-Fail "npm install failed (see $NpmLog)."
    }
    Write-Ok "npm install completed"

    Write-Step "Running npm run build in frontend\"
    & npm run build
    Test-ExternalExit -Step "npm run build"
    Write-Ok "Frontend production build written to frontend\dist\"
} finally {
    Pop-Location
}

# ------------------------------------------------------------------ data dirs
Write-Step "Creating data directories"
$null = New-Item -ItemType Directory -Force -Path (Join-Path $RootDir 'data\snapshots')
$null = New-Item -ItemType Directory -Force -Path (Join-Path $RootDir 'data\captures')
Write-Ok "data\snapshots and data\captures are ready"

# ------------------------------------------------------------------ summary
Write-Host ''
Write-Ok "Static AstroPhoto installation complete."
Write-Host ''
Write-Host 'Start the server from the repository root:'
Write-Host '  cd backend'
Write-Host '  .venv\Scripts\python -m uvicorn app.main:app --host 0.0.0.0 --port 8000'
Write-Host ''
Write-Host 'Then open http://localhost:8000 in your browser.'
Write-Host ''

if ($WithCamera) {
    Write-Host 'Real-camera (DMK 33GX249e) prerequisites:'
    Write-Host '  1. Install the Imaging Source ic4 driver and bind the GigE network adapter.'
    Write-Host '  2. Enable Jumbo Frames (9000 MTU) on the GigE adapter where possible.'
    Write-Host '  See backend\README.md for full real-camera setup details.'
} else {
    Write-Host 'Mock camera mode is used by default.'
    Write-Host 'For the real DMK 33GX249e: re-run  .\scripts\install.ps1 -WithCamera'
    Write-Host 'then install the ic4 driver and enable Jumbo Frames (see backend\README.md).'
}
