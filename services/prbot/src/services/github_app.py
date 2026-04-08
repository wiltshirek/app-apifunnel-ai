"""GitHub App authentication — JWT signing, installation tokens, webhook verification.

Platform credentials come from env:
  GH_APP_ID                Numeric App ID
  GH_APP_PRIVATE_KEY_FILE  Path to RSA private key PEM file (preferred)
  GH_APP_PRIVATE_KEY       Inline PEM string fallback
  GH_APP_WEBHOOK_SECRET    HMAC secret for webhook payload verification
"""

import hashlib
import hmac
import logging
import os
import time
from pathlib import Path
from typing import Optional

import httpx
import jwt

logger = logging.getLogger(__name__)

GITHUB_API_BASE = "https://api.github.com"

APP_INSTALL_URL = "https://github.com/apps/apifunnel-pr-bot"

_WEBHOOK_SECRET = os.environ.get("GH_APP_WEBHOOK_SECRET", "")

_cached_pem: Optional[str] = None


def _load_private_key() -> str:
    """Load the GitHub App RSA private key, preferring file over inline env."""
    global _cached_pem
    if _cached_pem is not None:
        return _cached_pem

    key_file = os.environ.get("GH_APP_PRIVATE_KEY_FILE", "")
    if key_file:
        p = Path(key_file)
        if not p.is_absolute():
            p = Path(os.environ.get("PROJECT_ROOT", ".")) / p
        if p.exists():
            _cached_pem = p.read_text().strip()
            logger.info("github_app: loaded private key from %s (%d bytes)", p, len(_cached_pem))
            return _cached_pem
        raise RuntimeError(
            f"GH_APP_PRIVATE_KEY_FILE points to {p} but the file does not exist"
        )

    inline = os.environ.get("GH_APP_PRIVATE_KEY", "")
    if inline:
        _cached_pem = inline
        logger.info("github_app: using inline GH_APP_PRIVATE_KEY (%d bytes)", len(inline))
        return _cached_pem

    raise RuntimeError(
        "GitHub App private key not configured. "
        "Set GH_APP_PRIVATE_KEY_FILE (path to .pem) or GH_APP_PRIVATE_KEY (inline PEM)."
    )


def make_app_jwt() -> str:
    """Return a signed JWT authenticating as the GitHub App."""
    app_id = os.environ.get("GH_APP_ID", "")
    if not app_id:
        raise RuntimeError("GH_APP_ID must be set to use GitHub App auth.")

    pem = _load_private_key()

    now = int(time.time())
    payload = {
        "iat": now - 60,
        "exp": now + 540,
        "iss": str(app_id),
    }

    return jwt.encode(payload, pem, algorithm="RS256")


async def get_installation_token(
    installation_id: int,
    repo: Optional[str] = None,
) -> str:
    """Generate a short-lived installation access token."""
    app_jwt = make_app_jwt()
    url = f"{GITHUB_API_BASE}/app/installations/{installation_id}/access_tokens"

    body = {}
    if repo:
        _, repo_name = repo.split("/", 1)
        body["repositories"] = [repo_name]
        body["permissions"] = {
            "contents": "write",
            "pull_requests": "write",
            "actions": "write",
            "workflows": "write",
        }

    headers = {
        "Authorization": f"Bearer {app_jwt}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(url, headers=headers, json=body)

    if resp.status_code == 201:
        token = resp.json().get("token")
        if not token:
            raise RuntimeError(f"installation token response missing 'token': {resp.text[:200]}")
        logger.info(
            "github_app: issued installation token for installation_id=%s repo=%s",
            installation_id, repo or "all"
        )
        return token

    body_text = resp.text[:300]
    logger.error(
        "github_app: failed to get installation token: %d %s",
        resp.status_code, body_text
    )
    raise RuntimeError(
        f"GitHub App installation token failed ({resp.status_code}): {body_text}"
    )


def verify_webhook_signature(payload_bytes: bytes, signature_header: str) -> bool:
    """Verify a GitHub webhook payload using HMAC-SHA256."""
    if not _WEBHOOK_SECRET:
        logger.warning("github_app: GH_APP_WEBHOOK_SECRET not set — accepting all webhooks (unsafe)")
        return True

    if not signature_header or not signature_header.startswith("sha256="):
        logger.warning("github_app: missing or malformed X-Hub-Signature-256 header")
        return False

    expected = hmac.new(
        _WEBHOOK_SECRET.encode(),
        payload_bytes,
        hashlib.sha256,
    ).hexdigest()

    received = signature_header[len("sha256="):]
    return hmac.compare_digest(expected, received)
