"""Authentication helpers for the repo search service.

Three header concerns:
    Authorization: Bearer <REPO_SEARCH_ADMIN_KEY>   — endpoint protection
    X-User-Token: <JWT>                              — user identity (decoded, not verified)
    X-Dependency-Tokens: {"github_rest": "gho_..."}  — forwarded GitHub OAuth token
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


def verify_admin_key(request: Request) -> bool:
    expected = os.environ.get("REPO_SEARCH_ADMIN_KEY") or os.environ.get("MCP_ADMIN_KEY")
    if not expected:
        return False
    auth = request.headers.get("authorization") or ""
    if not auth.startswith("Bearer "):
        return False
    token = auth[7:]
    if len(token) > 100:
        return False
    return token == expected


def get_identity(request: Request) -> Optional[Identity]:
    """Extract user identity from X-User-Token header (JWT, decoded without verification)."""
    user_token = request.headers.get("x-user-token") or ""
    if not user_token:
        return None
    claims = _decode_jwt_payload(user_token)
    if not claims:
        return None
    user_id = claims.get("sub") or claims.get("user_id")
    if not user_id:
        return None
    return Identity(
        user_id=user_id,
        tenant_id=claims.get("tenant_id"),
        instance_id=claims.get("instance_id"),
        email=claims.get("email"),
    )


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
    """Return an error response if the admin key is invalid, or None if OK."""
    if not verify_admin_key(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    return None


def require_github_token(request: Request) -> tuple[Optional[str], Optional[JSONResponse]]:
    """Return (token, None) if present, or (None, error_response) if missing."""
    token = get_github_token(request)
    if not token:
        return None, JSONResponse(
            {"error": "missing_dependency_token", "service": "github_rest"},
            status_code=401,
        )
    return token, None
