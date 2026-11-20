#!/usr/bin/env bash
# Start the CricketApp API locally with auto-reload.
#
# The app package lives in app/, so the canonical import target is app.main:app.
# A thin root main.py also exists so "uvicorn main:app" works, but Docker runs
# app.main:app. The script also cd's to the repository root because
# ./cricket.db and ./uploads are resolved relative to the working directory.
#
# Usage:
#   ./run.sh                 # port 8000, reload on
#   PORT=8001 ./run.sh       # different port
#   NO_RELOAD=1 ./run.sh     # no file watcher
#   ./run.sh --log-level debug   # extra args are passed to uvicorn
set -euo pipefail

cd "$(dirname "$0")"

PORT="${PORT:-8000}"
HOST_ADDR="${HOST_ADDR:-127.0.0.1}"

if [[ -n "${PYTHON:-}" ]]; then
    python_bin="$PYTHON"
elif [[ -x "venv/bin/python" ]]; then
    python_bin="venv/bin/python"
elif [[ -x ".venv/bin/python" ]]; then
    python_bin=".venv/bin/python"
else
    python_bin="python3"
fi

if ! "$python_bin" -c "import uvicorn" >/dev/null 2>&1; then
    echo "uvicorn is not installed for '$python_bin'." >&2
    echo "Run: $python_bin -m pip install -r requirements.txt" >&2
    exit 1
fi

if [[ ! -f ".env" ]]; then
    echo "warning: .env not found; copy .env.example to .env first (defaults point at SQLite)." >&2
fi

args=(-m uvicorn app.main:app --host "$HOST_ADDR" --port "$PORT")
if [[ "${NO_RELOAD:-0}" != "1" ]]; then
    args+=(--reload)
fi

echo
echo "Starting CricketApp"
echo "  API:    http://${HOST_ADDR}:${PORT}"
echo "  Docs:   http://${HOST_ADDR}:${PORT}/docs"
echo "  Health: http://${HOST_ADDR}:${PORT}/health"
echo "Press Ctrl+C to stop."
echo

exec "$python_bin" "${args[@]}" "$@"
