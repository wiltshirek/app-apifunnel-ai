# Handoff: api-apifunnel-ai — Deployment Runbook

You are the local assistant for this monorepo. This document is your complete runbook. Execute it top-to-bottom. You have shell access, `gh` CLI, and the `.env` file already has real credentials.

**GitHub repo:** `wiltshirek/app-apifunnel-ai`

---

## Architecture Overview

```
                        ┌─────────────────────────┐
                        │   api.apifunnel.ai      │
                        │   (Caddy reverse proxy)  │
                        └────────┬────────────────┘
                                 │
              ┌──────────────────┼──────────────────┐
              │                  │                   │
   /v1/*  /health    /internal/assets/*      /graphiti/*
   (catch-all)       /api/v1/assets/*        (optional)
              │                  │                   │
              ▼                  ▼                   ▼
   ┌──────────────────┐ ┌──────────────────┐ ┌───────────┐
   │  Orchestration   │ │    Lakehouse     │ │ Graphiti  │
   │  Node.js (Hono)  │ │  Python (FastAPI)│ │ (adapter) │
   │  :3001           │ │  :3002           │ │ :8001     │
   └──────────────────┘ └──────────────────┘ └───────────┘
              │                  │
              ▼                  ▼
   ┌──────────────────────────────────────────┐
   │  MongoDB Atlas (same cluster)            │
   │  ├── apifunnel (orchestration tables)    │
   │  └── mcp_code_execution_server (assets)  │
   └──────────────────────────────────────────┘
```

### Routing table

| Path pattern             | Routed to    | Port |
|--------------------------|------------- |------|
| `/internal/assets/*`     | Lakehouse    | 3002 |
| `/api/v1/assets/*`       | Lakehouse    | 3002 |
| `/v1/*`                  | Orchestration| 3001 |
| `/graphiti/*`            | Graphiti     | 8001 |
| `/health`                | Orchestration| 3001 |
| `/health/lakehouse`      | Lakehouse    | 3002 |
| Everything else          | Orchestration| 3001 |

---

## Phase 1: Local Testing

The `.env` at the repo root already has real credentials. Verify both services start and respond.

### 1a. Install dependencies

```bash
# Orchestration
cd services/orchestration && npm install && cd ../..

# Lakehouse (venv)
cd services/lakehouse
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
cd ../..
```

### 1b. Start services

```bash
./scripts/dev.sh
```

This starts:
- Orchestration → `http://localhost:3001`
- Lakehouse → `http://localhost:3002`

### 1c. Verify health

```bash
curl -sf http://localhost:3001/health && echo " ✅ Orchestration OK"
curl -sf http://localhost:3002/health && echo " ✅ Lakehouse OK"
```

### 1d. Test lakehouse endpoints

```bash
# List assets (needs a JWT — craft a minimal one or use the admin key)
curl -s http://localhost:3002/api/v1/assets \
  -H "Authorization: Bearer $(cat .env | grep MCP_ADMIN_KEY | cut -d= -f2-)" \
  | head -c 200

# Internal search (admin key + user token pattern)
ADMIN_KEY=$(grep MCP_ADMIN_KEY .env | cut -d= -f2-)
curl -s http://localhost:3002/internal/assets/search?q=test \
  -H "Authorization: Bearer $ADMIN_KEY" \
  -H "X-User-Token: eyJhbGciOiJub25lIn0.eyJzdWIiOiJ0ZXN0LXVzZXIiLCJ0ZW5hbnRfaWQiOiJ0ZXN0In0."
```

### 1e. Test orchestration endpoints

```bash
curl -s http://localhost:3001/v1/openapi | head -c 200
```

### Testing checklist

- [ ] `GET /health` on both services returns OK
- [ ] MongoDB connects on startup (check logs for "Connected to MongoDB")
- [ ] Lakehouse: `POST /api/v1/assets/upload` with a file → creates asset
- [ ] Lakehouse: `GET /api/v1/assets` → lists assets
- [ ] Lakehouse: `GET /api/v1/assets/search?q=...` → text search works
- [ ] Lakehouse: `GET /api/v1/assets/{id}` → single asset
- [ ] Lakehouse: `GET /api/v1/assets/{id}/download` → raw bytes
- [ ] Lakehouse: `DELETE /api/v1/assets/{id}` → removes from S3 + MongoDB
- [ ] Lakehouse: Internal routes work with admin key + X-User-Token
- [ ] Orchestration: `/v1/openapi` returns YAML
- [ ] Auth: requests without auth → 401/403

