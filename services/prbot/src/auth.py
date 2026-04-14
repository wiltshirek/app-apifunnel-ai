"""Authentication helpers.

Two distinct auth mechanisms, each with a different purpose:

  1. Admin key (MCP_ADMIN_KEY) — perimeter guard.
     "Is this caller allowed to reach this API at all?"
     The bridge and internal services send this. It protects routes from
     the public internet. It does NOT convey identity.

  2. JWT — identity.
     "Who is the end-user?" Carried in Authorization (direct) or
     X-User-Token (when admin key occupies Authorization). Only used
     by routes that actually need a user_id (e.g. install-link, run lists).

These are NOT fallbacks for each other. A route should explicitly require
one, the other, or both depending on what it actually needs.
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


def extract_dependency_tokens(request: Request) -> Dict[str, str]:
    """Parse X-Dependency-Tokens header into a dict of resolved credentials.

    The bridge resolves tokens listed in the server's dependency_tokens config
    and sends them JSON-encoded in this header.
    """
    raw = request.headers.get("x-dependency-tokens", "")
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {}


def verify_admin_key(request: Request) -> bool:
    """Perimeter check: is the caller an authorized internal service?

    Looks for MCP_ADMIN_KEY in the Authorization header.
    This only answers "allowed in?" — it says nothing about WHO.
    """
    expected = os.environ.get("MCP_ADMIN_KEY")
    if not expected:
        return False
    auth = request.headers.get("authorization") or ""
    if not auth.startswith("Bearer "):
        return False
    token = auth[7:]
    if len(token) > 100:
        return False
    return token == expected


def extract_identity(request: Request) -> Optional[Identity]:
    """Extract user identity from the request.

    Checks two locations (in order):
      1. X-User-Token header (when admin key occupies Authorization)
      2. Authorization: Bearer <JWT> (direct user calls)

    Returns None if no valid identity found. Does NOT check admin key —
    that's a separate concern (use verify_admin_key for perimeter).
    """
    user_token = request.headers.get("x-user-token") or ""
    if user_token:
        claims = _decode_jwt_payload(user_token)
        if claims:
            ident = _identity_from_claims(claims)
            if ident:
                return ident

    auth = request.headers.get("authorization") or ""
    if not auth.startswith("Bearer "):
        return None
    claims = _decode_jwt_payload(auth[7:])
    if not claims:
        return None
    return _identity_from_claims(claims)


# --- Deprecated aliases (remove once all call sites updated) ---------------

def authenticate_internal(request: Request) -> Optional[Identity]:
    """DEPRECATED — use verify_admin_key + extract_identity separately."""
    if verify_admin_key(request):
        ident = extract_identity(request)
        if ident:
            return ident
        return None
    return extract_identity(request)


def authenticate_jwt(request: Request) -> Optional[Identity]:
    """DEPRECATED — use extract_identity instead."""
    return extract_identity(request)
