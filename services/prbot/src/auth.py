"""Authentication helpers — unified platform auth shape.

Two distinct concerns, each with its own helper:

  1. verify_admin_key(request) — perimeter guard.
     "Is this caller allowed in at all?" Returns True/False. Says nothing
     about identity.

  2. extract_identity(request) — user identity.
     "Who is the end-user?" Returns an Identity or None. Does not gate access.

Canonical shape (preferred):
    Authorization: Bearer <user JWT>
    X-Admin-Key:   <MCP_ADMIN_KEY>        (optional — service-to-service trust)

Legacy shape (accepted during deprecation window, logs a warning):
    Authorization: Bearer <MCP_ADMIN_KEY>
    X-User-Token:  <user JWT>

See plans/rename-and-auth-unification/AUTH_UNIFICATION.md for the migration plan.
"""

import base64
import json
import logging
import os
from dataclasses import dataclass
from typing import Dict, Optional

from fastapi import Request

logger = logging.getLogger(__name__)


@dataclass
class Identity:
    user_id: str
    tenant_id: Optional[str] = None
    instance_id: Optional[str] = None
    email: Optional[str] = None
    subagent_task_id: Optional[str] = None
    scheduled_task_id: Optional[str] = None


def _decode_jwt_payload(token: str) -> Optional[dict]:
    """Base64-decode the JWT payload (no signature verification)."""
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
        subagent_task_id=claims.get("subagent_task_id"),
        scheduled_task_id=claims.get("scheduled_task_id"),
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


def extract_dependency_tokens(request: Request) -> Dict[str, str]:
    """Parse X-Dependency-Tokens header into a dict of resolved credentials."""
    raw = request.headers.get("x-dependency-tokens", "")
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {}


def verify_admin_key(request: Request) -> bool:
    """Perimeter check: is the caller an authorized internal service?

    Accepts:
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


def extract_identity(request: Request) -> Optional[Identity]:
    """Extract user identity.

    Canonical: Authorization: Bearer <user JWT>
    Legacy:    X-User-Token: <user JWT>   (when admin key occupied Authorization)

    Does NOT gate access — use verify_admin_key for perimeter. Returns None
    if no valid identity found.
    """
    auth = request.headers.get("authorization") or ""
    bearer = auth[7:] if auth.startswith("Bearer ") else ""

    # Canonical: JWT directly in Authorization
    if bearer and _looks_like_jwt(bearer):
        claims = _decode_jwt_payload(bearer)
        if claims:
            ident = _identity_from_claims(claims)
            if ident:
                return ident

    # Legacy: JWT in X-User-Token (only meaningful when admin key is in Authorization)
    user_token = request.headers.get("x-user-token") or ""
    if user_token:
        claims = _decode_jwt_payload(user_token)
        if claims:
            ident = _identity_from_claims(claims)
            if ident:
                # Only log once we've confirmed the header is actually used for identity.
                logger.warning(
                    "Legacy auth shape: identity in X-User-Token. "
                    "Caller should migrate to Authorization: Bearer <jwt>."
                )
                return ident

    return None


# --- Deprecated aliases (remove once all call sites updated) ---------------


def authenticate_internal(request: Request) -> Optional[Identity]:
    """DEPRECATED — use verify_admin_key + extract_identity separately."""
    if verify_admin_key(request):
        return extract_identity(request)
    return extract_identity(request)


def authenticate_jwt(request: Request) -> Optional[Identity]:
    """DEPRECATED — use extract_identity instead."""
    return extract_identity(request)
