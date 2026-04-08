"""MongoDB CRUD for workspace job tokens.

Collection: workspace_job_tokens

One document per GitHub Actions job. A short-lived, single-use token is
minted at dispatch time and passed to the workflow as an input. The workflow
calls /api/v1/workspace/job-secrets to exchange the token for the user's
ANTHROPIC_API_KEY. Token is consumed on first use and expires after 1 hour.
"""

import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from .api_keys import decrypt_api_key, encrypt_api_key, get_encryption_key

logger = logging.getLogger(__name__)

COLLECTION = "workspace_job_tokens"
TOKEN_TTL_MINUTES = 60


async def mint_job_token(
    user_id: str,
    repo: str,
    anthropic_api_key: str,
) -> str:
    """Encrypt and store the user's API key under a single-use token.

    Returns the token string to embed in the workflow_dispatch inputs.
    """
    from ..db import get_db

    db = await get_db()

    enc_key = get_encryption_key()
    encrypted = encrypt_api_key(anthropic_api_key, enc_key)

    token = str(uuid.uuid4())
    now = datetime.now(timezone.utc)

    await db[COLLECTION].insert_one({
        "token": token,
        "user_id": user_id,
        "repo": repo,
        "api_key_encrypted": encrypted,
        "consumed": False,
        "expires_at": now + timedelta(minutes=TOKEN_TTL_MINUTES),
        "created_at": now,
    })

    await db[COLLECTION].create_index(
        "expires_at",
        expireAfterSeconds=0,
        background=True,
    )

    logger.info("workspace_job_tokens: minted token for user=%s repo=%s", user_id, repo)
    return token


async def consume_job_token(
    token: str,
    repo: str,
) -> Optional[str]:
    """Atomically consume a job token and return the decrypted API key.

    Returns None if the token is invalid, expired, already consumed,
    or belongs to a different repo.
    """
    from ..db import get_db

    db = await get_db()
    now = datetime.now(timezone.utc)

    doc = await db[COLLECTION].find_one_and_update(
        {
            "token": token,
            "repo": repo,
            "consumed": False,
            "expires_at": {"$gt": now},
        },
        {"$set": {"consumed": True}},
    )

    if not doc:
        logger.warning(
            "workspace_job_tokens: invalid/expired/consumed token for repo=%s", repo
        )
        return None

    try:
        enc_key = get_encryption_key()
        api_key = decrypt_api_key(doc["api_key_encrypted"], enc_key)
        logger.info(
            "workspace_job_tokens: consumed token for user=%s repo=%s",
            doc["user_id"], repo,
        )
        return api_key
    except Exception as e:
        logger.error("workspace_job_tokens: decryption failed: %s", e)
        return None
