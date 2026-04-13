"""Authentication helpers — mirrors the bridge's dual-auth pattern.

Internal routes:
    Authorization: Bearer <MCP_ADMIN_KEY>
    X-User-Token: <user JWT>  (decoded without signature verification)

External routes:
    Authorization: Bearer <user JWT>
"""

import base64
import json
import logging
import os
from dataclasses import dataclass
from typing import Optional

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
    client_meta: Optional[dict] = None
    is_admin: bool = False


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
        client_meta=claims.get("client_meta"),
    )


def verify_admin_key(request: Request) -> bool:
    """Check if the request carries a valid MCP_ADMIN_KEY."""
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


def authenticate_internal(request: Request) -> Optional[Identity]:
    """Dual-auth for /internal/* routes.

    1. Authorization: Bearer <MCP_ADMIN_KEY>  →  identity from X-User-Token
    2. Else fall through to JWT in Authorization header
    """
    if verify_admin_key(request):
        user_token = request.headers.get("x-user-token") or ""
        claims = _decode_jwt_payload(user_token)
        if claims:
            ident = _identity_from_claims(claims)
            if ident:
                ident.is_admin = True
                return ident
        return None

    return authenticate_jwt(request)


def authenticate_jwt(request: Request) -> Optional[Identity]:
    """Authenticate via Bearer JWT in Authorization header."""
    auth = request.headers.get("authorization") or ""
    if not auth.startswith("Bearer "):
        return None
    claims = _decode_jwt_payload(auth[7:])
    if not claims:
        return None
    return _identity_from_claims(claims)
