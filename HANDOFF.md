# API Server Handoff — MCP Team

**Repo**: `git@github.com:wiltshirek/app-apifunnel-ai.git`  
**Runtime**: Node.js 20 + Hono framework  
**Target host**: Hetzner (same box as Graphiti adapter)  
**Port**: 3001 (Caddy proxies to `api.apifunnel.ai`)

---

## What this project is

A standalone REST API server that owns:

- **Subagents** — launch, status, cancel, results, follow-up messages
- **Scheduled Tasks** — CRUD, run history, run-now
- **Notifications** — push / list / ack via Firestore
- **Internal scheduler tick** — receives Vercel Cron call once per minute and dispatches due tasks

It reads from the **same MongoDB database and Firebase project** as the main web app (`one-mcp/web`). No data migration, no new collections.

---

## Getting started immediately

All credentials are already in `.env` at the repo root. Just:

```bash
npm ci
npm run dev        # dev with hot reload on port 3001
# or
npm run build && npm start   # production
```

The `.env` file is gitignored and pre-populated with real credentials copied from the web app. You should not need to look anything up.

**One value you must set before going live:**

```
CRON_SECRET=change-me-set-same-in-vercel
```

Pick any strong random string, set it here, and set the same value in Vercel as `CRON_SECRET` on the `one-mcp/web` project. This authenticates the Vercel Cron call to `/v1/internal/scheduler-tick`.

---

## Project structure

```
src/
  index.ts                      ← Hono app entrypoint, port 3001
  routes/
    subagents.ts                ← POST/GET/DELETE + /:id/response + /:id/message
    scheduled-tasks.ts          ← CRUD + /run-now + /runs + /runs/summary
    notifications.ts            ← GET/POST + /:id/ack
    internal.ts                 ← POST /scheduler-tick
  lib/
    db.ts                       ← MongoDB connection (mongoose)
    jwt.ts                      ← verifyToken, getAuthFromRequest, mintAppJwt
    auth-internal.ts            ← dual-auth: X-Admin-Key + Bearer JWT
    firebase-admin.ts           ← Firestore Admin SDK
    graphiti-runtime.ts         ← reads GRAPHITI_SERVICE_URL env var
    subagent-runner.ts          ← SSE consumer, calls APP_BASE_URL/api/chat
    notifications/              ← push, consume, format, types
    learning-graph/             ← client (HTTP to Graphiti), record-run-learnings,
                                   format-last-run-lessons
    utils/schedule.ts           ← calculateNextRun, isValidCron, generateThreadId
  models/
    SubagentTask.ts             ← verbatim from one-mcp/web
    ScheduledTask.ts            ← verbatim from one-mcp/web
    ConversationThread.ts       ← verbatim from one-mcp/web
openapi/
  orchestration.yaml            ← full OpenAPI 3.1 spec, served at GET /v1/openapi.json
.env                            ← real credentials (gitignored, pre-populated)
.env.example                    ← template for reference
```

---

## Graphiti on Hetzner

Graphiti is an HTTP adapter service. Run it on the **same Hetzner box** as this server.

The env var `GRAPHITI_SERVICE_URL=http://localhost:8001` in `.env` is already set. When Graphiti is running on the box, learning graph calls (preload, ingest, query) will work automatically with zero code changes.

If `GRAPHITI_SERVICE_URL` is unset or Graphiti is unreachable, all learning graph calls silently no-op — safe to leave during initial deployment.

The web app (`one-mcp/web`) also calls Graphiti. Its `graphiti-runtime.ts` currently only enables it during `npm run dev`. Once Graphiti is hosted, make this change:

**File**: `one-mcp/web/src/lib/graphiti-runtime.ts`

Replace the entire file with:

```ts
export function getGraphitiUrl(): string | null {
  return process.env.GRAPHITI_SERVICE_URL || null;
}

export function graphitiDisabledReason(): string {
  return 'GRAPHITI_SERVICE_URL is not set';
}
```

Then add to Vercel environment variables:
```
GRAPHITI_SERVICE_URL=https://api.apifunnel.ai/graphiti
```

And add a Caddy reverse proxy entry for that path (see Hetzner deployment section below).

---

## What needs to change in `one-mcp/web` (the Vercel project)

### 1. Retarget the Vercel Cron

The web app's `vercel.json` cron currently calls its own internal route. Change it to call this server:

**`one-mcp/web/vercel.json`** — find the scheduler-tick cron entry and change the path to an external fetch, or use a serverless function that forwards to:

```
POST https://api.apifunnel.ai/v1/internal/scheduler-tick
Authorization: Bearer {CRON_SECRET}
```

The simplest approach: keep the cron pointed at `/api/internal/scheduler-tick` in the web app, but update that route handler to just proxy the call to the new server. Then delete it once confirmed stable.

Once Vercel Cron is confirmed calling the new server correctly, delete:
```
one-mcp/web/src/app/api/internal/scheduler-tick/route.ts
```

### 2. Retarget `dispatch_subagent` platform tool

**File**: `one-mcp/web/src/lib/platform-tools/handlers/dispatch-subagent.ts`

