# API Test Plan — api-apifunnel-ai

**Last run:** 2026-04-07 (local)
**Result:** 42 / 42 passing, 0 broken endpoints (+ 2 new lakehouse tests for promote endpoint)

---

## Auth Patterns

The two services use **different** header layouts for service-to-service (internal) calls:

| Service | Internal auth | Direct auth |
|---------|--------------|-------------|
| **Orchestration** | `X-Admin-Key: <MCP_ADMIN_KEY>` + `Authorization: Bearer <JWT>` | `Authorization: Bearer <signed JWT>` |
| **Lakehouse** | `Authorization: Bearer <MCP_ADMIN_KEY>` + `X-User-Token: <JWT>` | `Authorization: Bearer <MCP_ADMIN_KEY>` (admin) or `Bearer <JWT>` (user) |

---

## OpenAPI Specs

| Service | Local URL | Prod URL | Notes |
|---------|-----------|----------|-------|
| Orchestration | `http://localhost:3001/v1/openapi.json` | `https://api.apifunnel.ai/v1/openapi.json` | Hand-written YAML, served via Caddy `/v1/*` catch-all |
| Lakehouse | `http://localhost:3002/openapi.json` | **Not routed** | FastAPI auto-generated; also serves `/docs` (Swagger UI) and `/redoc` |

> **Gap:** Lakehouse `/openapi.json`, `/docs`, and `/redoc` are not exposed through Caddy
> in production. Add Caddy `handle` blocks if public access is desired.

---

## Orchestration — port 3001

### Health & OpenAPI

| # | Method | Path | Auth | Expect | Status |
|---|--------|------|------|--------|--------|
| 1 | GET | `/health` | none | 200 | PASS |
| 2 | GET | `/v1/openapi.json` | none | 200 | PASS |

### Subagents (`/v1/subagents`)

| # | Method | Path | Auth | Expect | Status |
|---|--------|------|------|--------|--------|
| 3 | GET | `/v1/subagents` | none | 401 | PASS |
| 4 | GET | `/v1/subagents` | X-Admin-Key + JWT | 200 | PASS |
| 5 | GET | `/v1/subagents` | JWT only | 200 | PASS |
| 6 | GET | `/v1/subagents/:id` | JWT (fake id) | 404 | PASS |
| 7 | DELETE | `/v1/subagents/:id` | JWT (fake id) | 404 | PASS |
| 8 | GET | `/v1/subagents/:id/response` | JWT (fake id) | 404 | PASS |
| 9 | POST | `/v1/subagents` | JWT (empty body) | 400 | PASS |
| — | POST | `/v1/subagents/:id/message` | JWT | *not tested — requires real task* | — |

### Scheduled Tasks (`/v1/scheduled-tasks`)

| # | Method | Path | Auth | Expect | Status |
|---|--------|------|------|--------|--------|
| 10 | GET | `/v1/scheduled-tasks` | none | 401 | PASS |
| 11 | GET | `/v1/scheduled-tasks` | JWT | 200 | PASS |
| 12 | GET | `/v1/scheduled-tasks/:id` | JWT (fake id) | 404 | PASS |
| 13 | POST | `/v1/scheduled-tasks` | JWT (empty body) | 400 | PASS |
| 14 | DELETE | `/v1/scheduled-tasks/:id` | JWT (fake id) | 404 | PASS |
| 15 | POST | `/v1/scheduled-tasks/:id/run-now` | JWT (fake id) | 404 | PASS |
| 16 | GET | `/v1/scheduled-tasks/:id/runs` | JWT (fake id) | 404 | PASS |
| 17 | GET | `/v1/scheduled-tasks/:id/runs/summary` | JWT (fake id) | 404 | PASS |
| — | PUT | `/v1/scheduled-tasks/:id` | JWT | *not tested — requires real task* | — |
| — | PATCH | `/v1/scheduled-tasks/:id` | JWT | *not tested — aliases PUT* | — |

### Notifications (`/v1/notifications`)

| # | Method | Path | Auth | Expect | Status |
|---|--------|------|------|--------|--------|
| 18 | GET | `/v1/notifications` | none | 401 | PASS |
| 19 | GET | `/v1/notifications` | X-Admin-Key + JWT | 200 | PASS |
| 20 | POST | `/v1/notifications` | X-Admin-Key + JWT (empty body) | 400 | PASS |
| 21 | POST | `/v1/notifications/:id/ack` | X-Admin-Key + JWT (fake id) | 404 | PASS |

### Internal (`/v1/internal`)

| # | Method | Path | Auth | Expect | Status |
|---|--------|------|------|--------|--------|
| 22 | POST | `/v1/internal/scheduler-tick` | none | 401 | PASS |
| 23 | POST | `/v1/internal/scheduler-tick` | `Bearer <CRON_SECRET>` | 200 | PASS |

---

## Lakehouse — port 3002

### Health

| # | Method | Path | Auth | Expect | Status |
|---|--------|------|------|--------|--------|
| 24 | GET | `/health` | none | 200 | PASS |

### External (`/api/v1/assets`)

