# Lakehouse Migration — Server Responsibilities (api-apifunnel-ai)

> **Server team's work only.**
> Bridge changes: `LAKEHOUSE_MIGRATION_BRIDGE.md`
> Client app changes: `LAKEHOUSE_MIGRATION_CLIENT.md`

## Context

The lakehouse service currently has two route namespaces:
- `/internal/assets/*` — for sandbox containers and MCP tools (admin key + X-User-Token)
- `/api/v1/assets/*` — for the client UI (Bearer JWT)

This split was a bridge-ism — it only existed because both lived on the same server.
Now that this is a standalone service, the server doesn't need to know who's calling.
One clean API surface. The wrapping with MCP is the bridge's concern, not yours.

---

## Change 1 — Fix `_resolve_auth` to handle `X-User-Token`

**Why:** The bridge's MCP tools will call your `/api/v1/assets/*` routes using:
```
Authorization: Bearer <MCP_ADMIN_KEY>
X-User-Token: <user JWT>
```

Your current `_resolve_auth` returns `(None, True)` when it sees the admin key —
meaning no user identity is resolved, and all user-scoped queries become admin
queries over all data. The fix reads the forwarded user identity from `X-User-Token`.

**File:** `services/lakehouse/src/auth.py` or wherever `_resolve_auth` lives in `external.py`

```python
def _resolve_auth(request: Request) -> tuple[Optional[Identity], bool]:
    if verify_admin_key(request):
        # Bridge/server-to-server calls: resolve user from forwarded JWT
        user_token = request.headers.get("x-user-token") or ""
        if user_token:
            claims = _decode_jwt_payload(user_token)
            if claims:
                ident = _identity_from_claims(claims)
                if ident:
                    ident.is_admin = True
                    return ident, True
        # Admin key with no X-User-Token = unscoped (admin tooling only)
        return None, True
    # Standard UI path: Bearer JWT
    ident = authenticate_jwt(request)
    return ident, False
```

No CORS changes needed — `main.py` already has `allow_headers=["*"]`, which covers `X-User-Token`.

**This is the only change the bridge team needs before they can start their work.
Do this first.**

---

## Change 2 — Add `POST /api/v1/assets/{asset_id}/promote`

**Why:** The client app calls this to promote an ephemeral session artifact to
permanent storage ("Save Permanently" button in the widget). The client app
cannot cut over from the bridge until this endpoint exists here.

The bridge implementation is in `routes/assets.py` → `api_promote_session_artifact`.
Port that logic — it calls `promote_session_artifact(asset_id, user_id, tenant_id)`
which removes session metadata from the MongoDB document.

**Step 1 — Add `promote_session_artifact` to `services/lakehouse/src/services/assets.py`:**

```python
async def promote_session_artifact(
    db,
    asset_id: str,
    user_id: Optional[str] = None,
    tenant_id: Optional[str] = None,
    admin_access: bool = False,
) -> bool:
    query: dict = {"_id": asset_id}
    if not admin_access:
        if not user_id:
            return False
        query["user_id"] = user_id
    if tenant_id:
        query["tenant_id"] = tenant_id

    result = await db.assets.update_one(
        query,
        {"$unset": {"session_id": "", "is_ephemeral": ""},
         "$set": {"updated_at": datetime.utcnow()}},
    )
    return result.matched_count > 0
```

**Step 2 — Add the route to `services/lakehouse/src/routes/external.py`:**

```python
from ..services.assets import (
    ...
    promote_session_artifact,   # add this import
)

@router.post("/{asset_id}/promote")
async def api_promote(asset_id: str, request: Request):
    ident, is_admin = _resolve_auth(request)
    if not ident and not is_admin:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    db = await get_db()
    promoted = await promote_session_artifact(
        db, asset_id,
        user_id=ident.user_id if ident else None,
        tenant_id=ident.tenant_id if ident else None,
        admin_access=is_admin,
    )
    if not promoted:
        return JSONResponse({"error": "Asset not found or already promoted"}, status_code=404)
    return JSONResponse({"success": True, "asset_id": asset_id,
                         "message": "Asset promoted to permanent storage"})
```

