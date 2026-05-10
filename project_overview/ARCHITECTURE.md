# Architecture

## Services

| Service | Stack | Port | Notes |
|---|---|---|---|
| Subagents | Node.js (Hono) | 3001 | Subagent orchestration, notifications, one-shot LLM |
| Lakehouse | Python (FastAPI) | 3002 | |
| PR Bot | Python (FastAPI) | 3003 | |
| Repo Search | Python (FastAPI) | 3004 | |
| Video Edit | Node.js (Hono) | 3005 | Requires `yt-dlp`, `ffmpeg`, `poppler-utils`, `python3` |
| Caddy (proxy) | — | 80/443 | |

## Routing

All traffic enters through Caddy at `api.apifunnel.ai`.

| Path | Routed to | Notes |
|---|---|---|
| `/api/v1/assets/*` | Lakehouse :3002 | |
| `/internal/assets/*` | Lakehouse :3002 | Caddy rewrites to `/api/v1/assets/*` before hitting FastAPI |
| `/api/v1/prbot/*` | PR Bot :3003 | |
| `/api/v1/repo-search/*` | Repo Search :3004 | |
| `/api/v1/video/*` | Video Edit :3005 | Video editing, composition, analysis, transcript |
| `/v1/video/*` | Video Edit :3005 | Same service, alternate path prefix |
| `/v1/*` | Subagents :3001 | Subagent orchestration, notifications, one-shot LLM |
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

## MongoDB Search Indexes

Lakehouse hybrid search requires three indexes on the `assets` collection:

1. **Legacy `$text` index** — created automatically by `db.py` at startup. Used as fallback when Atlas Search / Vector Search indexes are not yet available.

2. **Atlas Vector Search index** (`assets_vector_search`) — stores embeddings for semantic search. Create via Atlas UI or Admin API:

```javascript
// Index type: vectorSearch
{
  "name": "assets_vector_search",
  "type": "vectorSearch",
  "definition": {
    "fields": [
      { "path": "embedding", "type": "vector", "numDimensions": 1536, "similarity": "cosine" },
      { "path": "user_id", "type": "filter" },
      { "path": "tenant_id", "type": "filter" },
      { "path": "content_type", "type": "filter" }
    ]
  }
}
```

3. **Atlas Search index** (`assets_text_search`) — BM25 keyword search. Create via Atlas UI or Admin API:

```javascript
// Index type: search
{
  "name": "assets_text_search",
  "type": "search",
  "definition": {
    "mappings": {
      "dynamic": false,
      "fields": {
        "extracted_text": { "type": "string", "analyzer": "lucene.standard" },
        "filename": { "type": "string", "analyzer": "lucene.standard" },
        "user_id": { "type": "token" },
        "tenant_id": { "type": "token" },
        "content_type": { "type": "token" }
      }
    }
  }
}
```

Both Atlas indexes must be created manually (they cannot be created via `create_index()`). The `$rankFusion` stage that combines them requires MongoDB 8.0+ and is currently a Preview feature. If these indexes are not present, search falls back to the legacy `$text` keyword path automatically.