| # | Method | Path | Auth | Expect | Status |
|---|--------|------|------|--------|--------|
| 25 | GET | `/api/v1/assets` | none | 401 | PASS |
| 26 | GET | `/api/v1/assets` | admin key | 200 | PASS |
| 26a | GET | `/api/v1/assets` | admin key + X-User-Token | 200 | PASS |
| 26b | GET | `/api/v1/assets` | user JWT | 200 | PASS |
| 27 | GET | `/api/v1/assets/search` | admin (no `q`) | 400 | PASS |
| 28 | GET | `/api/v1/assets/search?q=test` | admin | 200 | PASS |
| 29 | GET | `/api/v1/assets/:id` | admin (real id) | 200 | PASS |
| 30 | GET | `/api/v1/assets/:id` | admin (fake id) | 404 | PASS |
| 31 | GET | `/api/v1/assets/:id/download` | admin (real id) | 200 | PASS |
| 32 | POST | `/api/v1/assets/upload` | JWT with subagent claims (multipart) | 200 + provenance in response | PASS |
| 33 | POST | `/api/v1/assets/ingest` | admin (base64) | 200 | PASS |
| 34 | DELETE | `/api/v1/assets/:id` | admin | 200 | PASS |
| 35 | POST | `/api/v1/assets/:id/promote` | none | 401 | PASS |
| 36 | POST | `/api/v1/assets/:id/promote` | admin (fake id) | 404 | PASS |
| — | POST | `/api/v1/assets/:id/promote` | JWT (real ephemeral asset) | *not tested — requires session-tagged asset* | — |

### Internal (`/internal/assets`)

| # | Method | Path | Auth | Expect | Status |
|---|--------|------|------|--------|--------|
| 37 | GET | `/internal/assets` | none | 401 | PASS |
| 38 | GET | `/internal/assets` | admin + X-User-Token | 200 | PASS |
| 39 | POST | `/internal/assets/search` | admin + X-User-Token | 200 | PASS |
| 40 | GET | `/internal/assets/:id` | admin + X-User-Token (owned asset) | 200 | PASS |
| 41 | GET | `/internal/assets/:id/view` | admin + X-User-Token (owned asset) | 200 | PASS |
| 42 | GET | `/internal/assets/:id/bytes` | admin + X-User-Token (owned asset) | 200 | PASS |
| 43 | GET | `/internal/assets/:id/text` | admin + X-User-Token (plain text asset) | 400 | PASS |
| 44 | POST | `/internal/assets` | admin + X-User-Token (base64) | 200 | PASS |

> **Note on #41:** Returns 400 `"No extracted text available"` because the test asset is
> plain text with no separate `extracted_text` field. This is correct behavior — text
> extraction runs only on documents (PDFs, etc). Would return 200 on a processed PDF.

---

## Not Yet Tested (require real state)

These endpoints exist in code but weren't tested because they need a live subagent run
or a real scheduled task to exercise meaningfully:

| Method | Path | Reason |
|--------|------|--------|
| POST | `/v1/subagents` (full launch) | Fires `runSubagent` — needs MCP bridge running |
| POST | `/v1/subagents/:id/message` | Follow-up — requires a completed task |
| PUT | `/v1/scheduled-tasks/:id` | Update — requires an existing scheduled task |
| PATCH | `/v1/scheduled-tasks/:id` | Aliases PUT |
| POST | `/v1/notifications` (full push) | Requires Firebase configured |
| POST | `/v1/notifications/:id/ack` (real) | Requires a real pending notification |

---

## Caddy Routing (production)

Paths that reach each service through `api.apifunnel.ai`:

| Pattern | Destination |
|---------|-------------|
| `/internal/assets*` | Lakehouse :3002 |
| `/api/v1/assets*` | Lakehouse :3002 |
| `/v1/*` | Orchestration :3001 |
| `/graphiti/*` | Graphiti adapter :8001 (optional) |
| `/health` | Orchestration :3001 |
| `/health/lakehouse` | Lakehouse :3002 (rewritten to `/health`) |
| `/*` (catch-all) | Orchestration :3001 |

**Not routed:** Lakehouse `/openapi.json`, `/docs`, `/redoc`

---

## OpenAPI Spec Alignment (verified 2026-04-06)

Both hand-written specs are now fully aligned with the running code:

| Spec | Location | Paths | Status |
|------|----------|-------|--------|
| `orchestration.yaml` | `services/orchestration/openapi/` | 11 path groups, 18 method+path combos | All code routes covered, no stale entries, all $refs resolve |
| `lakehouse.yaml` | `services/lakehouse/openapi/` | 13 path groups, 15 method+path combos | All code routes covered, no stale entries |

**Changes made to reach alignment:**
- **orchestration.yaml:** Fixed path prefix `/api/` -> `/` (server URL already includes `/v1`). Added `POST /subagents` (launch), `DELETE /subagents/{id}` (cancel), `PATCH /scheduled-tasks/{id}`, `POST /scheduled-tasks/{id}/run-now`. Fixed request schema field names (`message` not `prompt`, `persona_id` not `personaId`). Removed unused batch schemas.
- **lakehouse.yaml (2026-04-06):** Added `POST /api/v1/assets/ingest` endpoint.
- **lakehouse.yaml (2026-04-07):** Removed all `/api/v1/assets/*` paths (browser-facing, not agent-callable). Added `POST /api/v1/assets/{asset_id}/promote`. Spec now only exposes `/internal/assets/*` (agent tools) + `/promote` (UI action) + `/health`.
