#!/usr/bin/env bash
#
# Static AstroPhoto — launcher (macOS / Linux / WSL)
#
# Starts the FastAPI backend with Uvicorn from the project virtualenv.
# backend/.venv must already exist; run scripts/install.sh or scripts/update.sh
# first if it does not.
#
# Exit code is 0 on success and non-zero on any failure.

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKEND_DIR="$ROOT_DIR/backend"
VENV_DIR="$BACKEND_DIR/.venv"
VENV_PYTHON="$VENV_DIR/bin/python"

if [[ ! -x "$VENV_PYTHON" ]]; then
  cat >&2 <<'EOF'
[run][ERROR] backend/.venv was not found or is not usable.

Run one of these first from the repository root:

  ./scripts/install.sh                     # first-time setup
  ./scripts/update.sh                      # update an existing installation

or, if using real-camera mode on Windows, rerun scripts/install.ps1.
EOF
  exit 1
fi

printf '\033[1;34m[run]\033[0m Starting Static AstroPhoto backend from %s\n' "$BACKEND_DIR"

cd "$BACKEND_DIR"

# Replace the shell with Uvicorn so Ctrl+C and the process exit code behave
# naturally.
exec "$VENV_PYTHON" -m uvicorn app.main:app --host 0.0.0.0 --port 8000