---

## Phase 2: Set GitHub Secrets

Use the `gh` CLI to set all secrets directly. Source the values from the local `.env` file. Run these commands from the repo root:

```bash
cd /path/to/api-apifunnel-ai

# Helper: read a value from .env (handles quoting)
env_val() { grep "^$1=" .env | head -1 | sed "s/^$1=//" | sed 's/^"//;s/"$//' ; }
```

All values come from two files — no placeholders, no manual lookups:
- **This repo's `.env`** → most secrets
- **Bridge `.env`** at `/Users/kenwiltshire/Documents/dev/mcp-code-execution/.env` → Hetzner infra secrets

```bash
REPO="wiltshirek/app-apifunnel-ai"
BRIDGE_ENV="/Users/kenwiltshire/Documents/dev/mcp-code-execution/.env"

# Helper: read a value from this repo's .env
env_val() { grep "^$1=" .env | head -1 | sed "s/^$1=//" | sed 's/^"//;s/"$//' ; }

# Helper: read a value from the bridge .env
bridge_val() { grep "^$1=" "$BRIDGE_ENV" | head -1 | sed "s/^$1=//" | sed 's/^"//;s/"$//' ; }

# ── From this repo's .env ─────────────────────────────────────────────────

# Database
gh secret set MONGODB_URI           --body "$(env_val MONGODB_URI)"           --repo "$REPO"
gh secret set LAKEHOUSE_MONGODB_URI --body "$(env_val LAKEHOUSE_MONGODB_URI)" --repo "$REPO"

# Auth
gh secret set JWT_SECRET   --body "$(env_val JWT_SECRET)"   --repo "$REPO"
gh secret set MCP_ADMIN_KEY --body "$(env_val MCP_ADMIN_KEY)" --repo "$REPO"
gh secret set CRON_SECRET  --body "$(env_val CRON_SECRET)"  --repo "$REPO"

# Firebase
gh secret set FIREBASE_PROJECT_ID   --body "$(env_val FIREBASE_PROJECT_ID)"   --repo "$REPO"
gh secret set FIREBASE_CLIENT_EMAIL --body "$(env_val FIREBASE_CLIENT_EMAIL)" --repo "$REPO"
gh secret set FIREBASE_PRIVATE_KEY  --body "$(env_val FIREBASE_PRIVATE_KEY)"  --repo "$REPO"

# External services
gh secret set APP_BASE_URL         --body "$(env_val APP_BASE_URL)"         --repo "$REPO"
gh secret set GRAPHITI_SERVICE_URL --body "$(env_val GRAPHITI_SERVICE_URL)" --repo "$REPO"

# Hetzner S3
gh secret set HETZNER_S3_ENDPOINT      --body "$(env_val HETZNER_S3_ENDPOINT)"      --repo "$REPO"
gh secret set HETZNER_S3_ACCESS_KEY    --body "$(env_val HETZNER_S3_ACCESS_KEY)"    --repo "$REPO"
gh secret set HETZNER_S3_SECRET        --body "$(env_val HETZNER_S3_SECRET)"        --repo "$REPO"
gh secret set HETZNER_S3_REGION        --body "$(env_val HETZNER_S3_REGION)"        --repo "$REPO"
gh secret set HETZNER_S3_ASSETS_BUCKET --body "$(env_val HETZNER_S3_ASSETS_BUCKET)" --repo "$REPO"

# ── From the bridge .env ──────────────────────────────────────────────────

# Hetzner Cloud API token (used by hcloud to find the server by label)
# In the bridge .env this is called HETZNER_API_KEY — same value, different name
gh secret set HETZNER_API_TOKEN --body "$(bridge_val HETZNER_API_KEY)" --repo "$REPO"

# SSH deploy key (the bridge stores this as WEBHOOK_SSH_PRIVATE_KEY, multiline)
# Extract it properly: everything between the quotes after WEBHOOK_SSH_PRIVATE_KEY=
python3 -c "
import re, pathlib
env = pathlib.Path('$BRIDGE_ENV').read_text()
m = re.search(r'WEBHOOK_SSH_PRIVATE_KEY=\"(-----BEGIN.*?-----END OPENSSH PRIVATE KEY-----)', env, re.DOTALL)
if m: print(m.group(1))
" | gh secret set DEPLOY_SSH_KEY --repo "$REPO"
```

