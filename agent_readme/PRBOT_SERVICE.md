# PR Bot Service

PR Bot dispatches GitHub Actions workflows that use an AI coding agent (Claude Code or Gemini CLI) to create pull requests from natural language task descriptions.

## Location

`services/prbot/` — Python 3.11 / FastAPI / Motor (MongoDB) / httpx.

Port **3003**. Proxy prefix: `/api/v1/prbot/*`.

## Routes

All router endpoints share prefix `/api/v1/prbot`.

| Method | Path | Auth | Purpose |
|--------|------|------|---------|
| POST | `/preflight` | JWT or admin-key | Check dispatch prerequisites (GitHub connected, LLM key stored) |
| POST | `/dispatch` | JWT or admin-key | Sync workflow file, mint job token, fire `workflow_dispatch` |
| POST | `/callback` | JWT or admin-key | Receive workflow completion callbacks (placeholder) |
| GET | `/install-link` | JWT only | Return GitHub App installation URL with `state=user_id` |
| GET | `/install-callback` | None (GitHub redirect) | Link App installation to platform user, 302 back to UI |
| GET | `/job-secrets` | Job token (query param) | Exchange single-use token for LLM API key — called by the workflow runner |
| POST | `/webhook` | HMAC (`X-Hub-Signature-256`) | Receive GitHub App installation lifecycle events |

App-level (no prefix):

| Method | Path | Auth | Purpose |
|--------|------|------|---------|
| GET | `/health` | None | Returns `{"status": "ok", "service": "prbot"}` |
| GET | `/openapi.yaml` | None | Serves the OpenAPI spec file |

## Auth Patterns

Two modes, same as lakehouse:

1. **User JWT** — `Authorization: Bearer <jwt>`. Payload decoded (no signature verification in this service). Identity from claims.
2. **Admin key + user token** — `Authorization: Bearer <MCP_ADMIN_KEY>` + `X-User-Token: <jwt>`. Identity from the user token, flagged as admin. Admin callers pass `user_id` in the request body.

Exception: `/job-secrets` uses a single-use token as the sole credential. `/webhook` uses HMAC signature verification. `/install-callback` has no auth (GitHub controls the redirect).

## Dispatch Flow

