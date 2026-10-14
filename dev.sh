#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
BACKEND="$ROOT/Backend"
FRONTEND="$ROOT/Frontend"

if [[ ! -f "$BACKEND/.env" ]]; then
  echo "Missing $BACKEND/.env (Tiger, Gemini, and ElevenLabs keys)."
  exit 1
fi

if [[ ! -x "$BACKEND/venv/bin/python" ]]; then
  python3 -m venv "$BACKEND/venv"
fi

"$BACKEND/venv/bin/pip" install -q -r "$BACKEND/requirements.txt"

if [[ ! -d "$FRONTEND/node_modules" ]]; then
  (cd "$FRONTEND" && npm install)
fi

cleanup() {
  kill "${PIDS[@]:-}" 2>/dev/null || true
}
PIDS=()
trap cleanup EXIT INT TERM

"$BACKEND/venv/bin/uvicorn" main:app --reload --host 127.0.0.1 --port 8000 --app-dir "$BACKEND" &
PIDS+=("$!")

if grep -q '^X_COOKIES=.' "$BACKEND/.env"; then
  (cd "$BACKEND" && ./venv/bin/python -u live_ingest.py) &
  PIDS+=("$!")
else
  echo "No X_COOKIES in .env; skipping live Twitter ingest."
fi

(cd "$FRONTEND" && npm run dev -- --host 127.0.0.1 --port 5173) &
PIDS+=("$!")

echo "Backend  http://127.0.0.1:8000/docs"
echo "Frontend http://127.0.0.1:5173"
wait
