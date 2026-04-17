#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

export NODE_ENV=development

if [ -f .env ]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi

echo "══════════════════════════════════════════════════════════"
echo "  api.apifunnel.ai — LOCAL DEV"
echo "══════════════════════════════════════════════════════════"
echo ""
echo "  Subagents      →  http://localhost:3001"
echo "  Lakehouse      →  http://localhost:3002"
echo "  Caddy proxy    →  http://localhost:3000  (all paths)"
echo ""

echo "▶ Starting subagents (Node)..."
(cd services/subagents && npm run dev) &
SUBAGENTS_PID=$!

echo "▶ Starting lakehouse (Python)..."
(cd services/lakehouse && uvicorn src.main:app --host 0.0.0.0 --port 3002 --reload) &
LAKE_PID=$!

cleanup() {
    echo ""
    echo "Shutting down..."
    kill $SUBAGENTS_PID $LAKE_PID 2>/dev/null || true
    wait $SUBAGENTS_PID $LAKE_PID 2>/dev/null || true
    echo "Done."
}
trap cleanup EXIT INT TERM

echo ""
echo "Both services running. Press Ctrl+C to stop."
echo ""
wait
