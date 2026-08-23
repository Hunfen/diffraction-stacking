#!/usr/bin/env bash
#
# Static AstroPhoto — one-command installer (macOS / Linux / WSL)
#
# Idempotent: it is safe to run this script repeatedly. An existing backend/.venv
# is reused as-is (no dependency reinstall); npm install and npm run build are
# re-run because they are quick no-ops when the tree is already up to date.
#
# Exit code is 0 on success and non-zero on any failure.

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKEND_DIR="$ROOT_DIR/backend"
FRONTEND_DIR="$ROOT_DIR/frontend"
VENV_DIR="$BACKEND_DIR/.venv"
VENV_PYTHON="$VENV_DIR/bin/python"
VENV_MARKER="$VENV_DIR/.deps-installed"

info()  { printf '\033[1;34m[install]\033[0m %s\n' "$*"; }
ok()    { printf '\033[1;32m[install]\033[0m %s\n' "$*"; }
warn()  { printf '\033[1;33m[install]\033[0m %s\n' "$*" >&2; }
fail()  { printf '\033[1;31m[install][ERROR]\033[0m %s\n' "$*" >&2; exit 1; }

trap 'fail "Installation failed (line ${LINENO:-?}): ${BASH_COMMAND:-unknown}"' ERR

# ---------------------------------------------------------------- prerequisites
info "Static AstroPhoto one-command installer"
info "Project root: $ROOT_DIR"

PYTHON_BIN=""
PYTHON_VERSION=""

# ------------------------------------------------------------------ Python venv
# Idempotency: a virtualenv is reused only when it has both a usable Python and
# the .deps-installed marker written after a successful requirements install.
# This prevents a failed first install from being skipped on the next run.
if [[ -x "$VENV_PYTHON" ]] && [[ -f "$VENV_MARKER" ]] && \
   "$VENV_PYTHON" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' >/dev/null 2>&1; then
  info "Reusing existing virtualenv: $VENV_DIR"
else
  if [[ -e "$VENV_DIR" ]]; then
    warn "Existing $VENV_DIR is broken, incomplete, or missing the dependency marker; recreating it."
    rm -rf "$VENV_DIR"
  fi

  info "Looking for a Python 3.10+ interpreter that can create a virtualenv"
  info "(preference order: python3.12, python3.11, python3.10, python3, python)"
  for candidate in python3.12 python3.11 python3.10 python3 python; do
    if ! command -v "$candidate" >/dev/null 2>&1; then
      continue
    fi
    if ! "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' >/dev/null 2>&1; then
      warn "Skipping $candidate: Python version < 3.10."
      continue
    fi
    candidate_version="$("$candidate" -c 'import platform; print(platform.python_version())')"
    info "Trying $candidate ($candidate_version)"
    # Actually creating the venv is the definitive test: uv-managed Python with
    # PEP 668 active (or distros without python3-venv) fails here.
    if "$candidate" -m venv "$VENV_DIR"; then
      PYTHON_BIN="$candidate"
      PYTHON_VERSION="$candidate_version"
      break
    fi
    warn "Skipping $candidate: venv creation failed (ensurepip blocked / venv module unavailable)."
    rm -rf "$VENV_DIR"
  done

  if [[ -z "$PYTHON_BIN" ]]; then
    cat >&2 <<'EOF'
[install][ERROR] No usable Python 3.10+ interpreter found. Tried python3.12,
python3.11, python3.10, python3, python — none could create a virtualenv.

Installation hints:
  macOS (Homebrew):  brew install python@3.12
  Debian / Ubuntu:   sudo apt update && sudo apt install python3.12 python3.12-venv
  Fedora / RHEL:     sudo dnf install python3.12
  uv-managed Python: use `uv venv` or install a standalone CPython instead.
EOF
    exit 1
  fi

  info "Using Python $PYTHON_BIN ($PYTHON_VERSION)"
  info "Upgrading pip in the virtualenv"
  if ! "$VENV_PYTHON" -m pip install --upgrade pip; then
    fail "Failed to upgrade pip in the virtualenv (see pip output above)."
  fi

  info "Installing backend dependencies from backend/requirements.txt"
  if ! "$VENV_PYTHON" -m pip install -r "$BACKEND_DIR/requirements.txt"; then
    fail "Failed to install backend/requirements.txt (see pip output above)."
  fi
  : > "$VENV_MARKER"
fi

# ------------------------------------------------------------------ Node / npm
if ! command -v node >/dev/null 2>&1 || ! command -v npm >/dev/null 2>&1; then
  cat >&2 <<'EOF'
[install][ERROR] Node.js and npm are required but not found on PATH.

Installation hints:
  macOS (Homebrew):  brew install node@22
  Debian / Ubuntu:   curl -fsSL https://deb.nodesource.com/setup_22.x | sudo -E bash -
                     sudo apt install -y nodejs
  Node Version Manager: https://github.com/nvm-sh/nvm
EOF
  exit 1
fi

NODE_MAJOR="$(node -p 'parseInt(process.versions.node, 10)' 2>/dev/null || echo 0)"
if [[ "$NODE_MAJOR" -lt 20 ]]; then
  cat >&2 <<'EOF'
[install][ERROR] Node.js 20 or newer is required (22+ recommended). Your version
is too old. Please upgrade Node.js first:
  macOS (Homebrew):  brew upgrade node
  Debian / Ubuntu:   use NodeSource 22.x (see https://github.com/nodesource/distributions)
EOF
  exit 1
fi
info "Using Node.js $(node --version) / npm $(npm --version)"

# ------------------------------------------------------------------ frontend
info "Running npm install in frontend/"
NPM_LOG="$(mktemp "${TMPDIR:-/tmp}/static-astrophoto-npm.XXXXXX")"
if ! (cd "$FRONTEND_DIR" && npm install) >"$NPM_LOG" 2>&1; then
  cat >&2 <<'EOF'
[install][ERROR] npm install failed. The npm log is shown below.
EOF
  sed -n '1,200p' "$NPM_LOG" >&2
  if grep -Eiq 'cache|EACCES|ENOTEMPTY|ECONNRESET|cb\(\) never called' "$NPM_LOG"; then
    cat >&2 <<'EOF'
This looks like an npm cache or permission problem. Fixes to try, then re-run
./scripts/install.sh:

  1. npm cache clean --force
  2. rm -rf frontend/.npm-cache        # project-local cache used by frontend/.npmrc
  3. sudo chown -R "$(id -un)" "$HOME/.npm"   # permission issues under ~/.npm
EOF
  else
    cat >&2 <<'EOF'
Check the network / registry connectivity and the log above, then re-run
./scripts/install.sh.
EOF
  fi
  rm -f "$NPM_LOG"
  exit 1
fi
rm -f "$NPM_LOG"
ok "npm install completed"

info "Running npm run build in frontend/"
if ! (cd "$FRONTEND_DIR" && npm run build); then
  fail "npm run build failed (frontend build error, see output above)."
fi
ok "Frontend production build written to frontend/dist/"

# ------------------------------------------------------------------ data dirs
info "Creating data directories"
mkdir -p "$ROOT_DIR/data/snapshots" "$ROOT_DIR/data/captures"
ok "data/snapshots and data/captures are ready"

# ------------------------------------------------------------------ summary
cat <<'EOF'

[install] Static AstroPhoto installation complete.

Start the server (Mock camera mode):

  cd backend
  .venv/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port 8000

Then open http://localhost:8000 in your browser.

Windows real-camera setup (DMK 33GX249e + ic4): run scripts/install.ps1
(-WithCamera for the ic4 Python bindings) on Windows; see backend/README.md.
EOF
