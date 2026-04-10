# Repo Search Service

## What This Is

A standalone REST API service on `api.apifunnel.ai` that provides semantic search over GitHub repository content. It indexes the human-readable parts of a repo (comments, docstrings, READMEs, file paths) into vectors and lets callers search by concept rather than exact text.

This is an independent third-party API server — like the GitHub REST API, Google Sheets API, or any other external service. It has its own database, its own storage, its own credentials. The MCP bridge team registers it like any other REST API they consume.

## Design Principles

- **Admin-key-protected endpoints.** Same pattern as lakehouse — `Authorization: Bearer <REPO_SEARCH_ADMIN_KEY>`. Private endpoints.
- **GitHub token via dependency forwarding.** The bridge passes the user's GitHub OAuth token in the `X-Dependency-Tokens` header. We use it for GitHub API calls. We never touch OAuth flows, token refresh, or token storage.
- **Light indexing.** Index comments, docstrings, READMEs, file paths — not raw code. Code is already searchable via GitHub's code search.
- **Flat vector store.** No hierarchy in vectors. GitHub's tree endpoint already gives hierarchy. A semantic hit returns a file path; the caller navigates from there.
- **Per-repo, on-demand.** Indexes are built per repo when first queried. Not a global index.
- **Fully owned infrastructure.** Own database, own S3 bucket, own credentials. No shared naming or credentials with the MCP team or any other service.

## Auth & Headers

Every request from the bridge carries three headers:

```
Authorization: Bearer <REPO_SEARCH_ADMIN_KEY>
X-User-Token: <user JWT>
X-Dependency-Tokens: {"github_rest": "gho_abc123..."}
```

### 1. Endpoint Protection — `Authorization`

Same pattern as lakehouse. Admin key gates access to the API. Missing or invalid → `401`.

### 2. User Identity — `X-User-Token`

JWT with user identity claims (user_id, tenant_id, etc.). Decoded without signature verification (same as lakehouse). Used to scope data per user in MongoDB.

### 3. GitHub Token — `X-Dependency-Tokens`

JSON object in the header. Key `github_rest` contains the user's live GitHub OAuth token. We parse it and use it for all GitHub API calls (tree fetch, content fetch, compare).

**What we do:**
- Parse `X-Dependency-Tokens` as JSON
- Extract `github_rest` — that's the GitHub token
- Use it for all GitHub API calls

**Error responses:**

If `X-Dependency-Tokens` is missing or `github_rest` is not present:
```json
HTTP/1.1 401

{"error": "missing_dependency_token", "service": "github_rest"}
```

If the GitHub token gets a 401/403 from GitHub:
```json
HTTP/1.1 401

{"error": "dependency_token_expired", "service": "github_rest"}
```

The bridge handles re-auth from there. We don't refresh tokens, handle OAuth flows, or access any database for tokens.

### When is the GitHub token needed?

| Scenario | GitHub token required? |
|---|---|
| Search an already-indexed repo | No — just vector math, no GitHub calls. |
| First search on an un-indexed repo | Yes — triggers indexing, needs GitHub access. |
| Explicit re-index | Yes — fetches repo content from GitHub. |
| Index status check | No — reads from our MongoDB. |
| Delete an index | No — deletes from our S3 + MongoDB. |

For requests that don't need the GitHub token (search on an indexed repo, status, delete), the header can be absent or the `github_rest` key can be missing — we only check it when we actually need to call GitHub.

## Service Layout

Follows the existing pattern established by lakehouse and prbot.

```
services/reposearch/
├── src/
│   ├── main.py              # FastAPI app, lifespan, CORS, health, OpenAPI spec
│   ├── config.py            # env var loading (REPO_SEARCH_*)
│   ├── db.py                # Motor async MongoDB client
│   ├── models.py            # Pydantic request/response schemas
│   ├── routes/
│   │   └── external.py      # /api/v1/repo-search/* endpoints
│   ├── services/
│   │   ├── github.py        # GitHub REST API calls (tree, contents, compare)
│   │   ├── indexer.py       # fetch → extract chunks → embed → store
│   │   └── searcher.py      # embed query → cosine similarity → top-k
│   └── storage/
│       └── s3.py            # .npz upload/download to apifunnel-repo-vectors bucket
├── openapi/
│   └── reposearch.yaml      # OpenAPI 3.0 spec
├── pyproject.toml
└── Dockerfile
```

**Port:** 3004
**PM2 name:** reposearch
**Caddy prefix:** `/api/v1/repo-search/*` → `localhost:3004`

## Environment Variables

All prefixed with `REPO_SEARCH_` — fully independent from other services.

```
REPO_SEARCH_ADMIN_KEY=           # admin key to gate API access
REPO_SEARCH_MONGODB_URI=         # can share Atlas cluster, different DB
REPO_SEARCH_DB_NAME=apifunnel_repo_search
REPO_SEARCH_S3_ENDPOINT=         # Hetzner S3 endpoint
REPO_SEARCH_S3_ACCESS_KEY=       # separate access key pair
REPO_SEARCH_S3_SECRET=
REPO_SEARCH_S3_REGION=
REPO_SEARCH_S3_BUCKET=apifunnel-repo-vectors
```

