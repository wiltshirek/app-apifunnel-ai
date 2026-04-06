#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

export NODE_ENV=development

# Load root .env into the shell environment so subprocesses inherit it
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
echo "  Orchestration  →  http://localhost:3001"
echo "  Lakehouse      →  http://localhost:3002"
echo "  Caddy proxy    →  http://localhost:3000  (all paths)"
echo ""

# ── Orchestration (Node) ──────────────────────────────────────────────────
echo "▶ Starting orchestration (Node)..."
(cd services/orchestration && npm run dev) &
ORCH_PID=$!

# ── Lakehouse (Python) ────────────────────────────────────────────────────
echo "▶ Starting lakehouse (Python)..."
(cd services/lakehouse && uvicorn src.main:app --host 0.0.0.0 --port 3002 --reload) &
LAKE_PID=$!

# ── Trap for clean shutdown ───────────────────────────────────────────────
cleanup() {
    echo ""
    echo "Shutting down..."
    kill $ORCH_PID $LAKE_PID 2>/dev/null || true
    wait $ORCH_PID $LAKE_PID 2>/dev/null || true
    echo "Done."
}
trap cleanup EXIT INT TERM

echo ""
echo "Both services running. Press Ctrl+C to stop."
echo ""
wait
