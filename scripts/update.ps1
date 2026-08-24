<#
.SYNOPSIS
  Static AstroPhoto updater for Windows (PowerShell).

.DESCRIPTION
  Pulls the latest main branch from GitHub using a safe fast-forward, refuses to
  run when the working tree has uncommitted changes, refreshes backend Python
  dependencies, and rebuilds the frontend. It is intended to be run after
  scripts\install.ps1 has created backend\.venv.

.PARAMETER WithCamera
  Additionally install imagingcontrol4 into backend\.venv for real-camera model.

.EXAMPLE
  .\scripts\update.ps1
  .\scripts\update.ps1 -WithCamera
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
    Write-Host "[update] $Message" -ForegroundColor Cyan
}

function Write-Ok {
    param([string]$Message)
    Write-Host "[update] $Message" -ForegroundColor Green
}

function Write-WarnMsg {
    param([string]$Message)
    Write-Host "[update] $Message" -ForegroundColor Yellow
}

function Write-Fail {
    param([string]$Message)
    Write-Host "[update][ERROR] $Message" -ForegroundColor Red
    exit 1
}

function Test-ExternalExit {
    param([string]$Step)
    if ($LASTEXITCODE -ne 0) {
        Write-Fail "$Step failed (exit code $LASTEXITCODE)."
    }
}

Write-Step "Static AstroPhoto updater"
Write-Step "Project root: $RootDir"

# ------------------------------------------------------------------ git pull
if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    Write-Fail @'
git is required but was not found on PATH.
Install Git for Windows (https://git-scm.com/download/win) or run:
  winget install Git.Git
then open a new terminal and re-run this script.
'@
}

Push-Location $RootDir
try {
    Write-Step "Checking for local changes before pulling"
    $statusOutput = & git status --porcelain --untracked-files=all -- . ':(exclude)frontend/dist' 2>$null
    if ($LASTEXITCODE -ne 0) {
        Write-Fail "git status failed (is this a Git repository?)."
    }
    if ($statusOutput) {
        Write-Fail @'
The working tree has local changes outside frontend\dist. Refusing to pull because git fetch/pull
could overwrite them.

Please commit or stash your changes first, then re-run:
  .\scripts\update.ps1
'@
    }

    Write-Step "Discarding local build output in frontend\dist (it is rebuilt below)"
    & git checkout -- frontend/dist
    Test-ExternalExit -Step "git checkout frontend\dist"

    & git clean -fdq -- frontend/dist
    if ($LASTEXITCODE -ne 0) {
        Write-Fail "git clean frontend\dist failed (exit code $LASTEXITCODE)."
    }
    Write-Ok "Local build output cleared"

    Write-Step "Pulling latest main: git pull --ff-only origin main"
    & git pull --ff-only origin main
    Test-ExternalExit -Step "git pull --ff-only origin main"
    Write-Ok "Pulled the latest main branch"
} finally {
    Pop-Location
}

# ------------------------------------------------------------------ backend
if (Test-Path $VenvPython) {
    Write-Step "Refreshing backend dependencies in backend\.venv"
    & $VenvPython -m pip install -r (Join-Path $BackendDir 'requirements.txt')
    Test-ExternalExit -Step "Installing backend requirements"
    Write-Ok "Backend dependencies are up to date"

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
} else {
    Write-Fail @'
backend\.venv was not found, so backend dependencies cannot be refreshed.

Run scripts\install.ps1 first (or scripts\update.ps1 after install):
  .\scripts\install.ps1
'@
}

# ------------------------------------------------------------------ Node / npm
try {
    $null = Get-Command node -ErrorAction Stop
    $null = Get-Command npm  -ErrorAction Stop
} catch {
    Write-Fail @'
Node.js and npm are required but were not found on PATH.
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
Push-Location $FrontendDir
try {
    Write-Step "Running npm install in frontend\"
    & npm install
    Test-ExternalExit -Step "npm install"
    Write-Ok "npm install completed"

    Write-Step "Running npm run build in frontend\"
    & npm run build
    Test-ExternalExit -Step "npm run build"
    Write-Ok "Frontend production build written to frontend\dist\"
} finally {
    Pop-Location
}

# ------------------------------------------------------------------ summary
Write-Host ''
Write-Host '[update] Static AstroPhoto update complete.' -ForegroundColor Green
Write-Host ''
Write-Host 'Start the server from the repository root:'
Write-Host '  .\scripts\run.ps1'
Write-Host 'or manually:'
Write-Host '  cd backend'
Write-Host '  .venv\Scripts\python -m uvicorn app.main:app --host 0.0.0.0 --port 8000'
Write-Host ''
