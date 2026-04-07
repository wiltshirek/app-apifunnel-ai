# Lakehouse Server — Unify Route Namespace

The `/internal/assets/*` prefix was a leftover from when these routes lived
on the bridge and were only called by sandbox containers. Now that the
lakehouse is a standalone service, there is no "internal" — every caller
is external. One namespace.

---

## What to do

Move all `/internal/assets/*` routes to `/api/v1/assets/*`:

| Current path | New path |
|---|---|
| `GET /internal/assets/{id}` | `GET /api/v1/assets/{id}` (already exists — merge) |
| `GET /internal/assets/{id}/view` | `GET /api/v1/assets/{id}/view` |
| `GET /internal/assets/{id}/bytes` | `GET /api/v1/assets/{id}/bytes` |
| `GET /internal/assets/{id}/text` | `GET /api/v1/assets/{id}/text` |
| `GET /internal/assets` | `GET /api/v1/assets` (already exists — merge) |
| `POST /internal/assets` | `POST /api/v1/assets/ingest` (already exists — merge) |
| `POST /internal/assets/search` | `POST /api/v1/assets/search` |

All routes use the same `_resolve_auth` (admin key + `X-User-Token` or
Bearer JWT). No behavior change, just path change.

Delete the `/internal/assets/*` routes entirely. No deprecation period —
the only caller is this bridge and we're updating simultaneously.

Update `openapi.yaml` in the same commit.

---

*Created: 2026-04-07*
