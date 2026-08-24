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

# Optional first argument: --stop stops a confirmed backend, --restart stops it
# and then starts a fresh one. With no argument the script keeps the existing
# reuse/stale-detection behavior.
ACTION="${1:-}"
if [[ -n "$ACTION" ]]; then
  if [[ "$ACTION" != "--stop" && "$ACTION" != "--restart" ]]; then
    printf '[run][ERROR] Unknown argument: %s\n' "$ACTION" >&2
    printf 'Usage: %s [--stop|--restart]\n' "$0" >&2
    exit 2
  fi
  if [[ $# -gt 1 ]]; then
    printf '[run][ERROR] Unexpected extra arguments after %s.\n' "$ACTION" >&2
    printf 'Usage: %s [--stop|--restart]\n' "$0" >&2
    exit 2
  fi
fi

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

# Return the PIDs currently listening on $PORT, one per line. Uses lsof first
# and falls back to fuser. Empty output when the port is free or when neither
# tool is available.
find_listening_pids() {
  if command -v lsof >/dev/null 2>&1; then
    lsof -nP -iTCP:"$PORT" -sTCP:LISTEN -t 2>/dev/null | sort -u || true
  elif command -v fuser >/dev/null 2>&1; then
    fuser -n tcp "$PORT" 2>/dev/null | tr '[:space:]' '\n' | grep -E '^[0-9]+$' | sort -u || true
  fi
}

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
  pids="$(find_listening_pids)"
  pid_list="${pids//$'\n'/,}"

  if [[ "$ACTION" == "--stop" || "$ACTION" == "--restart" ]]; then
    if [[ -z "$pids" ]]; then
      printf '[run][ERROR] Could not determine the PID(s) listening on port %s; refusing to stop.\n' "$PORT" >&2
      exit 1
    fi

    for pid in $pids; do
      kill "$pid" 2>/dev/null || true
    done

    port_free=0
    for _ in {1..10}; do
      sleep 0.5
      if [[ -z "$(find_listening_pids)" ]]; then
        port_free=1
        break
      fi
    done

    if [[ "$port_free" -eq 0 ]]; then
      printf '[run][ERROR] Port %s is still in use after stopping PID(s): %s.\n' "$PORT" "$pid_list" >&2
      exit 1
    fi

    if [[ "$ACTION" == "--stop" ]]; then
      printf '[run] Stopped backend on port %s (PID %s)\n' "$PORT" "$pid_list"
      exit 0
    fi

    printf '[run] Stopped old backend (PID %s), restarting…\n' "$pid_list"
    # Fall through to the normal startup code below.
  elif [[ -n "$repo_version" && -n "$backend_version" && "$backend_version" != "$repo_version" ]]; then
    printf '\033[1;33m[run]\033[0m A backend is already running on port %s, but its version is %s while the repository version is %s.\n' "$PORT" "$backend_version" "$repo_version"
    printf '\033[1;33m[run]\033[0m It is still serving the old code. Run with --restart to stop it and start this version, or --stop to stop it without starting.\n'
    print_urls "$PORT"
    exit 1
  else
    printf '\033[1;32m[run]\033[0m Backend is already running on port %s (version %s, camera_driver=%s).\n' "$PORT" "$backend_version" "$backend_camera"
    print_urls "$PORT"
    exit 0
  fi
else
  # Port may be occupied by something that is not this app. Never kill it: the
  # health check did not confirm ownership.
  pids="$(find_listening_pids)"
  if [[ -n "$pids" ]]; then
    pid_list="${pids//$'\n'/,}"
    printf '\033[1;31m[run]\033[0m Port %s is already in use by another process (PID: %s).\n' "$PORT" "$pid_list" >&2
    printf 'Stop that process and try again, or start on another port:\n' >&2
    printf '  PORT=%s %s\n' "$((PORT + 1))" "$0" >&2
    if [[ "$ACTION" == "--stop" || "$ACTION" == "--restart" ]]; then
      printf 'Refusing to stop it because /api/health did not confirm it is this backend.\n' >&2
    else
      printf 'If this is the Static AstroPhoto backend but its /api/health endpoint is unavailable, use --stop or --restart only after confirming it is safe.\n' >&2
    fi
    exit 1
  fi
fi

if [[ "$ACTION" == "--stop" ]]; then
  printf '[run] No backend is running on port %s (nothing to stop).\n' "$PORT"
  exit 0
fi

printf '\033[1;34m[run]\033[0m Starting Static AstroPhoto backend from %s\n' "$BACKEND_DIR"
print_urls "$PORT"

cd "$BACKEND_DIR"

# Replace the shell with Uvicorn so Ctrl+C and the process exit code behave
# naturally.
exec "$VENV_PYTHON" -m uvicorn app.main:app --host "$BIND_HOST" --port "$PORT"
