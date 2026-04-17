# Testing Guide

How to run test harnesses for the services in this repo.

## Subagents service — endpoint harness

Shell harness covering the full subagents API surface:

- `POST /v1/subagents/one-shot` — embedded-agent direct LLM call (OpenAI / Anthropic / Google)
- `POST /v1/subagents/headless_reasoning_action_with_tools` — canonical ReAct dispatch
- `POST /v1/subagents` — legacy dispatch alias
- Auth matrix (canonical, wrong admin, unauth)
- Route-collision check (`/one-shot` must not match `:id` dynamic routes)

### Prerequisites

1. **Subagents service running locally**:
   ```bash
   cd services/subagents
   set -a; source ../../.env; set +a   # load MONGODB_URI, MCP_ADMIN_KEY, etc.
   npm run dev
   ```
   Default host `http://localhost:3001`. Change with `--host` if yours differs.

2. **LLM API keys** for the happy-path tests (optional — if unset, those tests skip cleanly and only the negative paths run).
   - `OPENAI_API_KEY` — for OpenAI provider test
   - `ANTHROPIC_API_KEY` — for Anthropic provider test
   - `GOOGLE_API_KEY` (or `GEMINI_API_KEY`) — for Google provider test

   If you don't have your own test keys, the bridge repo's `.env` has them. Source it before running:
   ```bash
   set -a; source ../mcp-code-execution/.env; set +a
   ```

   The harness uses these keys **in-process only** — mints an ephemeral JWT carrying `api_settings`, posts to the endpoint, discards. No persistence anywhere.

### Running

From the repo root:

```bash
./services/subagents/scripts/test-endpoints.sh
```

Flags:
- `--host URL` — override server base URL (default `http://localhost:3001`)
- `--verbose` / `-v` — print full request/response bodies
- `--help` — usage

Exit code: `0` if all non-skipped tests pass, `1` otherwise.

### Expected output (all keys set)

```
  Results: 19 passed, 0 failed, 1 skipped
```

The one skipped entry is the headless-dispatch happy path, which requires the web app's `/api/chat` to be reachable (see "Web app integration" below).

### Expected output (no LLM keys set)

```
  Results: 10 passed, 0 failed, 4 skipped
```

Only negative paths run. The 4 skipped are the three provider-specific one-shot happy paths plus the headless happy path.

### What each test actually proves

| Test | Proves |
|------|--------|
| `one-shot [unauth]` | Route enforces authentication |
| `one-shot [empty body]` | Input validation runs (400 `"A prompt is required"`) |
| `one-shot [no api_key]` | JWT path decodes, `api_settings` lookup runs, correct 400 `missing_api_key` error — confirms the direct-LLM code path is executing, NOT `runSubagent` |
| `one-shot [wrong admin]` | Auth module rejects bad admin key |
| `one-shot [openai/anthropic/google happy path]` | Real LLM call returns non-empty `text`, correct `tokens`, correct `provider` |
| `headless [unauth/empty body]` | ReAct dispatch auth + validation |
| `/v1/subagents [legacy alias]` | Backwards compat — same handler as `headless_reasoning_action_with_tools` |
| `GET /v1/subagents/one-shot` | Route ordering — static `one-shot` doesn't collide with `:id` dynamic routes |

### Web app integration (headless dispatch happy path)

The headless subagent dispatch happy-path test requires `$APP_BASE_URL/health` to return 200. By default `APP_BASE_URL=https://app.apifunnel.ai` (prod). For local end-to-end:

1. Start the web app (`one-mcp` repo) on port 4000.
2. Set `APP_BASE_URL=http://localhost:4000` in this repo's `.env`.
3. Restart the subagents service.
4. Re-run the harness with any LLM key set.

The harness will pick up the reachable URL automatically and run the dispatch test.

### Troubleshooting

- **`MCP_ADMIN_KEY not set in .env`** — ensure the repo root has a populated `.env` file.
- **`curl: (7) Failed to connect to localhost:3001`** — subagents service is not running. Start it per the prerequisites above.
- **Model `xxx` is no longer available** — Google and Anthropic deprecate model IDs regularly. Edit `scripts/test-endpoints.sh` to use a currently-valid id. Query live lists:
  ```bash
  # Anthropic
  curl -s https://api.anthropic.com/v1/models \
    -H "x-api-key: $ANTHROPIC_API_KEY" \
    -H "anthropic-version: 2023-06-01" | jq '.data[].id'

  # Google
  curl -s "https://generativelanguage.googleapis.com/v1beta/models?key=$GOOGLE_API_KEY" \
    | jq '.models[] | select(.supportedGenerationMethods[]? == "generateContent") | .name'
  ```
- **Thinking models (Gemini 2.5, Claude 4 extended-thinking, OpenAI o1/o3)** can consume the entire `max_tokens` budget on reasoning and emit no visible text. The harness uses `gemini-flash-lite-latest` (non-thinking), `claude-haiku-4-5` (fast, non-thinking), and `gpt-4o-mini` (non-reasoning) specifically to avoid this.

## Adding tests for other services

When adding a harness for lakehouse / prbot / reposearch / wake, follow the same pattern:

1. `services/<name>/scripts/test-endpoints.sh` — self-contained shell harness
2. Cover: health, OpenAPI serve, auth matrix (canonical + legacy + wrong + unauth), input validation, happy paths (skip cleanly if preconditions not met)
3. Update this doc with a new section describing how to run it