### Verify secrets are set

```bash
gh secret list --repo wiltshirek/app-apifunnel-ai
```

You should see all of: `MONGODB_URI`, `LAKEHOUSE_MONGODB_URI`, `JWT_SECRET`, `MCP_ADMIN_KEY`, `CRON_SECRET`, `FIREBASE_PROJECT_ID`, `FIREBASE_CLIENT_EMAIL`, `FIREBASE_PRIVATE_KEY`, `APP_BASE_URL`, `GRAPHITI_SERVICE_URL`, `HETZNER_S3_ENDPOINT`, `HETZNER_S3_ACCESS_KEY`, `HETZNER_S3_SECRET`, `HETZNER_S3_REGION`, `HETZNER_S3_ASSETS_BUCKET`, `HETZNER_API_TOKEN`, `DEPLOY_SSH_KEY`.

---

## Phase 3: Hetzner Server Provisioning

### 3a. Check if the server exists

```bash
export HCLOUD_TOKEN="<hetzner-api-token>"
hcloud server list -l app=api-platform
```

If a server with label `app=api-platform` already exists and is running, skip to 3c.

If no server exists, either:
- Create one in Hetzner Cloud Console with label `app=api-platform`
- Or use: `hcloud server create --name api-platform --type cx22 --image ubuntu-22.04 --location hel1 --label app=api-platform`

### 3b. Label an existing server (if reusing one)

**IMPORTANT:** Use the `app` label key (not `role`). Other projects use the `role` key
for their own selectors — overwriting it will break their deployments.

```bash
hcloud server add-label <server-name> app=api-platform
```

### 3c. Install prerequisites on the server

SSH into the server and run:

```bash
NODE_IP=$(hcloud server list -l app=api-platform --status running -o noheader -o 'columns=ipv4' | head -1 | tr -d '[:space:]')
ssh root@$NODE_IP
```

Then on the server:

```bash
# Node.js 20
curl -fsSL https://deb.nodesource.com/setup_20.x | bash -
apt-get install -y nodejs

# Python 3.11+
apt-get install -y python3 python3-pip python3-venv libmagic1

# PM2
npm install -g pm2
pm2 startup

# Caddy
apt-get install -y debian-keyring debian-archive-keyring apt-transport-https
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' | tee /etc/apt/sources.list.d/caddy-stable.list
apt-get update
apt-get install caddy

# App directory
mkdir -p /opt/api-apifunnel/services
```

Verify:
```bash
node --version    # v20.x
python3 --version # 3.11+
pm2 --version
caddy version
```

### 3d. DNS

Ensure `api.apifunnel.ai` has an A record pointing to `$NODE_IP`. Caddy handles TLS automatically via Let's Encrypt.

---

## Phase 4: Deploy

### Option A: Push to main (automatic)

```bash
git add -A && git commit -m "Initial monorepo setup" && git push origin main
```

The GitHub Actions workflow triggers automatically.

### Option B: Manual dispatch

Go to Actions tab → "Deploy to Hetzner" → "Run workflow" → select `main`.

### Option C: Via gh CLI

```bash
gh workflow run deploy.yml --repo wiltshirek/app-apifunnel-ai
```

### Monitor the deploy

```bash
gh run list --repo wiltshirek/app-apifunnel-ai --limit 3
gh run watch --repo wiltshirek/app-apifunnel-ai   # live tail
```

---

## Phase 5: Post-Deploy

### 5a. Create MongoDB text index

This is a one-time operation after first deploy. The lakehouse search endpoint requires it.

Connect to MongoDB Atlas (use `mongosh` or the Atlas UI Data Explorer) and run:

```javascript
use mcp_code_execution_server
db.assets.createIndex({ extracted_text: "text" })
```

### 5b. Verify production health

```bash
curl -sf https://api.apifunnel.ai/health && echo " ✅ Orchestration OK"
curl -sf https://api.apifunnel.ai/health/lakehouse && echo " ✅ Lakehouse OK"
```

### 5c. Verify routing

```bash
# Lakehouse routes
curl -s https://api.apifunnel.ai/api/v1/assets -H "Authorization: Bearer test" | head -c 200

# Orchestration routes
curl -s https://api.apifunnel.ai/v1/openapi | head -c 200
```

