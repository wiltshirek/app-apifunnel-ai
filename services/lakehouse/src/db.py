"""MongoDB connection for the lakehouse service.

Connects to the same MongoDB cluster as the bridge, targeting the
same database (default: mcp_code_execution_server) and the `assets` collection.

Env vars:
    LAKEHOUSE_MONGODB_URI: Connection string (falls back to MONGODB_URI)
    LAKEHOUSE_DB_NAME: Database name (default: mcp_code_execution_server)
"""

import logging
import os
from typing import Optional

from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase

logger = logging.getLogger(__name__)

_client: Optional[AsyncIOMotorClient] = None
_db: Optional[AsyncIOMotorDatabase] = None


async def get_db() -> AsyncIOMotorDatabase:
    global _client, _db

    if _db is not None:
        return _db

    uri = os.environ.get("LAKEHOUSE_MONGODB_URI") or os.environ.get("MONGODB_URI")
    if not uri:
        raise RuntimeError("LAKEHOUSE_MONGODB_URI (or MONGODB_URI) is not set")

    db_name = os.environ.get("LAKEHOUSE_DB_NAME", "mcp_code_execution_server")

    _client = AsyncIOMotorClient(
        uri,
        serverSelectionTimeoutMS=10_000,
        connectTimeoutMS=10_000,
        socketTimeoutMS=30_000,
        retryWrites=True,
        w="majority",
    )
    _db = _client[db_name]

    await _client.admin.command("ping")
    logger.info("Connected to MongoDB (database: %s)", db_name)
    return _db


async def close_db() -> None:
    global _client, _db
    if _client:
        _client.close()
        _client = None
        _db = None