## Endpoints

All under `/api/v1/repo-search`.

### POST /api/v1/repo-search/search

Semantic search across a repo. If the repo isn't indexed yet, triggers indexing first (requires GitHub token in `X-Dependency-Tokens`).

**Headers:**
```
Authorization: Bearer <REPO_SEARCH_ADMIN_KEY>
X-User-Token: <user JWT>
X-Dependency-Tokens: {"github_rest": "gho_..."}
```

**Request:**
```json
{
  "repo": "owner/repo-name",
  "query": "onboarding flow for new users",
  "branch": "main",
  "top_k": 10
}
```

**Response (index ready):**
```json
{
  "repo": "owner/repo-name",
  "query": "onboarding flow for new users",
  "results": [
    {
      "file_path": "src/components/onboarding/auth-dialog.tsx",
      "chunk": "Onboarding authentication dialog — handles new user signup",
      "chunk_type": "docstring",
      "score": 0.87
    }
  ],
  "index_sha": "a1b2c3d..."
}
```

**Response (indexing in progress):**
```json
{
  "repo": "owner/repo-name",
  "status": "indexing",
  "message": "First-time indexing in progress. Retry in ~30 seconds.",
  "estimated_files": 247
}
```

### GET /api/v1/repo-search/repos/{owner}/{repo}

Index status and metadata. Returns `404` if never indexed.

```json
{
  "repo": "owner/repo-name",
  "status": "ready",
  "branch": "main",
  "last_indexed_sha": "a1b2c3d...",
  "last_indexed_at": "2026-04-10T12:00:00Z",
  "file_count": 247,
  "chunk_count": 1483
}
```

### POST /api/v1/repo-search/repos/{owner}/{repo}/reindex

Force a full re-index. GitHub token required in `X-Dependency-Tokens` header.

```json
{
  "branch": "main"
}
```

### DELETE /api/v1/repo-search/repos/{owner}/{repo}

Remove a repo's index entirely (vectors from S3, record from MongoDB).

## What Gets Indexed

For each file in a repo:

| Source | What to extract |
|---|---|
| File path | Full path as a searchable string |
| Docstrings / JSDoc | Top-of-file and function-level docstrings |
| Comments | Inline and block comments, stripped of syntax markers |
| README files | Full content of `README*` and `CHANGELOG*` |
| Commit messages | Recent commit messages touching the file (configurable N) |

Not indexed: code itself (variable names, function bodies, imports). That's what GitHub code search is for.

## Embedding

`all-MiniLM-L6-v2` via `sentence-transformers` (PyPI). 384 dimensions, fast, good for short text chunks.

At query time: embed query → dot product against stored matrix → return top-k. Pure numpy — no FAISS or vector DB needed at this scale.

## Vector Storage (S3)

Each repo index is a single `.npz` file on S3.

- **Bucket:** `apifunnel-repo-vectors`
- **Key pattern:** `{owner}/{repo}/{branch}.npz`
- **Contents:**
  - `embeddings`: float32 matrix, shape `(N, 384)`
  - `file_paths`: string array, length N
  - `chunks`: string array, length N
  - `chunk_types`: string array (docstring, comment, readme, path, commit)

Size estimate: 500 files × ~3 chunks avg = 1500 chunks × 1.5 KB = ~2.3 MB per repo. Manageable.

## Metadata Storage (MongoDB)

Database: `apifunnel_repo_search`

### Collection: `repo_indexes`

```json
{
  "repo": "owner/repo-name",
  "branch": "main",
  "last_indexed_sha": "a1b2c3d...",
  "last_indexed_at": "2026-04-10T12:00:00Z",
  "file_count": 247,
  "chunk_count": 1483,
  "s3_key": "owner/repo-name/main.npz",
  "status": "ready",
  "created_at": "2026-04-10T12:00:00Z"
}
```

## Indexing Pipeline

1. **Fetch tree** — `GET /repos/{owner}/{repo}/git/trees/{branch}?recursive=1`
2. **Filter** — Skip binary, vendored (`node_modules`, `vendor`, `.git`), large (>100KB)
3. **Fetch content** — `GET /repos/{owner}/{repo}/contents/{path}?ref={branch}`, base64 decode
4. **Extract chunks** — Parse comments, docstrings, README content. Tag each with file path + chunk type.
5. **Add path chunks** — Every file path becomes a chunk (conceptual file name search).
6. **Embed** — Batch all chunks through MiniLM.
7. **Store** — Save `.npz` to S3. Upsert MongoDB record.

### Incremental Re-indexing

1. Compare HEAD SHA against `last_indexed_sha`
2. Same → return `status: "current"`
3. Different → `GET /repos/{owner}/{repo}/compare/{last_sha}...{head_sha}` for changed files
4. Re-extract and re-embed only changed files
5. Replace/append/remove in matrix
6. Upload updated `.npz` to S3

