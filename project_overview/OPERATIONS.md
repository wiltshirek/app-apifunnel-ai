# Operations

## Deploy

Push to `main` triggers the GitHub Actions deploy workflow automatically.
Manual trigger: `gh workflow run deploy.yml --repo wiltshirek/app-apifunnel-ai`

Monitor: `gh run watch --repo wiltshirek/app-apifunnel-ai`

## Hetzner Server

Server is identified by label `app=api-platform` (not `role` — that key is used by other projects; overwriting it breaks their deployments).

```bash
hcloud server list -l app=api-platform
```

## GitHub Secrets

Secrets come from two `.env` files — not one:

| Secret | Source |
|---|---|
| `MONGODB_URI`, `LAKEHOUSE_MONGODB_URI`, `JWT_SECRET`, `MCP_ADMIN_KEY`, `CRON_SECRET`, `FIREBASE_*`, `HETZNER_S3_*`, `APP_BASE_URL`, `GRAPHITI_SERVICE_URL` | This repo's `.env` |
| `HETZNER_API_TOKEN` | `mcp-code-execution/.env` as `HETZNER_API_KEY` (different name, same value) |
| `DEPLOY_SSH_KEY` | `mcp-code-execution/.env` as `WEBHOOK_SSH_PRIVATE_KEY` |

## Caddy

```bash
caddy validate --config /etc/caddy/Caddyfile
caddy reload --config /etc/caddy/Caddyfile
journalctl -u caddy -f
```

## PM2

```bash
pm2 ls
pm2 logs lakehouse
pm2 logs subagents
pm2 restart all
```

## Test Tokens

To test authenticated endpoints locally, mint a JWT via the bridge:

```
POST /api/v1/internal/mint-token
```

Token must have a `sub` claim matching a user_id with a GitHub OAuth token in the `user_api_tokens` collection. Tokens expire in 30 days.

## PR Bot Gotchas

**Target repo must have this enabled before first dispatch:**
GitHub repo → Settings → Actions → Workflow permissions → "Allow GitHub Actions to create and approve pull requests". Without it, `gh pr create` fails silently inside the workflow.

**First dispatch after a new repo is configured** returns `"status": "repo_just_configured"` — not an error, it's the workflow self-configuring. Wait ~8s and dispatch again; second call returns `"status": "dispatched"` with a `dispatch_id`.

**Polling runs:** `/runs/{id}` accepts both `dispatch_id` (e.g. `dsp_a1b2c3...`) and the GitHub Actions `run_id` — either works.

**Logs:** `?page=-1` returns the last page (most recent output). `?all=true` returns everything. Default page size is 100 lines.

**Credentials** can go in the request body (`github_token`, `api_key`) or in the `X-Dependency-Tokens` header (`github_rest`, `agent_key`) when called through the bridge.
