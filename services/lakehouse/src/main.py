"""Lakehouse API — standalone asset storage and retrieval service.

Runs on port 3002. Caddy routes all traffic here under /api/v1/assets/*.
Requests arriving via /internal/assets/* are rewritten by Caddy before hitting FastAPI.
"""

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import PlainTextResponse

from .db import get_db, close_db
from .routes.external import router as external_router

_OPENAPI_SPEC = Path(__file__).resolve().parent.parent / "openapi" / "lakehouse.yaml"

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

_DEFAULT_ORIGINS = [
    "https://app.apifunnel.ai",
    "http://localhost:3000",
    "http://localhost:4000",
]
_extra = os.environ.get("CORS_EXTRA_ORIGINS", "")
_origins = _DEFAULT_ORIGINS + [o.strip() for o in _extra.split(",") if o.strip()]

app.add_middleware(
    CORSMiddleware,
    allow_origins=_origins,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(external_router)


@app.get("/health")
async def health():
    return {"status": "ok", "service": "lakehouse"}


@app.get("/openapi.yaml", include_in_schema=False)
async def openapi_spec():
    if _OPENAPI_SPEC.exists():
        return PlainTextResponse(_OPENAPI_SPEC.read_text(), media_type="application/yaml")
    return PlainTextResponse("spec not found", status_code=404)
