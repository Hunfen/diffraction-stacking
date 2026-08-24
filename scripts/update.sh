#!/usr/bin/env bash
#
# Static AstroPhoto — updater (macOS / Linux / WSL)
#
# Pulls the latest main branch from GitHub using a safe fast-forward, refuses
# to run when the working tree has uncommitted changes, refreshes backend
# Python dependencies, and rebuilds the frontend.
#
# Usage:
#   ./scripts/update.sh
#   ./scripts/update.sh --with-camera     # also ensure imagingcontrol4
#
# Exit code is 0 on success and non-zero on any failure.

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKEND_DIR="$ROOT_DIR/backend"
FRONTEND_DIR="$ROOT_DIR/frontend"
VENV_DIR="$BACKEND_DIR/.venv"
VENV_PYTHON="$VENV_DIR/bin/python"

WITH_CAMERA=0
if [[ "${1:-}" == "-WithCamera" || "${1:-}" == "--with-camera" ]]; then
  WITH_CAMERA=1
elif [[ -n "${1:-}" ]]; then
  echo "[update][ERROR] Unknown argument: $1" >&2
  echo "Usage: ./scripts/update.sh [--with-camera]" >&2
  exit 1
fi

info()  { printf '\033[1;34m[update]\033[0m %s\n' "$*"; }
ok()    { printf '\033[1;32m[update]\033[0m %s\n' "$*"; }
warn()  { printf '\033[1;33m[update]\033[0m %s\n' "$*" >&2; }
fail()  { printf '\033[1;31m[update][ERROR]\033[0m %s\n' "$*" >&2; exit 1; }

trap 'fail "Update failed (line ${LINENO:-?}): ${BASH_COMMAND:-unknown}"' ERR

info "Static AstroPhoto updater"
info "Project root: $ROOT_DIR"

# ------------------------------------------------------------------ git pull
if ! command -v git >/dev/null 2>&1; then
  cat >&2 <<'EOF'
[update][ERROR] git is required but was not found on PATH.

Installation hints:
  macOS (Homebrew):      brew install git
  Debian / Ubuntu:       sudo apt update && sudo apt install git
  Fedora / RHEL:         sudo dnf install git
EOF
  exit 1
fi

cd "$ROOT_DIR"

if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  fail "This directory is not a Git working tree."
fi

info "Checking for local changes before pulling"
if [[ -n "$(git status --porcelain --untracked-files=all)" ]]; then
  cat >&2 <<'EOF'
[update][ERROR] The working tree has local changes. Refusing to pull because
git fetch/pull could overwrite them.

Please commit or stash your changes first, then re-run:
  ./scripts/update.sh
EOF
  exit 1
fi

info "Pulling latest main: git pull --ff-only origin main"
if ! git pull --ff-only origin main; then
  fail "git pull --ff-only origin main failed (see git output above)."
fi
ok "Pulled the latest main branch"

# ------------------------------------------------------------------ backend
if [[ -x "$VENV_PYTHON" ]]; then
  info "Refreshing backend dependencies in backend/.venv"
  if ! "$VENV_PYTHON" -m pip install -r "$BACKEND_DIR/requirements.txt"; then
    fail "Failed to install backend/requirements.txt (see pip output above)."
  fi
  ok "Backend dependencies are up to date"

  if [[ "$WITH_CAMERA" -eq 1 ]]; then
    info "WithCamera: ensuring imagingcontrol4 is installed"
    if "$VENV_PYTHON" -m pip show imagingcontrol4 >/dev/null 2>&1; then
      info "imagingcontrol4 already installed"
    else
      if ! "$VENV_PYTHON" -m pip install imagingcontrol4; then
        fail "Failed to install imagingcontrol4 (see pip output above)."
      fi
      ok "imagingcontrol4 installed (real-camera mode)"
    fi
  fi
else
  cat >&2 <<'EOF'
[update][ERROR] backend/.venv was not found, so backend dependencies cannot
be refreshed.

Run scripts/install.sh first:
  ./scripts/install.sh
EOF
  exit 1
fi

# ------------------------------------------------------------------ Node / npm
if ! command -v node >/dev/null 2>&1 || ! command -v npm >/dev/null 2>&1; then
  cat >&2 <<'EOF'
[update][ERROR] Node.js and npm are required but were not found on PATH.

Installation hints:
  macOS (Homebrew):  brew install node@22
  Debian / Ubuntu:   curl -fsSL https://deb.nodesource.com/setup_22.x | sudo -E bash -
                     sudo apt install -y nodejs
EOF
  exit 1
fi

NODE_MAJOR="$(node -p 'parseInt(process.versions.node, 10)' 2>/dev/null || echo 0)"
if [[ "$NODE_MAJOR" -lt 20 ]]; then
  cat >&2 <<'EOF'
[update][ERROR] Node.js 20 or newer is required (22+ recommended). Your version
is too old. Please upgrade Node.js first:
  macOS (Homebrew):  brew upgrade node
  Debian / Ubuntu:   use NodeSource 22.x (see https://github.com/nodesource/distributions)
EOF
  exit 1
fi
info "Using Node.js $(node --version) / npm $(npm --version)"

# ------------------------------------------------------------------ frontend
info "Running npm install in frontend/"
if ! (cd "$FRONTEND_DIR" && npm install); then
  fail "npm install failed (see npm output above)."
fi
ok "npm install completed"

info "Running npm run build in frontend/"
if ! (cd "$FRONTEND_DIR" && npm run build); then
  fail "npm run build failed (frontend build error, see output above)."
fi
ok "Frontend production build written to frontend/dist/"

# ------------------------------------------------------------------ summary
cat <<'EOF'

[update] Static AstroPhoto update complete.

Start the server from the repository root:

  ./scripts/run.sh

or manually:

  cd backend
  .venv/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port 8000

Then open http://localhost:8000 in your browser.
EOF
