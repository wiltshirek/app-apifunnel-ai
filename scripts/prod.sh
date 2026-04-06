#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

export NODE_ENV=production

echo "══════════════════════════════════════════════════════════"
echo "  api.apifunnel.ai — PRODUCTION (PM2)"
echo "══════════════════════════════════════════════════════════"

# ── Build orchestration ───────────────────────────────────────────────────
echo "▶ Building orchestration..."
(cd services/orchestration && npm ci --production=false && npm run build)

# ── Install lakehouse deps ────────────────────────────────────────────────
echo "▶ Installing lakehouse dependencies..."
(cd services/lakehouse && pip install --no-cache-dir -q .)

# ── PM2 start/restart ────────────────────────────────────────────────────
echo "▶ Starting services via PM2..."

pm2 delete orchestration 2>/dev/null || true
pm2 delete lakehouse 2>/dev/null || true

pm2 start services/orchestration/dist/index.js \
    --name orchestration \
    --env-file .env \
    --max-memory-restart 512M

pm2 start "uvicorn src.main:app --host 0.0.0.0 --port 3002 --workers 2" \
    --name lakehouse \
    --cwd services/lakehouse \
    --interpreter none \
    --max-memory-restart 512M

pm2 save

echo ""
echo "✅ Both services running under PM2."
echo ""
pm2 ls
echo ""
echo "  Orchestration  →  http://localhost:3001"
echo "  Lakehouse      →  http://localhost:3002"
echo "  Caddy proxy    →  https://api.apifunnel.ai"
