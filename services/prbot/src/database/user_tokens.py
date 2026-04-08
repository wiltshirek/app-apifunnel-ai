"""User API token lookup — simplified for prbot.

Reads from the user_api_tokens collection (same as the bridge) to resolve
the user's persisted GitHub OAuth token.

Only the fields needed by workspace dispatch are used here: access_token
and expires_at. Token refresh is not handled — the bridge owns that lifecycle.
"""

import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


async def get_user_api_token(
    user_id: str,
    instance_id: Optional[str],
    server_name: str,
) -> Optional[Dict[str, Any]]:
    """Get a user-scoped OAuth token document from MongoDB.

    Returns the token document dict (with access_token, etc.) or None.
    Rejects expired tokens — does NOT attempt refresh (the bridge handles that).
    """
    from ..db import get_db

    db = await get_db()

    query: Dict[str, Any] = {
        "user_id": user_id,
        "server_name": server_name,
    }
    if instance_id is not None:
        query["instance_id"] = instance_id

    token_doc = await db.user_api_tokens.find_one(query)

    if not token_doc:
        return None

    expires_at = token_doc.get("expires_at")
    if expires_at:
        if isinstance(expires_at, str):
            try:
                from dateutil import parser
                expires_at = parser.parse(expires_at)
            except Exception:
                pass

        if isinstance(expires_at, datetime):
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=timezone.utc)
            else:
                expires_at = expires_at.astimezone(timezone.utc)

            if datetime.now(timezone.utc) >= expires_at:
                logger.warning("Token for %s expired at %s (user=%s)", server_name, expires_at, user_id[:12])
                return None

    return token_doc
