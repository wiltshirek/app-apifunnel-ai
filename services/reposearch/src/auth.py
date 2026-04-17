"""Authentication helpers — unified platform auth shape.

Canonical shape (preferred):
    Authorization: Bearer <user JWT>
    X-Admin-Key:   <MCP_ADMIN_KEY>        (optional — service-to-service trust)

Legacy shape (accepted during deprecation window, logs a warning):
    Authorization: Bearer <MCP_ADMIN_KEY>
    X-User-Token:  <user JWT>

Dependency tokens (unchanged):
    X-Dependency-Tokens: {"github_rest": "gho_..."}

See plans/rename-and-auth-unification/AUTH_UNIFICATION.md for the migration plan.
"""

import base64
import json
import logging
import os
from dataclasses import dataclass
from typing import Optional

from fastapi import Request
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)


@dataclass
class Identity:
    user_id: str
    tenant_id: Optional[str] = None
    instance_id: Optional[str] = None
    email: Optional[str] = None
    is_admin: bool = False


def _decode_jwt_payload(token: str) -> Optional[dict]:
    parts = token.split(".")
    if len(parts) != 3:
        return None
    try:
        payload_b64 = parts[1]
        padding = 4 - len(payload_b64) % 4
        if padding != 4:
            payload_b64 += "=" * padding
        return json.loads(base64.b64decode(payload_b64))
    except Exception:
        return None


def _identity_from_claims(claims: dict) -> Optional[Identity]:
    user_id = claims.get("sub") or claims.get("user_id")
    if not user_id:
        return None
    return Identity(
        user_id=user_id,
        tenant_id=claims.get("tenant_id"),
        instance_id=claims.get("instance_id"),
        email=claims.get("email"),
    )


def _looks_like_jwt(token: str) -> bool:
    if not token:
        return False
    parts = token.split(".")
    if len(parts) != 3:
        return False
    return _decode_jwt_payload(token) is not None


def _admin_key_match(candidate: str) -> bool:
    expected = os.environ.get("MCP_ADMIN_KEY")
    if not expected or not candidate:
        return False
    return candidate == expected


def verify_admin_key(request: Request) -> bool:
    """Returns True if the caller carries a valid platform admin key.

    Accepts either:
      - X-Admin-Key: <MCP_ADMIN_KEY>             (canonical)
      - Authorization: Bearer <MCP_ADMIN_KEY>    (legacy — logs a warning)
    """
    canonical = request.headers.get("x-admin-key") or ""
    if canonical and _admin_key_match(canonical):
        return True

    auth = request.headers.get("authorization") or ""
    if auth.startswith("Bearer "):
        token = auth[7:]
        if token and not _looks_like_jwt(token) and _admin_key_match(token):
            logger.warning(
                "Legacy auth shape: admin key in Authorization header. "
                "Caller should migrate to X-Admin-Key."
            )
            return True

    return False


def authenticate_internal(request: Request) -> Optional[Identity]:
    """Resolve user identity for a protected route.

    Canonical: identity from Authorization Bearer JWT; X-Admin-Key (if present)
    upgrades is_admin.

    Legacy: admin key in Authorization, JWT in X-User-Token (logs a warning).
    """
    auth = request.headers.get("authorization") or ""
    bearer = auth[7:] if auth.startswith("Bearer ") else ""

    # Canonical: JWT in Authorization
    if bearer and _looks_like_jwt(bearer):
        claims = _decode_jwt_payload(bearer)
        if not claims:
            return None
        ident = _identity_from_claims(claims)
        if not ident:
            return None
        x_admin = request.headers.get("x-admin-key") or ""
        ident.is_admin = bool(x_admin and _admin_key_match(x_admin))
        return ident

    # Legacy: admin key in Authorization + JWT in X-User-Token
    if bearer and _admin_key_match(bearer):
        logger.warning(
            "Legacy auth shape: admin key in Authorization + X-User-Token. "
            "Caller should migrate to Authorization: Bearer <jwt> + X-Admin-Key."
        )
        user_token = request.headers.get("x-user-token") or ""
        claims = _decode_jwt_payload(user_token)
        if not claims:
            return None
        ident = _identity_from_claims(claims)
        if ident:
            ident.is_admin = True
        return ident

    return None


def authenticate_jwt(request: Request) -> Optional[Identity]:
    """Extract identity from a user JWT in the Authorization header."""
    auth = request.headers.get("authorization") or ""
    if not auth.startswith("Bearer "):
        return None
    bearer = auth[7:]
    if not _looks_like_jwt(bearer):
        return None
    claims = _decode_jwt_payload(bearer)
    if not claims:
        return None
    return _identity_from_claims(claims)


def get_identity(request: Request) -> Optional[Identity]:
    """Extract user identity from either auth shape (canonical or legacy)."""
    return authenticate_internal(request)


def get_github_token(request: Request) -> Optional[str]:
    """Extract the GitHub OAuth token from X-Dependency-Tokens header."""
    raw = request.headers.get("x-dependency-tokens")
    if not raw:
        return None
    try:
        tokens = json.loads(raw)
        return tokens.get("github_rest")
    except (json.JSONDecodeError, TypeError):
        return None


def require_admin(request: Request) -> Optional[JSONResponse]:
    """Return an error response if neither admin key nor valid JWT is present."""
    if verify_admin_key(request):
        return None
    if authenticate_jwt(request):
        return None
    return JSONResponse({"error": "Unauthorized"}, status_code=401)


def require_github_token(request: Request) -> tuple[Optional[str], Optional[JSONResponse]]:
    """Return (token, None) if present, or (None, error_response) if missing."""
    token = get_github_token(request)
    if not token:
        return None, JSONResponse(
            {"error": "missing_dependency_token", "service": "github_rest"},
            status_code=401,
        )
    return token, None
