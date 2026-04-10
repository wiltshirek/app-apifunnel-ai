"""MongoDB connection for the repo search service.

Database: apifunnel_repo_search (configurable via REPO_SEARCH_DB_NAME).
Completely independent from lakehouse / MCP databases.
"""

import logging
from typing import Optional

from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase

from . import config

logger = logging.getLogger(__name__)

_client: Optional[AsyncIOMotorClient] = None
_db: Optional[AsyncIOMotorDatabase] = None


async def get_db() -> AsyncIOMotorDatabase:
    global _client, _db

    if _db is not None:
        return _db

    uri = config.mongodb_uri()
    name = config.db_name()

    _client = AsyncIOMotorClient(
        uri,
        serverSelectionTimeoutMS=10_000,
        connectTimeoutMS=10_000,
        socketTimeoutMS=30_000,
        retryWrites=True,
        w="majority",
    )
    _db = _client[name]

    await _client.admin.command("ping")
    logger.info("Connected to MongoDB (database: %s)", name)
    return _db


async def close_db() -> None:
    global _client, _db
    if _client:
        _client.close()
        _client = None
        _db = None
