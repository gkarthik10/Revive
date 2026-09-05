#!/usr/bin/env bash
# Starts the Revive backend (FastAPI, port 8000) and frontend (Vite, port
# 5173) together, so the live demo is one command instead of two terminals.
# Ctrl+C stops both.

set -e

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "==> Starting backend (FastAPI on :8000)"
cd "$ROOT_DIR/backend"

if [ ! -d ".venv" ]; then
  echo "    No .venv found — installing dependencies with the system pip."
  echo "    (Create a virtualenv first if you'd rather not do that.)"
  pip install -r requirements.txt -q
fi

export PYTHONPATH=.
uvicorn app.dashboard_api.api:app --port 8000 &
BACKEND_PID=$!

cleanup() {
  echo
  echo "==> Stopping backend (pid $BACKEND_PID)"
  kill "$BACKEND_PID" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

echo "    Waiting for backend to come up..."
for _ in $(seq 1 20); do
  if curl -sf http://127.0.0.1:8000/api/health > /dev/null 2>&1; then
    echo "    Backend is up."
    break
  fi
  sleep 0.5
done

echo "==> Starting frontend (Vite on :5173)"
cd "$ROOT_DIR/frontend"

if [ ! -d "node_modules" ]; then
  echo "    Installing frontend dependencies..."
  npm install
fi

npm run dev
