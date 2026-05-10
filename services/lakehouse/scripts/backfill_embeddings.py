#!/usr/bin/env python3
"""Backfill embeddings for existing assets that have extracted_text but no embedding.

Usage:
    cd services/lakehouse
    OPENAI_API_KEY=sk-... python scripts/backfill_embeddings.py

Or with the repo-root .env already loaded:
    python scripts/backfill_embeddings.py

Options (env vars):
    LAKEHOUSE_MONGODB_URI / MONGODB_URI  — Mongo connection string
    LAKEHOUSE_DB_NAME                    — database name (default: mcp_code_execution_server)
    OPENAI_API_KEY                       — platform OpenAI key
    BACKFILL_BATCH_SIZE                  — documents per batch (default: 50)
    BACKFILL_RPM_LIMIT                   — max requests per minute (default: 2500)
"""

import asyncio
import logging
import os
import sys
import time
from pathlib import Path

# Load the repo-root .env so OPENAI_API_KEY / MONGODB_URI are available
from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parent.parent.parent.parent / ".env")

from motor.motor_asyncio import AsyncIOMotorClient

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger("backfill_embeddings")

# Append the service src to the path so we can import the embeddings module
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from services.embeddings import get_embedding  # noqa: E402


BATCH_SIZE = int(os.environ.get("BACKFILL_BATCH_SIZE", "50"))
RPM_LIMIT = int(os.environ.get("BACKFILL_RPM_LIMIT", "2500"))


async def main():
    uri = os.environ.get("LAKEHOUSE_MONGODB_URI") or os.environ.get("MONGODB_URI")
    if not uri:
        logger.error("LAKEHOUSE_MONGODB_URI or MONGODB_URI must be set")
        sys.exit(1)

    if not os.environ.get("OPENAI_API_KEY"):
        logger.error("OPENAI_API_KEY must be set")
        sys.exit(1)

    db_name = os.environ.get("LAKEHOUSE_DB_NAME", "mcp_code_execution_server")
    client = AsyncIOMotorClient(uri)
    db = client[db_name]

    total = await db.assets.count_documents({
        "extracted_text": {"$exists": True, "$nin": [None, ""]},
        "embedding": {"$exists": False},
    })
    logger.info("Found %d assets needing embeddings", total)

    if total == 0:
        logger.info("Nothing to backfill")
        return

    processed = 0
    failed = 0
    minute_start = time.monotonic()
    calls_this_minute = 0

    cursor = db.assets.find(
        {
            "extracted_text": {"$exists": True, "$nin": [None, ""]},
            "embedding": {"$exists": False},
        },
        {"_id": 1, "extracted_text": 1, "filename": 1},
    ).batch_size(BATCH_SIZE)

    async for doc in cursor:
        # Rate limiting
        if calls_this_minute >= RPM_LIMIT:
            elapsed = time.monotonic() - minute_start
            if elapsed < 60:
                sleep_time = 60 - elapsed + 0.5
                logger.info("Rate limit reached, sleeping %.1fs", sleep_time)
                await asyncio.sleep(sleep_time)
            minute_start = time.monotonic()
            calls_this_minute = 0

        asset_id = doc["_id"]
        text = doc["extracted_text"]

        embedding = await get_embedding(text)
        calls_this_minute += 1

        if embedding is None:
            failed += 1
            logger.warning("Failed to embed %s (%s)", asset_id, doc.get("filename", "?"))
            continue

        await db.assets.update_one(
            {"_id": asset_id},
            {"$set": {"embedding": embedding}},
        )
        processed += 1

        if processed % 100 == 0:
            logger.info("Progress: %d/%d embedded, %d failed", processed, total, failed)

    logger.info("Backfill complete: %d embedded, %d failed out of %d total",
                processed, failed, total)

    client.close()


if __name__ == "__main__":
    asyncio.run(main())
