"""Hetzner S3-compatible object storage client.

Env vars:
    HETZNER_S3_ENDPOINT, HETZNER_S3_ACCESS_KEY, HETZNER_S3_SECRET,
    HETZNER_S3_REGION (default hel1), HETZNER_S3_ASSETS_BUCKET (required for uploads)
"""

import io
import logging
import os
from typing import Any, BinaryIO, Dict, Optional

logger = logging.getLogger(__name__)

_client: Optional[Any] = None


def _get_client():
    global _client
    if _client is not None:
        return _client

    endpoint = os.environ.get("HETZNER_S3_ENDPOINT")
    if not endpoint:
        logger.debug("HETZNER_S3_ENDPOINT not set — S3 disabled")
        return None

    import boto3
    from botocore.config import Config

    _client = boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=os.environ.get("HETZNER_S3_ACCESS_KEY", "").strip(),
        aws_secret_access_key=os.environ.get("HETZNER_S3_SECRET", "").strip(),
        region_name=os.environ.get("HETZNER_S3_REGION", "hel1"),
        config=Config(signature_version="s3v4"),
    )
    logger.info("Connected to Hetzner S3 (%s)", endpoint)
    return _client


def _bucket() -> str:
    bucket = os.environ.get("HETZNER_S3_ASSETS_BUCKET")
    if not bucket:
        raise RuntimeError("HETZNER_S3_ASSETS_BUCKET not set")
    return bucket


async def upload_file(
    file_obj: BinaryIO,
    s3_key: str,
    content_type: str,
    metadata: Optional[Dict[str, str]] = None,
) -> bool:
    client = _get_client()
    if not client:
        return False
    try:
        extra: Dict[str, Any] = {"ContentType": content_type}
        if metadata:
            extra["Metadata"] = {k: str(v) for k, v in metadata.items()}
        client.upload_fileobj(file_obj, _bucket(), s3_key, ExtraArgs=extra)
        return True
    except Exception as exc:
        logger.error("S3 upload failed for %s: %s", s3_key, exc)
        return False


async def download_file(s3_key: str) -> Optional[bytes]:
    client = _get_client()
    if not client:
        return None
    try:
        buf = io.BytesIO()
        client.download_fileobj(_bucket(), s3_key, buf)
        buf.seek(0)
        return buf.read()
    except Exception as exc:
        logger.error("S3 download failed for %s: %s", s3_key, exc)
        return None


async def get_presigned_url(s3_key: str, expires_in: int = 300) -> Optional[str]:
    client = _get_client()
    if not client:
        return None
    try:
        return client.generate_presigned_url(
            "get_object",
            Params={"Bucket": _bucket(), "Key": s3_key},
            ExpiresIn=expires_in,
        )
    except Exception as exc:
        logger.error("Presigned URL failed for %s: %s", s3_key, exc)
        return None


async def delete_file(s3_key: str) -> bool:
    client = _get_client()
    if not client:
        return False
    try:
        client.delete_object(Bucket=_bucket(), Key=s3_key)
        return True
    except Exception as exc:
        logger.error("S3 delete failed for %s: %s", s3_key, exc)
        return False