1. Resolve GitHub token — persisted OAuth first, GitHub App installation token fallback.
2. Load `workspace_agent_key` (user's LLM API key, AES-256-GCM encrypted at rest).
3. Mint a single-use job token (UUID, stored with encrypted API key, TTL-indexed).
4. Sync workflow YAML to `.github/workflows/mcp-workspace.yml` on the repo's default branch.
5. Fire `workflow_dispatch` with inputs: `task_context`, `job_token`, `platform_url`, `target_branch`.
6. Best-effort: wait 2s, fetch the Actions run URL.
7. Return 202 with `run_url` (or `repo_just_configured` if the workflow was just created — caller retries after ~8s).

The workflow runner calls back to `/api/v1/prbot/job-secrets` to exchange its token for the API key, then runs the coding agent.

## Database

Shares the same MongoDB cluster as lakehouse. Env: `PRBOT_MONGODB_URI` (fallback `MONGODB_URI`), `PRBOT_DB_NAME` (default `mcp_code_execution_server`).

Collections used:

| Collection | Module | Purpose |
|------------|--------|---------|
| `user_api_tokens` | `database/user_tokens.py` | GitHub OAuth tokens (read-only, bridge owns refresh) |
| `service_api_keys` | `database/api_keys.py` | Encrypted LLM API keys per user |
| `workspace_job_tokens` | `database/job_tokens.py` | Single-use tokens with TTL |
| `github_app_installations` | `database/github_app_installations.py` | App install ↔ user ↔ repo mapping |

## Env Vars

| Var | Required | Default | Purpose |
|-----|----------|---------|---------|
| `PRBOT_MONGODB_URI` | No | `MONGODB_URI` | Mongo connection string |
| `PRBOT_DB_NAME` | No | `mcp_code_execution_server` | Database name |
| `MCP_ADMIN_KEY` | Yes | — | Admin bearer token |
| `ENCRYPTION_KEY` | Yes | — | Base64-encoded 32-byte AES key for API key encryption |
| `GH_APP_ID` | Yes | — | GitHub App ID |
| `GH_APP_PRIVATE_KEY_FILE` | Yes* | — | Path to RSA PEM (or use `GH_APP_PRIVATE_KEY` inline) |
| `GH_APP_PRIVATE_KEY` | Yes* | — | RSA PEM string (alternative to file) |
| `GH_APP_WEBHOOK_SECRET` | No | — | HMAC secret for webhook verification (if unset, all webhooks accepted) |
| `WORKSPACE_WORKFLOW_FILE` | No | `mcp-workspace.yml` | Workflow filename synced to repos |
| `MCP_BRIDGE_BASE_URL` | No | `https://tool.apifunnel.ai` | Base URL passed to workflow for job-secrets fetch |
| `WORKSPACE_INSTALL_RETURN_URL` | No | `https://app.apifunnel.ai/free-tools/prbot/app` | Redirect target after App install |

## OpenAPI Spec

`services/prbot/openapi/prbot.yaml` — hand-maintained, served at `GET /api/v1/prbot/openapi.yaml` (production: `https://api.apifunnel.ai/api/v1/prbot/openapi.yaml`). Documents all router endpoints plus `/health`. Unauthenticated endpoints marked with `security: []`.

## File Structure

```
services/prbot/
├── Dockerfile
├── pyproject.toml
├── openapi/prbot.yaml
└── src/
    ├── main.py                  # App, lifespan, CORS, health, spec route
    ├── auth.py                  # JWT decode, admin key, Identity dataclass
    ├── db.py                    # Motor client singleton
    ├── database/
    │   ├── api_keys.py          # AES-GCM decrypt of service API keys
    │   ├── user_tokens.py       # GitHub OAuth token lookup
    │   ├── job_tokens.py        # Mint/consume single-use tokens
    │   └── github_app_installations.py
    ├── routes/
    │   └── external.py          # All HTTP handlers (APIRouter prefix /api/v1/prbot)
    ├── services/
    │   ├── dispatch.py          # Workflow sync + dispatch logic
    │   ├── preflight.py         # Prerequisite checks + formatters
    │   ├── github_api.py        # httpx GitHub REST helpers
    │   └── github_app.py        # App JWT, installation tokens, webhook HMAC
    └── prompts/
        ├── __init__.py          # load_prompt()
        └── coding_agent.md      # Injected into workflow as CLAUDE.md
```

## Local Dev

```bash
python3 run_server.py prbot        # start on :3003 with --reload
docker compose up                  # all services + Caddy on :3000
```

## Deploy

Single GitHub Actions workflow (`.github/workflows/deploy.yml`) on push to `main`. Detects which services changed and deploys only those. All services deploy to the same Hetzner node via SSH + pm2.

| Service | Port | pm2 name | Trigger path |
|---------|------|----------|--------------|
| orchestration | 3001 | `orchestration` | `services/orchestration/` |
| lakehouse | 3002 | `lakehouse` | `services/lakehouse/` |
| prbot | 3003 | `prbot` | `services/prbot/` |
| caddy | 80/443 | systemd | `proxy/` |

Manual full deploy: trigger `workflow_dispatch` on the deploy action — deploys everything.

## Proxy

Production: Caddy on the Hetzner node handles TLS (Let's Encrypt) and path-based routing to each service. Config: `proxy/Caddyfile`.

Local: `proxy/Caddyfile.dev` mounted by docker-compose. HTTP on `:3000`, same routing rules.

| Path pattern | Backend |
|--------------|---------|
| `/api/v1/assets/*` | lakehouse :3002 |
| `/api/v1/prbot/*` | prbot :3003 |
| `/v1/*` | orchestration :3001 |
| `/health/lakehouse` | lakehouse :3002 (rewrite → `/health`) |
| `/health/prbot` | prbot :3003 (rewrite → `/health`) |
| `/health` | orchestration :3001 |
| `*` (catch-all) | orchestration :3001 |