Line 37 currently calls `${baseUrl}/api/subagents`. The `baseUrl` is passed in from the chat route and defaults to the same app. Change the fetch target to the new API server:

```ts
// Before (line 37)
const res = await fetch(`${baseUrl}/api/subagents`, {

// After
const orchestrationUrl = process.env.ORCHESTRATION_API_URL || baseUrl
const res = await fetch(`${orchestrationUrl}/v1/subagents`, {
```

Add to Vercel environment variables:
```
ORCHESTRATION_API_URL=https://api.apifunnel.ai
```

### 3. Fix `graphiti-runtime.ts` (described above)

### 4. Update `payload_url` in subagent notifications

**File**: `one-mcp/web/src/lib/subagent-runner.ts`

The `pushNotification` call near the bottom sets a relative `payload_url`. Update to absolute:

```ts
// Before
payload_url: `/api/subagents/${task_id}/response`  (if present)

// After — in one-mcp/web/src/lib/subagent-runner.ts
// No payload_url is currently set in this file — the new server's
// src/lib/subagent-runner.ts also omits it. Leave as-is for now.
// The agent uses get_subagent_status which knows the correct base URL.
```

This is low priority — the agent resolves the payload via `task_id` regardless.

---

## Add your Lakehouse endpoints

Create `src/routes/lakehouse.ts` using the same pattern as any other route:

```ts
import { Hono } from 'hono';
import { connectDB } from '../lib/db';
import { authenticateInternalRequest } from '../lib/auth-internal';

export const lakhouseRouter = new Hono();

lakhouseRouter.get('/assets', async (c) => {
  await connectDB();
  const auth = authenticateInternalRequest(c.req.raw);
  if (!auth) return c.json({ error: 'Unauthorized' }, 401);
  // ... your logic
});
```

Register in `src/index.ts`:

```ts
import { lakhouseRouter } from './routes/lakehouse';
app.route('/v1/lakehouse', lakhouseRouter);
```

Auth helpers available:
- `authenticateInternalRequest(req)` — dual-auth: X-Admin-Key or Bearer JWT
- `getAuthFromRequest(req)` — Bearer JWT only
- Both return an `AuthPayload` with `sub` (user ID), `email`, `instance_id`, etc.

---

## Hetzner deployment

### PM2

Create `ecosystem.config.js` at the repo root:

```js
module.exports = {
  apps: [{
    name: 'api-apifunnel-ai',
    script: 'dist/index.js',
    instances: 1,
    autorestart: true,
    watch: false,
    env_file: '.env',
  }],
};
```

```bash
npm ci
npm run build
pm2 start ecosystem.config.js
pm2 save
pm2 startup   # follow the printed command to enable auto-restart on reboot
```

### Caddy

Add to your `Caddyfile`:

```
api.apifunnel.ai {
    reverse_proxy /v1/* localhost:3001
    reverse_proxy /health localhost:3001

    # Proxy Graphiti under a clean path (optional — lets the web app reach it via HTTPS)
    reverse_proxy /graphiti/* localhost:8001
}
```

Caddy handles HTTPS + Let's Encrypt automatically.

### Deploy flow

```bash
git pull origin main
npm ci
npm run build
pm2 restart api-apifunnel-ai
```

---

## Complete `.env` reference

All values are already in the `.env` file. This table is for reference only.

| Variable | Value source | Notes |
|---|---|---|
| `MONGODB_URI` | `web/.env.local` | Same cluster, same `apifunnel` database |
| `JWT_SECRET` | `web/.env.local` | Web app uses unsigned JWTs — keep as `unsigned-trust-relationship` |
| `MCP_ADMIN_KEY` | `web/.env.local` | Must match bridge exactly |
| `CRON_SECRET` | **You set this** | Any strong random string, same value in Vercel |
| `FIREBASE_PROJECT_ID` | `web/.env.local` | `apifunnel-ai` |
| `FIREBASE_CLIENT_EMAIL` | `web/.env.local` | Same service account |
| `FIREBASE_PRIVATE_KEY` | `web/.env.local` | Same service account key |
| `APP_BASE_URL` | — | `https://app.apifunnel.ai` — subagent runner calls `/api/chat` here |
| `GRAPHITI_SERVICE_URL` | — | `http://localhost:8001` once Graphiti is running on the box |
| `PORT` | — | `3001` |
| `NODE_ENV` | — | `production` |

---

## Vercel environment variables to add on `one-mcp/web`

Once this server is live, add these to the Vercel project settings for `one-mcp/web`:

| Variable | Value |
|---|---|
| `ORCHESTRATION_API_URL` | `https://api.apifunnel.ai` |
| `GRAPHITI_SERVICE_URL` | `https://api.apifunnel.ai/graphiti` |
| `CRON_SECRET` | Same value as in this server's `.env` |

---

## Quick health check after deploy

```bash
curl https://api.apifunnel.ai/health
# → {"status":"ok","ts":"..."}

curl https://api.apifunnel.ai/v1/openapi.json
# → full OpenAPI YAML spec
```
