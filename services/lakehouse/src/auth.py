"""Authentication for the Lakehouse API.

Contract (locked):

    1. X-Admin-Key present and matches MCP_ADMIN_KEY?   → continue. else 401.
    2. Authorization: Bearer <jwt> present?             → decode. else 400.
    3. Decoded payload has `sub`?                       → caller's user_id = payload.sub. else 400.
    4. Every data operation scopes by that user_id. Always.

Admin key is a perimeter check (is the caller allowed to reach this endpoint at all?).
It does NOT convey identity, does NOT grant unscoped access, and does NOT substitute
for a user_id. Identity comes exclusively from the JWT `sub` claim. JWTs are decoded,
not signature-verified — this is a trusted service-to-service relationship.
"""

import base64
import json
import logging
import os
from dataclasses import dataclass
from typing import Optional

from fastapi import HTTPException, Request

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


def _decode_jwt_payload(token: str) -> Optional[dict]:
    """Base64-decode the JWT payload. No signature verification — trusted relationship."""
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


def _admin_key_match(candidate: str) -> bool:
    expected = os.environ.get("MCP_ADMIN_KEY")
    if not expected or not candidate:
        return False
    return candidate == expected


def require_identity(request: Request) -> Identity:
    """Authenticate a request and return the caller's identity.

    Raises HTTPException with the appropriate status per the four-line contract:
      - 401 if X-Admin-Key is missing or does not match MCP_ADMIN_KEY.
      - 400 if Authorization: Bearer <jwt> is absent or undecodable.
      - 400 if the decoded payload has no `sub` claim.

    On success returns an `Identity` keyed on `sub`. There is no admin mode;
    there is no unscoped caller; every authenticated request has a user_id.
    """
    admin_key = request.headers.get("x-admin-key") or ""
    if not _admin_key_match(admin_key):
        raise HTTPException(status_code=401, detail="Unauthorized")

    auth = request.headers.get("authorization") or ""
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=400, detail="Missing Authorization Bearer token")

    claims = _decode_jwt_payload(auth[7:])
    if not claims:
        raise HTTPException(status_code=400, detail="Unreadable JWT")

    sub = claims.get("sub")
    if not sub:
        raise HTTPException(status_code=400, detail="JWT missing 'sub' claim")

    return Identity(
        user_id=sub,
        tenant_id=claims.get("tenant_id"),
        instance_id=claims.get("instance_id"),
        email=claims.get("email"),
        subagent_task_id=claims.get("subagent_task_id"),
        scheduled_task_id=claims.get("scheduled_task_id"),
        client_meta=claims.get("client_meta"),
    )
