#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

export NODE_ENV=production

echo "══════════════════════════════════════════════════════════"
echo "  api.apifunnel.ai — PRODUCTION (PM2)"
echo "══════════════════════════════════════════════════════════"

echo "▶ Building subagents..."
(cd services/subagents && npm ci --production=false && npm run build)

echo "▶ Installing lakehouse dependencies..."
(cd services/lakehouse && pip install --no-cache-dir -q .)

echo "▶ Starting services via PM2..."

pm2 delete subagents 2>/dev/null || true
pm2 delete orchestration 2>/dev/null || true  # one-shot cleanup of the old pm2 entry
pm2 delete lakehouse 2>/dev/null || true

set -a && source .env && set +a

pm2 start services/subagents/dist/index.js \
    --name subagents \
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
echo "  Subagents      →  http://localhost:3001"
echo "  Lakehouse      →  http://localhost:3002"
echo "  Caddy proxy    →  https://api.apifunnel.ai"
