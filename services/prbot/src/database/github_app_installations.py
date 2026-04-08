"""MongoDB CRUD for GitHub App installations.

Collection: github_app_installations

One document per installation. A user may have multiple installations
(one per GitHub account/org they install the app on).

Key lookup pattern: given (user_id, repo) → installation_id.

User linkage strategy:
  - Primary path: install-callback receives state=user_id from GitHub redirect,
    calls link_installation_by_id immediately after install.
  - Fallback: get_installation_for_repo does a repo-only lookup and auto-links
    user_id from the dispatch JWT on first call (covers direct GitHub installs
    where state param was not set).
"""

import logging
from typing import Any, Dict, List, Optional

from ..db import get_db as get_mongodb

logger = logging.getLogger(__name__)

COLLECTION = "github_app_installations"


async def upsert_installation(
    installation_id: int,
    account_login: str,
    account_type: str,
    user_id: Optional[str],
    repos: List[str],
    permissions: Dict[str, str],
) -> None:
    """Create or update an installation record.

    Called from the webhook handler on installation.created / installation.added.

    Args:
        installation_id: GitHub's installation ID.
        account_login: GitHub account that installed the app (user or org login).
        account_type: "User" or "Organization".
        user_id: Our platform user_id (linked via OAuth account matching).
                 May be None on first webhook if we haven't matched yet.
        repos: List of "owner/repo" strings the app can access.
        permissions: Dict of permission name → access level.
    """
    db = await get_mongodb()
    if db is None:
        logger.error("github_app_installations.upsert: no DB connection")
        return

    await db[COLLECTION].update_one(
        {"installation_id": installation_id},
        {"$set": {
            "installation_id": installation_id,
            "account_login": account_login,
            "account_type": account_type,
            "user_id": user_id,
            "repos": repos,
            "permissions": permissions,
            "suspended": False,
        }},
        upsert=True,
    )
    logger.info(
        "github_app_installations: upserted installation_id=%s account=%s repos=%s",
        installation_id, account_login, repos,
    )


async def add_repos(installation_id: int, repos: List[str]) -> None:
    """Append repos to an existing installation (installation_repositories.added)."""
    db = await get_mongodb()
    if db is None:
        return
    await db[COLLECTION].update_one(
        {"installation_id": installation_id},
        {"$addToSet": {"repos": {"$each": repos}}},
    )


async def remove_repos(installation_id: int, repos: List[str]) -> None:
    """Remove repos from an installation (installation_repositories.removed)."""
    db = await get_mongodb()
    if db is None:
        return
    await db[COLLECTION].update_one(
        {"installation_id": installation_id},
        {"$pull": {"repos": {"$in": repos}}},
    )


async def delete_installation(installation_id: int) -> None:
    """Remove an installation record (installation.deleted)."""
    db = await get_mongodb()
    if db is None:
        return
    await db[COLLECTION].delete_one({"installation_id": installation_id})
    logger.info("github_app_installations: deleted installation_id=%s", installation_id)


async def set_suspended(installation_id: int, suspended: bool) -> None:
    """Mark an installation as suspended or active."""
    db = await get_mongodb()
    if db is None:
        return
    await db[COLLECTION].update_one(
        {"installation_id": installation_id},
        {"$set": {"suspended": suspended}},
    )


async def get_installation_for_repo(
    user_id: str,
    repo: str,
) -> Optional[Dict[str, Any]]:
    """Find the installation that covers the given repo for this user.

    Lookup strategy (in order):
      1. Fast path: installation already linked to this user_id + repo.
      2. Fallback: installation covers this repo but user_id not yet set
         (webhook arrived, callback/linking hasn't happened yet).
         → auto-link user_id now using the caller's JWT identity.

    Returns None if the app is not installed on the repo at all, or if
    the installation is suspended.

    Args:
        user_id: Platform user ID from JWT.
        repo: "owner/repo" string.
    """
    db = await get_mongodb()
    if db is None:
        return None

    # 1. Fast path — already linked
    doc = await db[COLLECTION].find_one({
        "user_id": user_id,
        "repos": repo,
        "suspended": {"$ne": True},
    })
    if doc:
        return doc

    # 2. Fallback — repo covered but user_id not linked yet
    doc = await db[COLLECTION].find_one({
        "user_id": None,
        "repos": repo,
        "suspended": {"$ne": True},
    })
    if doc:
        # Auto-link: the first authenticated user to dispatch to this repo claims it.
        await db[COLLECTION].update_one(
            {"_id": doc["_id"]},
            {"$set": {"user_id": user_id}},
        )
        doc["user_id"] = user_id
        logger.info(
            "github_app_installations: auto-linked user_id=%s to installation_id=%s via repo=%s",
            user_id, doc["installation_id"], repo,
        )

    return doc


async def link_installation_by_id(
    installation_id: int,
    user_id: str,
) -> bool:
    """Link a specific installation to a platform user_id.

    Called from the install-callback route when GitHub redirects back
    after installation with state=user_id. This is the primary linkage
    path for users who install via the platform-generated link.

    Uses upsert so this works even if the webhook hasn't arrived yet
    (GitHub fires the callback and webhook nearly simultaneously — the
    callback can win the race). The webhook's upsert_installation will
    fill in the remaining fields when it arrives.

    Returns True if the document was created or updated.
    """
    db = await get_mongodb()
    if db is None:
        return False

    result = await db[COLLECTION].update_one(
        {"installation_id": installation_id},
        {"$set": {"user_id": user_id, "installation_id": installation_id}},
        upsert=True,
    )
    changed = result.modified_count > 0 or result.upserted_id is not None
    if changed:
        logger.info(
            "github_app_installations: linked installation_id=%s to user_id=%s (upserted=%s)",
            installation_id, user_id, result.upserted_id is not None,
        )
    return changed
