# PR Bot Service

PR Bot dispatches GitHub Actions workflows that use an AI coding agent (Claude Code or Gemini CLI) to create pull requests from natural language task descriptions.

## Location

`services/prbot/` — Python 3.11 / FastAPI / Motor (MongoDB) / httpx.

Port **3003**. Proxy prefix: `/api/v1/prbot/*`.

## Routes

All router endpoints share prefix `/api/v1/prbot`.

| Method | Path | Auth | Purpose |
|--------|------|------|---------|
| POST | `/dispatch` | JWT or admin-key | Sync workflow file, fire `workflow_dispatch`. Returns `dispatch_id`. |
| POST | `/callback` | `report_token` | Receive workflow completion report. Decodes logs into structured lines. |
| GET | `/runs` | JWT or admin-key | List run reports for a repo (paginated, no log lines). |
| GET | `/runs/{id}` | JWT or admin-key | Run status + metadata + log summary. `{id}` = `dispatch_id` or `run_id`. |
| GET | `/runs/{id}/logs` | JWT or admin-key | Paginated log lines. Supports `page`, `page_size`, `page=-1`, `all=true`. |
| GET | `/install-link` | JWT only | Return GitHub App installation URL with `state=user_id` |
| GET | `/install-callback` | None (GitHub redirect) | Link App installation to platform user, 302 back to UI |
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

1. Credentials (github_token, api_key) come from request body or `X-Dependency-Tokens` header.
2. Sync workflow YAML to `.github/workflows/mcp-workspace.yml` on the repo's default branch.
3. Generate `dispatch_id` (`dsp_` + random token) and one-time `report_token`.
4. Fire `workflow_dispatch` with inputs: `task_context`, `api_key`, `base_branch`, `branch_name`, `callback_url`, `report_token`.
5. Store dispatch record in MongoDB with `status: "dispatched"`.
6. Best-effort: wait 2s, fetch the Actions run URL.
7. Return 202 with `dispatch_id`, `run_url`, `branch_name` (or `repo_just_configured` if the workflow was just created — caller retries after ~8s).

## Run Lifecycle

```
dispatched → queued → in_progress → completed | failed | timed_out | cancelled
```

The workflow's final step POSTs a structured report to `/callback` with `report_token` auth. On receipt, the server decodes `agent_log_b64` into structured `log_lines` for paginated retrieval, merges step outcomes, and clears the token.

Use `GET /runs/{dispatch_id}` to poll status and `GET /runs/{dispatch_id}/logs` to fetch agent output.

## Database

Shares the same MongoDB cluster as lakehouse. Env: `PRBOT_MONGODB_URI` (fallback `MONGODB_URI`), `PRBOT_DB_NAME` (default `mcp_code_execution_server`).

Collections used:

| Collection | Module | Purpose |
|------------|--------|---------|
| `prbot_run_reports` | `database/run_reports.py` | Dispatch records, callback reports, structured log lines |
| `github_app_installations` | `database/github_app_installations.py` | App install ↔ user ↔ repo mapping |

Indexes on `prbot_run_reports`:
- `{ dispatch_id: 1 }` unique sparse — lookup by our correlation key
- `{ repo: 1, dispatched_at: -1 }` — list runs for a repo, newest first

## Env Vars

| Var | Required | Default | Purpose |
|-----|----------|---------|---------|
| `PRBOT_MONGODB_URI` | No | `MONGODB_URI` | Mongo connection string |
| `PRBOT_DB_NAME` | No | `mcp_code_execution_server` | Database name |
| `MCP_ADMIN_KEY` | Yes | — | Admin bearer token |
| `PRBOT_BASE_URL` | No | `https://api.apifunnel.ai` | Base URL for callback_url sent to workflow |
| `GH_APP_ID` | Yes | — | GitHub App ID |
| `GH_APP_PRIVATE_KEY_FILE` | Yes* | — | Path to RSA PEM (or use `GH_APP_PRIVATE_KEY` inline) |
| `GH_APP_PRIVATE_KEY` | Yes* | — | RSA PEM string (alternative to file) |
| `GH_APP_WEBHOOK_SECRET` | No | — | HMAC secret for webhook verification (if unset, all webhooks accepted) |
| `WORKSPACE_WORKFLOW_FILE` | No | `mcp-workspace.yml` | Workflow filename synced to repos |
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
    ├── main.py                  # App, lifespan, CORS, health, index creation
    ├── auth.py                  # JWT decode, admin key, Identity dataclass
    ├── db.py                    # Motor client singleton
    ├── database/
    │   ├── run_reports.py       # Dispatch records, log processing, paginated queries
    │   └── github_app_installations.py
    ├── routes/
    │   └── external.py          # All HTTP handlers (APIRouter prefix /api/v1/prbot)
    ├── services/
    │   ├── dispatch.py          # Workflow sync + dispatch logic (generates dispatch_id)
    │   ├── github_api.py        # httpx GitHub REST helpers
    │   └── github_app.py        # App JWT, installation tokens, webhook HMAC
    └── prompts/
        ├── __init__.py          # load_prompt(), load_asset()
        ├── coding_agent.md      # Injected into workflow as CLAUDE.md
        ├── mcp_config.json      # MCP server config injected into workflow
        └── workspace_mcp_server.py  # MCP server for submit_pr tool
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
| subagents | 3001 | `subagents` | `services/subagents/` |
| lakehouse | 3002 | `lakehouse` | `services/lakehouse/` |
| prbot | 3003 | `prbot` | `services/prbot/` |
| reposearch | 3004 | `reposearch` | `services/reposearch/` |
| caddy | 80/443 | systemd | `proxy/` |

Manual full deploy: trigger `workflow_dispatch` on the deploy action — deploys everything.

## Proxy

Production: Caddy on the Hetzner node handles TLS (Let's Encrypt) and path-based routing to each service. Config: `proxy/Caddyfile`.

Local: `proxy/Caddyfile.dev` mounted by docker-compose. HTTP on `:3000`, same routing rules.

| Path pattern | Backend |
|--------------|---------|
| `/api/v1/assets/*` | lakehouse :3002 |
| `/api/v1/prbot/*` | prbot :3003 |
| `/api/v1/repo-search/*` | reposearch :3004 |
| `/v1/*` | subagents :3001 |
| `/health/lakehouse` | lakehouse :3002 (rewrite → `/health`) |
| `/health/prbot` | prbot :3003 (rewrite → `/health`) |
| `/health/reposearch` | reposearch :3004 (rewrite → `/health`) |
| `/health` | subagents :3001 |
| `*` (catch-all) | subagents :3001 |
