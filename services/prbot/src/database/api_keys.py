"""Service API Keys — encryption, decryption, and lookup.

AES-256-GCM encrypted at rest. Format: base64(iv):base64(ciphertext):base64(tag)
"""

import base64
import logging
import os
from typing import Optional

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

logger = logging.getLogger(__name__)


def get_encryption_key() -> bytes:
    """Get 32-byte encryption key from ENCRYPTION_KEY env var."""
    key_b64 = os.environ.get("ENCRYPTION_KEY")
    if not key_b64:
        raise ValueError("ENCRYPTION_KEY environment variable not set.")

    try:
        key = base64.b64decode(key_b64)
        if len(key) != 32:
            raise ValueError(f"ENCRYPTION_KEY must be 32 bytes (got {len(key)})")
        return key
    except Exception as e:
        raise ValueError(f"Invalid ENCRYPTION_KEY format: {e}")


def decrypt_api_key(encrypted_value: str, encryption_key: bytes) -> str:
    """Decrypt AES-256-GCM encrypted API key (iv:ciphertext:tag)."""
    try:
        parts = encrypted_value.split(":")
        if len(parts) != 3:
            raise ValueError("Invalid encrypted format (expected iv:ciphertext:tag)")

        iv = base64.b64decode(parts[0])
        ciphertext = base64.b64decode(parts[1])
        tag = base64.b64decode(parts[2])

        ciphertext_with_tag = ciphertext + tag
        aesgcm = AESGCM(encryption_key)
        plaintext = aesgcm.decrypt(iv, ciphertext_with_tag, None)

        return plaintext.decode("utf-8")
    except Exception as e:
        raise ValueError(f"Failed to decrypt API key: {e}")


def encrypt_api_key(plaintext_key: str, encryption_key: bytes) -> str:
    """Encrypt API key with AES-256-GCM. Returns iv:ciphertext:tag."""
    aesgcm = AESGCM(encryption_key)
    iv = os.urandom(12)

    ciphertext_with_tag = aesgcm.encrypt(iv, plaintext_key.encode("utf-8"), None)

    ciphertext = ciphertext_with_tag[:-16]
    tag = ciphertext_with_tag[-16:]

    iv_b64 = base64.b64encode(iv).decode("utf-8")
    ciphertext_b64 = base64.b64encode(ciphertext).decode("utf-8")
    tag_b64 = base64.b64encode(tag).decode("utf-8")

    return f"{iv_b64}:{ciphertext_b64}:{tag_b64}"


async def get_service_api_key(user_id: str, service_name: str) -> Optional[str]:
    """Retrieve and decrypt a single service API key."""
    from ..db import get_db

    try:
        db = await get_db()
    except Exception:
        logger.debug("MongoDB not available, skipping service API key lookup")
        return None

    try:
        encryption_key = get_encryption_key()
    except ValueError as e:
        logger.warning("Cannot decrypt service API keys: %s", e)
        return None

    doc = await db.service_api_keys.find_one({
        "user_id": user_id,
        "service_name": service_name,
    })
    if not doc:
        return None

    encrypted_key = doc.get("api_key_encrypted")
    if not encrypted_key:
        return None

    try:
        return decrypt_api_key(encrypted_key, encryption_key)
    except ValueError as e:
        logger.error("Failed to decrypt key for service %s (user=%s): %s", service_name, user_id, e)
        return None
