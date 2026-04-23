# Architecture

## Services

| Service | Stack | Port |
|---|---|---|
| Subagents | Node.js (Hono) | 3001 |
| Lakehouse | Python (FastAPI) | 3002 |
| PR Bot | Python (FastAPI) | 3003 |
| Repo Search | Python (FastAPI) | 3004 |
| Caddy (proxy) | — | 80/443 |

## Routing

All traffic enters through Caddy at `api.apifunnel.ai`.

| Path | Routed to | Notes |
|---|---|---|
| `/api/v1/assets/*` | Lakehouse :3002 | |
| `/internal/assets/*` | Lakehouse :3002 | Caddy rewrites to `/api/v1/assets/*` before hitting FastAPI |
| `/api/v1/prbot/*` | PR Bot :3003 | |
| `/api/v1/repo-search/*` | Repo Search :3004 | |
| `/v1/*` | Subagents :3001 | |
| `/graphiti/*` | Graphiti :8001 | Strip-prefix rewrite |
| `/health/lakehouse` | Lakehouse :3002 | Rewrites to `/health` |
| Everything else | Subagents :3001 | |

**Gotcha:** `/internal/assets/*` is the MCP server's entry point into the lakehouse. Caddy rewrites the path — FastAPI never sees `/internal/`. If you add new Caddy rules that need path translation, always add a `uri replace` directive.

## Databases

Two separate MongoDB databases on the same Atlas cluster:

| Env var | Database name | Used by |
|---|---|---|
| `MONGODB_URI` | `apifunnel` | Subagents |
| `LAKEHOUSE_MONGODB_URI` | `mcp_code_execution_server` | Lakehouse, PR Bot, Repo Search |

**Gotcha:** The lakehouse DB is named `mcp_code_execution_server` — a legacy name from before the lakehouse was its own service. Don't rename it; data is already there in production.

## Bridge Integration

The MCP bridge (`mcp-code-execution`) calls the lakehouse via the `lakehouse` auth pattern:

```
Authorization: Bearer <MCP_ADMIN_KEY>
X-User-Token: <user_jwt>
```

This is **not** the standard dual-auth pattern used by direct REST callers. The bridge sends the admin key in the `Authorization` header and the user JWT in `X-User-Token` as a passthrough. See `AUTH_IDENTITY_CONTRACT.md` for how `require_identity` resolves both.

Bridge config lives in `mcp-code-execution` at `.mcp-bridge/mcp-servers.json`, entry `lakehouse_api`:
- `base_url`: `http://localhost:3002`
- `prod_url`: `https://api.apifunnel.ai`

## S3 Storage

Lakehouse stores asset binaries in Hetzner Object Storage (S3-compatible). If S3 is unavailable on startup, the service starts anyway and logs a warning — it degrades gracefully. Metadata writes to MongoDB still succeed; only binary retrieval fails.

Required env vars: `HETZNER_S3_ENDPOINT`, `HETZNER_S3_ACCESS_KEY`, `HETZNER_S3_SECRET`, `HETZNER_S3_REGION`, `HETZNER_S3_ASSETS_BUCKET`.

## MongoDB Text Index (one-time, post-deploy)

Lakehouse full-text search requires a text index. Run once after first deploy:

```javascript
use mcp_code_execution_server
db.assets.createIndex({ extracted_text: "text" })
```

Without this, search returns 500.
