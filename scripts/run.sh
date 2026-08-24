#!/usr/bin/env bash
#
# Static AstroPhoto — launcher (macOS / Linux / WSL)
#
# Starts the FastAPI backend with Uvicorn from the project virtualenv.
# backend/.venv must already exist; run scripts/install.sh or scripts/update.sh
# first if it does not.
#
# If the chosen port is already serving this backend, the script detects it,
# prints the access URLs, and exits successfully instead of failing with
# "address already in use".
#
# Exit code is 0 on success and non-zero on any failure.

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKEND_DIR="$ROOT_DIR/backend"
VENV_DIR="$BACKEND_DIR/.venv"
VENV_PYTHON="$VENV_DIR/bin/python"
PORT="${PORT:-8000}"
BIND_HOST="${BIND_HOST:-0.0.0.0}"

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

print_urls() {
  local port="$1"
  printf '\033[1;34m[run]\033[0m Access URLs:\n'
  printf '  http://localhost:%s\n' "$port"

  # Print the machine's non-loopback IPv4 addresses as additional LAN URLs.
  "$VENV_PYTHON" - "$port" <<'PY'
import socket
import sys

port = sys.argv[1]
try:
    infos = socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)
    ips = sorted({info[4][0] for info in infos if not info[4][0].startswith("127.")})
    for ip in ips:
        print(f"  http://{ip}:{port}")
except Exception:
    pass
PY
  printf '\n'
}

# Repository version (from backend/app/__init__.py), used to detect a stale
# backend process that is still serving the previous version of the code.
repo_version="$(sed -n 's/^APP_VERSION = "\(.*\)"/\1/p' "$BACKEND_DIR/app/__init__.py" | head -n 1)"

# If something is already listening on the chosen port, check whether it is
# this backend. If it is healthy, reuse it instead of failing with EADDRINUSE.
health_info="$("$VENV_PYTHON" - "$PORT" <<'PY' 2>/dev/null || true
import json
import sys
import urllib.request

port = sys.argv[1]
try:
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=3) as resp:
        data = json.load(resp)
    if data.get("ok") is True:
        version = data.get("data", {}).get("version", "")
        camera = data.get("data", {}).get("camera_driver", "")
        print(f"{version}|{camera}")
        sys.exit(0)
except Exception:
    pass
sys.exit(1)
PY
)"

if [[ -n "$health_info" ]]; then
  backend_version="${health_info%%|*}"
  backend_camera="${health_info#*|}"

  if [[ -n "$repo_version" && -n "$backend_version" && "$backend_version" != "$repo_version" ]]; then
    printf '\033[1;33m[run]\033[0m A backend is already running on port %s, but its version is %s while the repository version is %s.\n' "$PORT" "$backend_version" "$repo_version"
    printf '\033[1;33m[run]\033[0m It is still serving the old code. Stop it, then run this script again.\n'
    print_urls "$PORT"
    exit 1
  fi

  printf '\033[1;32m[run]\033[0m Backend is already running on port %s (version %s, camera_driver=%s).\n' "$PORT" "$backend_version" "$backend_camera"
  print_urls "$PORT"
  exit 0
fi

printf '\033[1;34m[run]\033[0m Starting Static AstroPhoto backend from %s\n' "$BACKEND_DIR"
print_urls "$PORT"

cd "$BACKEND_DIR"

# Replace the shell with Uvicorn so Ctrl+C and the process exit code behave
# naturally.
exec "$VENV_PYTHON" -m uvicorn app.main:app --host "$BIND_HOST" --port "$PORT"
