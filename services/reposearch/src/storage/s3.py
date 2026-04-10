"""S3 storage for vector index .npz files.

Uses REPO_SEARCH_S3_* env vars — fully independent from lakehouse S3 config.
"""

import io
import logging
from typing import Any, Optional

from .. import config

logger = logging.getLogger(__name__)

_client: Optional[Any] = None


def _get_client():
    global _client
    if _client is not None:
        return _client

    endpoint = config.s3_endpoint()
    if not endpoint:
        logger.debug("REPO_SEARCH_S3_ENDPOINT not set — S3 disabled")
        return None

    import boto3
    from botocore.config import Config

    _client = boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=config.s3_access_key().strip(),
        aws_secret_access_key=config.s3_secret().strip(),
        region_name=config.s3_region(),
        config=Config(signature_version="s3v4"),
    )
    logger.info("Connected to S3 (%s)", endpoint)
    return _client


def _bucket() -> str:
    return config.s3_bucket()


async def upload_vectors(s3_key: str, data: bytes) -> bool:
    client = _get_client()
    if not client:
        return False
    try:
        client.upload_fileobj(
            io.BytesIO(data),
            _bucket(),
            s3_key,
            ExtraArgs={"ContentType": "application/octet-stream"},
        )
        logger.info("Uploaded vectors to s3://%s/%s (%d bytes)", _bucket(), s3_key, len(data))
        return True
    except Exception as exc:
        logger.error("S3 upload failed for %s: %s", s3_key, exc)
        return False


async def download_vectors(s3_key: str) -> Optional[bytes]:
    client = _get_client()
    if not client:
        return None
    try:
        buf = io.BytesIO()
        client.download_fileobj(_bucket(), s3_key, buf)
        buf.seek(0)
        return buf.read()
    except client.exceptions.NoSuchKey:
        return None
    except Exception as exc:
        logger.error("S3 download failed for %s: %s", s3_key, exc)
        return None


async def delete_vectors(s3_key: str) -> bool:
    client = _get_client()
    if not client:
        return False
    try:
        client.delete_object(Bucket=_bucket(), Key=s3_key)
        return True
    except Exception as exc:
        logger.error("S3 delete failed for %s: %s", s3_key, exc)
        return False