Add to `services/lakehouse/openapi/lakehouse.yaml` under `/api/v1/assets/{asset_id}`:
```yaml
/api/v1/assets/{asset_id}/promote:
  post:
    operationId: promote_asset
    summary: Promote ephemeral artifact to permanent storage
    parameters:
      - name: asset_id
        in: path
        required: true
        schema:
          type: string
    responses:
      '200':
        description: Promoted successfully
      '404':
        description: Not found or already permanent
```

---

## Change 3 — Fix provenance tagging on multipart upload

**Why:** When a subagent run uploads an asset, the JWT contains `subagent_task_id`
and `scheduled_task_id` claims. These must be persisted on the MongoDB asset
document so the platform UI can group assets by subagent run.

**Already done:** `Identity`, `_identity_from_claims`, `upload_asset`, `internal_upload`,
and `api_ingest` all correctly handle provenance. No changes needed there.

**Gap:** The multipart `api_upload` handler (`POST /api/v1/assets/upload`) silently
drops provenance — it calls `upload_asset(...)` without those fields.

**Fix in `services/lakehouse/src/routes/external.py`** — update `api_upload`:
```python
@router.post("/upload")
async def api_upload(
    request: Request,
    files: list[UploadFile] = File(...),
    tenant_id: Optional[str] = Form(None),
    user_id: Optional[str] = Form(None),
):
    ident, is_admin = _resolve_auth(request)
    if not ident and not is_admin:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    effective_user = user_id if is_admin and user_id else (ident.user_id if ident else None)
    if not effective_user:
        return JSONResponse({"error": "user_id required for admin uploads"}, status_code=400)
    effective_tenant = tenant_id or (ident.tenant_id if ident else None)

    db = await get_db()
    uploaded = []
    for f in files:
        file_bytes = await f.read()
        result = await upload_asset(
            db, file_bytes, f.filename or "untitled", effective_user,
            tenant_id=effective_tenant,
            subagent_task_id=ident.subagent_task_id if ident else None,
            scheduled_task_id=ident.scheduled_task_id if ident else None,
        )
        uploaded.append(result)

    return JSONResponse({"assets": uploaded})
```

---

## Change 4 — Fix OpenAPI spec (remove `/api/v1/assets/*` paths)

**File:** `services/lakehouse/openapi/lakehouse.yaml`

**Remove these path blocks entirely:**
- `/api/v1/assets`
- `/api/v1/assets/search`
- `/api/v1/assets/upload`
- `/api/v1/assets/ingest`
- `/api/v1/assets/{asset_id}`
- `/api/v1/assets/{asset_id}/download`

The spec is what the bridge indexes for code execution tool discovery. Agents
should only discover `/internal/assets/*` routes — those are the sandbox-callable
operations. The `/api/v1/assets/*` routes are for the browser UI and should not
appear as agent-callable tools.

Keep in the spec: all `/internal/assets/*` paths and `/health`.

---

## Change 5 (optional, deferred) — Consolidate to one route namespace

**Why:** Once the bridge MCP tools are calling `/api/v1/assets/*` and the client
app has cut over, the `/internal/assets/*` routes are redundant. The server can
remove the `/internal/` prefix entirely — one route set handles all callers via
`_resolve_auth`.

**When:** After bridge Phase A and client cutover are both confirmed in production.

Steps:
1. Merge any features unique to internal routes into `/api/v1/assets/*`
   (primarily: the `X-User-Token` auth support from Change 1)
2. Update bridge's `lakehouse_client.py` to use `/api/v1/assets/*` URLs
3. Remove `routes/internal.py` from the lakehouse service
4. Update the OpenAPI spec — now only `/api/v1/assets/*` paths remain

---

## Dependency Order

```
Change 1 (fix _resolve_auth)      ← bridge team is blocked until this is deployed
Change 2 (add /promote)           ← client app is blocked until this is deployed
Change 3 (provenance tagging)     ← verify before client cutover
Change 4 (fix OpenAPI spec)       ← do anytime, no dependencies
Change 5 (consolidate routes)     ← last, after all consumers verified
```

---

*Created: 2026-04-06*
