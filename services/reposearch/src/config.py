"""Environment-based configuration for the repo search service.

All env vars are prefixed with REPO_SEARCH_ to avoid collisions.
"""

import os


def _require(key: str) -> str:
    val = os.environ.get(key)
    if not val:
        raise RuntimeError(f"{key} is not set")
    return val


def admin_key() -> str:
    return _require("REPO_SEARCH_ADMIN_KEY")


def mongodb_uri() -> str:
    return _require("REPO_SEARCH_MONGODB_URI")


def db_name() -> str:
    return os.environ.get("REPO_SEARCH_DB_NAME", "apifunnel_repo_search")


def s3_endpoint() -> str | None:
    return os.environ.get("REPO_SEARCH_S3_ENDPOINT")


def s3_access_key() -> str:
    return os.environ.get("REPO_SEARCH_S3_ACCESS_KEY", "")


def s3_secret() -> str:
    return os.environ.get("REPO_SEARCH_S3_SECRET", "")


def s3_region() -> str:
    return os.environ.get("REPO_SEARCH_S3_REGION", "hel1")


def s3_bucket() -> str:
    return _require("REPO_SEARCH_S3_BUCKET")
