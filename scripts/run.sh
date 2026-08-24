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

# If something is already listening on the chosen port, check whether it is
# this backend. If it is healthy, reuse it instead of failing with EADDRINUSE.
if "$VENV_PYTHON" - "$PORT" <<'PY'
import json
import sys
import urllib.request

port = sys.argv[1]
try:
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=3) as resp:
        data = json.load(resp)
    sys.exit(0 if data.get("ok") is True else 1)
except Exception:
    sys.exit(1)
PY
then
  printf '\033[1;32m[run]\033[0m Backend is already running on port %s.\n' "$PORT"
  print_urls "$PORT"
  exit 0
fi

printf '\033[1;34m[run]\033[0m Starting Static AstroPhoto backend from %s\n' "$BACKEND_DIR"
print_urls "$PORT"

cd "$BACKEND_DIR"

# Replace the shell with Uvicorn so Ctrl+C and the process exit code behave
# naturally.
exec "$VENV_PYTHON" -m uvicorn app.main:app --host "$BIND_HOST" --port "$PORT"
