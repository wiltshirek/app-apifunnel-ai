"""Search engine: embed query → cosine similarity → top-k.

Pure numpy — no FAISS or vector DB needed at this scale.
Sub-millisecond for 10K chunks.
"""

import logging
from typing import Optional

import numpy as np

from ..storage.s3 import download_vectors
from .indexer import _get_model, _unpack_index, get_index_status, _s3_key

logger = logging.getLogger(__name__)


async def search(
    owner: str,
    repo: str,
    query: str,
    branch: str = "main",
    top_k: int = 10,
) -> Optional[list[dict]]:
    """Search the vector index for a repo.

    Returns None if the repo isn't indexed.
    Returns a list of {file_path, chunk, chunk_type, score} dicts.
    """
    status = await get_index_status(owner, repo, branch)
    if not status or status.get("status") != "ready":
        return None

    s3_key = status.get("s3_key", _s3_key(owner, repo, branch))
    data = await download_vectors(s3_key)
    if not data:
        logger.error("Index record exists but S3 data missing for %s/%s", owner, repo)
        return None

    embeddings, file_paths, chunks, chunk_types = _unpack_index(data)

    if len(embeddings) == 0:
        return []

    model = _get_model()
    query_vec = model.encode([query], normalize_embeddings=True)

    scores = np.dot(embeddings, query_vec.T).flatten()

    top_k = min(top_k, len(scores))
    top_indices = np.argpartition(scores, -top_k)[-top_k:]
    top_indices = top_indices[np.argsort(scores[top_indices])[::-1]]

    results = []
    for idx in top_indices:
        score = float(scores[idx])
        if score < 0.1:
            continue
        results.append({
            "file_path": file_paths[idx],
            "chunk": chunks[idx],
            "chunk_type": chunk_types[idx],
            "score": round(score, 4),
        })

    return results
