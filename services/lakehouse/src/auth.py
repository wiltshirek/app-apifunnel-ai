"""Authentication helpers.

Auth contract (same as code-execution API):
    Admin-key callers:
        Authorization: Bearer <MCP_ADMIN_KEY>
        user_id / instance_id as query params (GET) or body fields (POST)

    Direct callers (MCP, browser):
        Authorization: Bearer <user JWT>
"""

import base64
import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

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
    client_meta: Optional[Dict[str, Any]] = None
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
        return json.loads(base64.urlsafe_b64decode(payload_b64))
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


def authenticate_jwt(request: Request) -> Optional[Identity]:
    """Authenticate via Bearer JWT in Authorization header."""
    auth = request.headers.get("authorization") or ""
    if not auth.startswith("Bearer "):
        return None
    claims = _decode_jwt_payload(auth[7:])
    if not claims:
        return None
    return _identity_from_claims(claims)


async def require_identity(request: Request) -> Identity:
    """Authenticate or raise.

    1. Admin-key caller → identity from query params, form fields, or JSON body.
    2. Else → identity from JWT in Authorization header.
    """
    if verify_admin_key(request):
        params = await _resolve_identity_params(request)
        user_id = params.get("user_id")
        if not user_id:
            raise HTTPException(status_code=400, detail="Admin-key caller must provide user_id")

        client_meta = params.get("client_meta")
        if isinstance(client_meta, str):
            try:
                client_meta = json.loads(client_meta)
            except (json.JSONDecodeError, TypeError):
                client_meta = None

        return Identity(
            user_id=user_id,
            instance_id=params.get("instance_id"),
            tenant_id=params.get("tenant_id"),
            subagent_task_id=params.get("subagent_task_id"),
            scheduled_task_id=params.get("scheduled_task_id"),
            client_meta=client_meta,
            is_admin=True,
        )

    ident = authenticate_jwt(request)
    if ident is None:
        raise HTTPException(status_code=401, detail="Unauthorized")
    return ident


_IDENTITY_FIELDS = ("user_id", "instance_id", "tenant_id",
                    "subagent_task_id", "scheduled_task_id", "client_meta")


async def _resolve_identity_params(request: Request) -> dict:
    """Pull identity fields from query params, form data, or JSON body (first wins)."""
    qp = {k: request.query_params[k] for k in _IDENTITY_FIELDS
          if k in request.query_params}
    if "user_id" in qp:
        return qp

    content_type = (request.headers.get("content-type") or "").lower()

    if "multipart/form-data" in content_type or "application/x-www-form-urlencoded" in content_type:
        form = await request.form()
        merged = dict(qp)
        for k in _IDENTITY_FIELDS:
            if k not in merged and k in form:
                merged[k] = str(form[k])
        return merged

    if "application/json" in content_type:
        try:
            body = await request.json()
            if isinstance(body, dict):
                merged = dict(qp)
                for k in _IDENTITY_FIELDS:
                    if k not in merged and k in body:
                        merged[k] = body[k]
                return merged
        except Exception:
            pass

    return qp
