# Auth & Identity Contract

## The Principle

Every API endpoint on this platform supports **two authentication modes**. Both resolve to the same `Identity` object. The endpoint logic never knows or cares which mode was used.

### Mode 1 — Platform Caller (admin key + explicit identity)

```
Authorization: Bearer <MCP_ADMIN_KEY>
user_id=...  (query param for GET/DELETE, body/form field for POST/PUT)
instance_id=...  (optional, same placement)
```

**Who uses this:** Our platform application (`one-mcp`) calling on behalf of a user. The platform has already authenticated the user (via session cookie, JWT, etc.) and knows their identity. It asserts that identity explicitly in the request.

**Why:** The platform is a trusted intermediary. It doesn't make sense for it to mint a JWT just to have the API decode it back into the same user_id it already had. Explicit params are simpler, debuggable, and don't couple the API to our JWT format or signing keys.

### Mode 2 — Agent/Direct Caller (JWT carries identity)

```
Authorization: Bearer <user JWT>
```

**Who uses this:** The code execution bridge (MCP server) acting on behalf of an agent session. The bridge has a JWT from the MCP session handshake. It forwards that JWT to the API. Identity is decoded from token claims.

**Why:** In the agent path, the bridge doesn't "know" the user — it just has a session token. The JWT is the identity. The bridge shouldn't have to crack it open and re-package it; that would duplicate parsing logic and create a coupling point. Pass the token through, let the API resolve it.

## Why Two Modes Exist

These aren't two competing patterns that need to converge into one. They exist because the two callers have fundamentally different relationships with user identity:

| | Platform (one-mcp) | Agent Bridge (code-execution) |
|---|---|---|
| **Knows user_id?** | Yes — from its own session/auth | No — only has a JWT token |
| **Trusted?** | Yes — admin key proves it | Yes — JWT signature proves it |
| **Identity source** | Already resolved, pass it explicitly | Encoded in JWT, let the API decode it |
| **Coupling** | None — just a string param | Coupled to JWT format (acceptable) |

Forcing the platform into JWT mode would mean minting throwaway JWTs with no consumer benefit. Forcing the bridge into explicit-identity mode would mean cracking open JWTs in the bridge just to re-serialize them as params — added complexity, added coupling, no gain.

## Implementation

The `require_identity` function in `auth.py` is the single entry point. Every route calls it. It resolves both modes:

```python
def require_identity(request: Request) -> Identity:
    # Mode 1: Admin-key caller → read identity from params
    if verify_admin_key(request):
        user_id = request.query_params.get("user_id")
        if not user_id:
            raise HTTPException(400, "Admin-key caller must provide user_id")
        return Identity(user_id=user_id, instance_id=..., is_admin=True)

    # Mode 2: JWT caller → decode identity from token claims
    ident = authenticate_jwt(request)
    if ident is None:
        raise HTTPException(401, "Unauthorized")
    return ident
```

**Admin key detection:** `verify_admin_key` checks if the Bearer token is short (<=100 chars) and matches `MCP_ADMIN_KEY`. JWTs are always longer, so there's no ambiguity.

## Rules for New Endpoints

1. **Always call `require_identity(request)`** — never parse auth headers manually.
2. **Never assume which mode is in use** — your route logic receives an `Identity` and works with it. Period.
3. **Admin-key callers must provide `user_id`** — if they don't, `require_identity` returns 400. This is intentional. An admin key without a subject is a bug.
4. **JWT callers don't send `user_id` as a param** — it comes from claims. If both are present (admin key + JWT-looking token), admin key wins because `verify_admin_key` checks first.
5. **`is_admin` flag** — set to `True` only for admin-key callers. Use this if you ever need to distinguish (e.g., audit logging). Do not use it for authorization decisions beyond what the endpoint already enforces.

## Cross-Service Consistency

This same contract applies across all API services on this platform:

| Service | Location | Status |
|---|---|---|
| Lakehouse | `services/lakehouse/src/auth.py` | Implemented |
| Code Execution | `mcp-code-execution` (separate repo) | Implemented for `/api/executions/*`, pending for skill/schedule CRUD |
| PR Bot | `services/prbot/` | Uses admin key only (no user-scoped data) |

The code execution server's `lakehouse_client.py` already has a `_resolve_auth` helper that picks the right mode automatically — JWT when a token is available, admin-key + explicit params when it's not. That's the model: the client decides which mode based on what it has, the server accepts either.
