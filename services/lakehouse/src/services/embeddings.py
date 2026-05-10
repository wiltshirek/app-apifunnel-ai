"""Embedding generation for hybrid semantic search.

Platform infrastructure — uses OPENAI_API_KEY from the environment,
not user-scoped JWT api_settings.
"""

import logging
import os
from typing import List, Optional

logger = logging.getLogger(__name__)

_MODEL = "text-embedding-3-small"
_DIMENSIONS = 1536
_MAX_TOKENS = 8191

_client = None
_enc = None


def _get_client():
    global _client
    if _client is None:
        from openai import AsyncOpenAI

        api_key = os.environ.get("OPENAI_API_KEY")
        if not api_key:
            return None
        _client = AsyncOpenAI(api_key=api_key)
    return _client


def _get_encoder():
    global _enc
    if _enc is None:
        try:
            import tiktoken
            _enc = tiktoken.encoding_for_model(_MODEL)
        except Exception:
            pass
    return _enc


def _truncate_text(text: str, max_tokens: int = _MAX_TOKENS) -> str:
    """Truncate text to fit within the embedding model's token window."""
    enc = _get_encoder()
    if enc is not None:
        tokens = enc.encode(text)
        if len(tokens) <= max_tokens:
            return text
        return enc.decode(tokens[:max_tokens])
    # Rough fallback: ~4 chars per token
    return text[: max_tokens * 4]


async def get_embedding(text: str) -> Optional[List[float]]:
    """Generate an embedding vector for the given text.

    Returns None if the API key is missing or the call fails, allowing
    callers to degrade gracefully (asset still searchable via keyword).
    """
    client = _get_client()
    if client is None:
        logger.warning("OPENAI_API_KEY not set — skipping embedding generation")
        return None

    if not text:
        return None

    text = text.strip()
    if not text:
        return None

    text = _truncate_text(text)

    try:
        response = await client.embeddings.create(
            model=_MODEL,
            input=text,
            dimensions=_DIMENSIONS,
        )
        return response.data[0].embedding
    except Exception as exc:
        logger.error("Embedding generation failed: %s", exc)
        return None
