"""Lakehouse API — standalone asset storage and retrieval service.

Runs on port 3002. Caddy routes /internal/assets/* and /api/v1/assets/* here.
"""

import logging
import os
from contextlib import asynccontextmanager

from dotenv import load_dotenv

load_dotenv()

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .db import get_db, close_db
from .routes.internal import router as internal_router
from .routes.external import router as external_router

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await get_db()
    logger.info("Lakehouse API started")
    yield
    await close_db()
    logger.info("Lakehouse API shut down")


app = FastAPI(
    title="Lakehouse API",
    version="1.0.0",
    description="Asset storage, retrieval, and full-text search",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://app.apifunnel.ai",
        "http://localhost:3000",
    ],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(internal_router)
app.include_router(external_router)


@app.get("/health")
async def health():
    return {"status": "ok", "service": "lakehouse"}
