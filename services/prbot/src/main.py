"""PR Bot API — workspace dispatch, preflight, and job-secrets service.

Runs on port 3003. Handles GitHub Actions workflow dispatch for the PR agent,
pre-flight checks, job-secret exchange, GitHub App webhooks, and install callbacks.
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
from .routes.external import router as api_router

_OPENAPI_SPEC = Path(__file__).resolve().parent.parent / "openapi" / "prbot.yaml"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await get_db()
    from .database.run_reports import ensure_indexes
    await ensure_indexes()
    logger.info("PR Bot API started")
    yield
    await close_db()
    logger.info("PR Bot API shut down")


app = FastAPI(
    title="PR Bot API",
    version="1.0.0",
    description="Workspace dispatch, preflight, and job-secrets for the PR agent",
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

app.include_router(api_router)


@app.get("/health")
async def health():
    return {"status": "ok", "service": "prbot"}


@app.get("/openapi.yaml", include_in_schema=False)
async def openapi_spec():
    if _OPENAPI_SPEC.exists():
        return PlainTextResponse(_OPENAPI_SPEC.read_text(), media_type="application/yaml")
    return PlainTextResponse("spec not found", status_code=404)