## Infrastructure Changes

### Caddy (`proxy/Caddyfile`)

Add reposearch routing:

```
handle /api/v1/repo-search {
    reverse_proxy localhost:3004
}
handle /api/v1/repo-search/* {
    reverse_proxy localhost:3004
}
handle /health/reposearch {
    rewrite * /health
    reverse_proxy localhost:3004
}
```

### Docker Compose (`docker-compose.yml`)

```yaml
reposearch:
  build: services/reposearch
  ports:
    - "3004:3004"
  env_file: .env
  restart: unless-stopped
```

### CI/CD (`deploy.yml`)

Add to change detection:
```
reposearch=$(echo "$CHANGED" | grep -qE '^services/reposearch/' && echo true || echo false)
```

Add deploy step (same pattern as prbot/lakehouse):
```
- name: Deploy reposearch
  if: needs.detect-changes.outputs.reposearch == 'true'
  run: |
    SSH="ssh -i ~/.ssh/key -o StrictHostKeyChecking=no root@$NODE_IP"
    SCP="scp -i ~/.ssh/key -o StrictHostKeyChecking=no -r"

    $SSH "mkdir -p /opt/api-apifunnel/services/reposearch"
    $SCP services/reposearch/pyproject.toml "root@$NODE_IP:/opt/api-apifunnel/services/reposearch/"
    $SCP services/reposearch/src "root@$NODE_IP:/opt/api-apifunnel/services/reposearch/"
    $SCP services/reposearch/openapi "root@$NODE_IP:/opt/api-apifunnel/services/reposearch/"

    $SSH "cd /opt/api-apifunnel/services/reposearch && pip install --no-cache-dir -q ."
    $SSH "cd /opt/api-apifunnel && pm2 restart reposearch --update-env 2>/dev/null || pm2 start 'uvicorn src.main:app --host 0.0.0.0 --port 3004 --workers 2' --name reposearch --cwd services/reposearch --interpreter none --max-memory-restart 512M"
```

Add env vars to the `.env` block written by CI:
```
REPO_SEARCH_ADMIN_KEY=${{ secrets.REPO_SEARCH_ADMIN_KEY }}
REPO_SEARCH_MONGODB_URI=${{ secrets.REPO_SEARCH_MONGODB_URI }}
REPO_SEARCH_DB_NAME=apifunnel_repo_search
REPO_SEARCH_S3_ENDPOINT=${{ secrets.REPO_SEARCH_S3_ENDPOINT }}
REPO_SEARCH_S3_ACCESS_KEY=${{ secrets.REPO_SEARCH_S3_ACCESS_KEY }}
REPO_SEARCH_S3_SECRET=${{ secrets.REPO_SEARCH_S3_SECRET }}
REPO_SEARCH_S3_REGION=${{ secrets.REPO_SEARCH_S3_REGION }}
REPO_SEARCH_S3_BUCKET=apifunnel-repo-vectors
```

Add health check:
```
$SSH "curl -sf http://localhost:3004/health && echo ' ✅ Repo Search OK' || (echo ' ❌ Repo Search down' && pm2 logs reposearch --nostream --lines 30 2>&1 || true)"
```

### .env.example

Add section:
```
# ── Repo Search ────────────────────────────────────────────
REPO_SEARCH_ADMIN_KEY=
REPO_SEARCH_MONGODB_URI=mongodb+srv://...
REPO_SEARCH_DB_NAME=apifunnel_repo_search
REPO_SEARCH_S3_ENDPOINT=https://hel1.your-objectstorage.com
REPO_SEARCH_S3_ACCESS_KEY=
REPO_SEARCH_S3_SECRET=
REPO_SEARCH_S3_REGION=hel1
REPO_SEARCH_S3_BUCKET=apifunnel-repo-vectors
```

## Dependencies (`pyproject.toml`)

```toml
[project]
name = "reposearch-api"
version = "1.0.0"
description = "Semantic search over GitHub repositories"
requires-python = ">=3.11"
dependencies = [
    "fastapi>=0.115.0",
    "uvicorn[standard]>=0.30.0",
    "motor>=3.3.0",
    "boto3>=1.35.0",
    "httpx>=0.27.0",
    "numpy>=1.26.0",
    "sentence-transformers>=3.0.0",
    "python-dotenv>=1.0.1",
]
```

## v1 Scope

- Index: comments, docstrings, READMEs, file paths
- Query: top-k semantic search, single repo
- Storage: S3 (.npz) + MongoDB (metadata)
- Embedding: MiniLM local (sentence-transformers)
- Auth: admin key on endpoints (same as lakehouse); GitHub token via `X-Dependency-Tokens` header; user identity via `X-User-Token`
- Infra: fully owned — own DB name, own S3 bucket, own credentials

## Not v1

- Cross-repo search
- Webhook-triggered re-indexing
- LLM-generated file summaries
- In-memory caching of hot indexes
- Rate limiting by client