---

## Repository Structure

```
api-apifunnel-ai/
├── .env                      # Real credentials (gitignored)
├── .env.example              # Template (safe to commit)
├── .github/workflows/
│   └── deploy.yml            # GitHub Actions → Hetzner
├── docker-compose.yml        # Local dev with Docker
├── proxy/
│   ├── Caddyfile             # Production Caddy config
│   └── Caddyfile.dev         # Local dev Caddy config (port 3000)
├── scripts/
│   ├── dev.sh                # Start both services locally
│   └── prod.sh               # PM2 build + start for production
├── services/
│   ├── orchestration/        # Node.js (Hono + Mongoose)
│   │   ├── package.json
│   │   ├── tsconfig.json
│   │   ├── Dockerfile
│   │   ├── openapi/
│   │   └── src/
│   └── lakehouse/            # Python (FastAPI + Motor)
│       ├── pyproject.toml
│       ├── Dockerfile
│       └── src/
│           ├── main.py       # FastAPI app, CORS, lifespan
│           ├── db.py         # Motor MongoDB connection
│           ├── auth.py       # JWT decode, admin key, dual-auth
│           ├── routes/
│           │   ├── internal.py   # /internal/assets/* (MCP_ADMIN_KEY)
│           │   └── external.py   # /api/v1/assets/* (Bearer JWT)
│           ├── services/
│           │   └── assets.py     # Upload, search, thumbnails, PDF extraction
│           └── storage/
│               └── s3.py         # Hetzner S3 client
└── HANDOFF.md                # This file
```

---

## Environment Variables Reference

| Variable                | Used by        | Purpose                              |
|-------------------------|----------------|--------------------------------------|
| `NODE_ENV`              | Both           | `development` or `production`        |
| `MONGODB_URI`           | Orchestration  | → `apifunnel` database               |
| `LAKEHOUSE_MONGODB_URI` | Lakehouse      | → `mcp_code_execution_server` DB     |
| `LAKEHOUSE_DB_NAME`     | Lakehouse      | Explicit DB name (fallback default)  |
| `MCP_ADMIN_KEY`         | Both           | Server-to-server auth                |
| `JWT_SECRET`            | Both           | JWT decode (unsigned trust)          |
| `HETZNER_S3_*`          | Lakehouse      | S3 storage for asset binaries        |
| `FIREBASE_*`            | Orchestration  | Firestore notifications              |
| `PORT`                  | Orchestration  | HTTP port (default 3001)             |
| `APP_BASE_URL`          | Orchestration  | Frontend app URL                     |
| `GRAPHITI_SERVICE_URL`  | Orchestration  | Learning graph service               |
| `CRON_SECRET`           | Orchestration  | Vercel cron auth                     |

---

## Bridge Integration

The MCP bridge (`mcp-code-execution` project) has been updated:

**File:** `.mcp-bridge/mcp-servers.json` → `lakehouse_api` entry
- `base_url`: `http://localhost:3002` (was `http://localhost:8080`)
- `prod_url`: `https://api.apifunnel.ai` (was `https://tool.apifunnel.ai`)

Auth pattern (`"pattern": "lakehouse"`) means the bridge sends:
- `Authorization: Bearer <MCP_ADMIN_KEY>` for server-to-server calls
- `X-User-Token: <user_jwt>` to forward user identity

---

## Troubleshooting

### Lakehouse can't connect to MongoDB
- Check `LAKEHOUSE_MONGODB_URI` is set (falls back to `MONGODB_URI` if not)
- Verify MongoDB Atlas IP access list includes the Hetzner node's IP
- `pm2 logs lakehouse`

### S3 uploads failing
- Verify all `HETZNER_S3_*` env vars are set
- Verify `mcp-lakehouse` bucket exists in Hetzner Object Storage
- Lakehouse gracefully degrades if S3 is unavailable (logs warning)

### Caddy not routing
- `caddy validate --config /etc/caddy/Caddyfile`
- `caddy reload --config /etc/caddy/Caddyfile`
- `journalctl -u caddy -f`

### PM2 commands
```bash
pm2 ls                    # List all services
pm2 logs orchestration    # Orchestration logs
pm2 logs lakehouse        # Lakehouse logs
pm2 restart all           # Restart everything
pm2 monit                 # Real-time monitoring
```
