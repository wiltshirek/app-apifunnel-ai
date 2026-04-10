"""Indexing pipeline: fetch repo → extract chunks → embed → store.

Chunks are the human-readable parts of code: comments, docstrings, READMEs,
and file paths. Raw code is not indexed — that's what GitHub code search is for.
"""

import io
import logging
import re
from datetime import datetime, timezone
from typing import Optional

import httpx
import numpy as np

from ..db import get_db
from ..storage.s3 import upload_vectors, download_vectors
from .github import (
    get_repo_tree,
    get_file_contents_batch,
    get_head_sha,
    get_changed_files,
)

logger = logging.getLogger(__name__)

COLLECTION = "repo_indexes"

# Lazy-loaded embedding model
_model = None


def _get_model():
    global _model
    if _model is None:
        from sentence_transformers import SentenceTransformer
        _model = SentenceTransformer("all-MiniLM-L6-v2")
        logger.info("Loaded embedding model: all-MiniLM-L6-v2")
    return _model


# ── Chunk extraction ────────────────────────────────────────────────────────

_COMMENT_PATTERNS = [
    # Python / Ruby / Shell single-line
    re.compile(r"^\s*#\s?(.+)$", re.MULTILINE),
    # JS / TS / Java / Go / Rust single-line
    re.compile(r"^\s*//\s?(.+)$", re.MULTILINE),
    # Multi-line block comments (C-style)
    re.compile(r"/\*\*?\s*([\s\S]*?)\*/"),
]

_DOCSTRING_PATTERN = re.compile(r'(?:"""([\s\S]*?)"""|\'\'\'([\s\S]*?)\'\'\')')

README_NAMES = frozenset({
    "readme", "readme.md", "readme.txt", "readme.rst",
    "changelog", "changelog.md",
})


def _extract_comments(content: str) -> list[str]:
    comments = []
    for pattern in _COMMENT_PATTERNS:
        for m in pattern.finditer(content):
            text = m.group(1) if m.lastindex else m.group(0)
            text = text.strip()
            if len(text) > 10:
                comments.append(text)
    return comments


def _extract_docstrings(content: str) -> list[str]:
    docstrings = []
    for m in _DOCSTRING_PATTERN.finditer(content):
        text = (m.group(1) or m.group(2) or "").strip()
        if len(text) > 10:
            docstrings.append(text)
    return docstrings


def _extract_chunks_from_file(path: str, content: str) -> list[dict]:
    """Extract indexable chunks from a single file."""
    chunks = []

    # File path is always a chunk
    chunks.append({"text": path, "type": "path", "file_path": path})

    basename = path.rsplit("/", 1)[-1].lower()

    if basename in README_NAMES:
        # Index full README content, split into paragraphs for better embedding
        paragraphs = [p.strip() for p in content.split("\n\n") if len(p.strip()) > 20]
        for para in paragraphs[:50]:
            chunks.append({"text": para, "type": "readme", "file_path": path})
        return chunks

    for ds in _extract_docstrings(content):
        chunks.append({"text": ds, "type": "docstring", "file_path": path})

    for comment in _extract_comments(content):
        chunks.append({"text": comment, "type": "comment", "file_path": path})

    return chunks


# ── Embedding ───────────────────────────────────────────────────────────────

def _embed_texts(texts: list[str]) -> np.ndarray:
    model = _get_model()
    return model.encode(texts, show_progress_bar=False, normalize_embeddings=True)


# ── S3 serialization ───────────────────────────────────────────────────────

def _pack_index(
    embeddings: np.ndarray,
    file_paths: list[str],
    chunks: list[str],
    chunk_types: list[str],
) -> bytes:
    buf = io.BytesIO()
    np.savez_compressed(
        buf,
        embeddings=embeddings.astype(np.float32),
        file_paths=np.array(file_paths, dtype=object),
        chunks=np.array(chunks, dtype=object),
        chunk_types=np.array(chunk_types, dtype=object),
    )
    buf.seek(0)
    return buf.read()


def _unpack_index(data: bytes) -> tuple[np.ndarray, list[str], list[str], list[str]]:
    buf = io.BytesIO(data)
    npz = np.load(buf, allow_pickle=True)
    return (
        npz["embeddings"],
        npz["file_paths"].tolist(),
        npz["chunks"].tolist(),
        npz["chunk_types"].tolist(),
    )


# ── Main pipeline ──────────────────────────────────────────────────────────

def _s3_key(owner: str, repo: str, branch: str) -> str:
    return f"{owner}/{repo}/{branch}.npz"


async def get_index_status(owner: str, repo: str, branch: str = "main") -> Optional[dict]:
    db = await get_db()
    return await db[COLLECTION].find_one(
        {"repo": f"{owner}/{repo}", "branch": branch},
        {"_id": 0},
    )


async def delete_index(owner: str, repo: str, branch: str = "main") -> bool:
    db = await get_db()
    record = await db[COLLECTION].find_one({"repo": f"{owner}/{repo}", "branch": branch})
    if not record:
        return False

    from ..storage.s3 import delete_vectors
    s3_key = record.get("s3_key", _s3_key(owner, repo, branch))
    await delete_vectors(s3_key)
    await db[COLLECTION].delete_one({"_id": record["_id"]})
    return True


async def check_index(
    token: str,
    owner: str,
    repo: str,
    branch: str = "main",
    force: bool = False,
) -> dict:
    """Quick, non-blocking check on index status.

    Returns immediately with one of:
      - {"status": "ready", ...}   — index is current, go search
      - {"status": "current", ...} — index is current, go search
      - {"status": "indexing", ...} — background job running, caller should retry
      - {"status": "started", ...}  — just kicked off, caller should retry

    Never blocks on the actual indexing work.
    """
    import asyncio

    db = await get_db()
    repo_full = f"{owner}/{repo}"
    key = _s3_key(owner, repo, branch)

    existing = await db[COLLECTION].find_one({"repo": repo_full, "branch": branch})

    if existing and existing.get("status") == "indexing":
        return {
            "status": "indexing",
            "message": "Indexing already in progress. Retry in ~30 seconds.",
            "repo": repo_full,
            "estimated_files": existing.get("file_count"),
        }

    async with httpx.AsyncClient(timeout=30.0) as client:
        head_sha = await get_head_sha(client, token, owner, repo, branch)

    if (
        existing
        and not force
        and existing.get("last_indexed_sha") == head_sha
        and existing.get("status") == "ready"
    ):
        return {"status": "current", "repo": repo_full, "index_sha": head_sha}

    # Mark as indexing and fire off background task
    await db[COLLECTION].update_one(
        {"repo": repo_full, "branch": branch},
        {"$set": {"status": "indexing", "updated_at": datetime.now(timezone.utc)}},
        upsert=True,
    )

    base_sha = existing.get("last_indexed_sha") if existing and not force else None
    asyncio.create_task(
        _run_index_background(token, owner, repo, branch, key, head_sha, base_sha)
    )

    tree_count = existing.get("file_count") if existing else None
    return {
        "status": "started",
        "message": "Indexing started. Poll GET /repos/{owner}/{repo} or retry search in ~30 seconds.",
        "repo": repo_full,
        "estimated_files": tree_count,
    }


async def _run_index_background(
    token: str,
    owner: str,
    repo: str,
    branch: str,
    s3_key: str,
    head_sha: str,
    base_sha: Optional[str],
) -> None:
    """Background task that does the actual indexing work."""
    repo_full = f"{owner}/{repo}"
    db = await get_db()
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            if base_sha:
                await _incremental_index(
                    client, token, owner, repo, branch, s3_key,
                    base_sha, head_sha, db,
                )
            else:
                await _full_index(
                    client, token, owner, repo, branch, s3_key, head_sha, db,
                )
        logger.info("Background indexing complete for %s", repo_full)
    except Exception as exc:
        logger.error("Background indexing failed for %s: %s", repo_full, exc)
        await db[COLLECTION].update_one(
            {"repo": repo_full, "branch": branch},
            {"$set": {"status": "error", "error": str(exc)}},
        )


async def _full_index(
    client: httpx.AsyncClient,
    token: str,
    owner: str,
    repo: str,
    branch: str,
    s3_key: str,
    head_sha: str,
    db,
) -> dict:
    repo_full = f"{owner}/{repo}"

    tree = await get_repo_tree(client, token, owner, repo, branch)
    logger.info("Indexing %s: %d files in tree", repo_full, len(tree))

    paths = [f["path"] for f in tree]
    contents = await get_file_contents_batch(client, token, owner, repo, paths, branch)

    all_chunks = []
    for path, content in contents.items():
        all_chunks.extend(_extract_chunks_from_file(path, content))

    # Also add path chunks for files we couldn't fetch content for
    for path in paths:
        if path not in contents:
            all_chunks.append({"text": path, "type": "path", "file_path": path})

    if not all_chunks:
        await db[COLLECTION].update_one(
            {"repo": repo_full, "branch": branch},
            {"$set": {
                "status": "ready", "last_indexed_sha": head_sha,
                "last_indexed_at": datetime.now(timezone.utc),
                "file_count": len(tree), "chunk_count": 0,
                "s3_key": s3_key,
            }},
        )
        return {"status": "ready", "repo": repo_full, "index_sha": head_sha}

    texts = [c["text"] for c in all_chunks]
    embeddings = _embed_texts(texts)

    data = _pack_index(
        embeddings,
        [c["file_path"] for c in all_chunks],
        texts,
        [c["type"] for c in all_chunks],
    )

    await upload_vectors(s3_key, data)

    await db[COLLECTION].update_one(
        {"repo": repo_full, "branch": branch},
        {"$set": {
            "status": "ready",
            "last_indexed_sha": head_sha,
            "last_indexed_at": datetime.now(timezone.utc),
            "file_count": len(tree),
            "chunk_count": len(all_chunks),
            "s3_key": s3_key,
        }},
    )

    logger.info("Indexed %s: %d chunks from %d files", repo_full, len(all_chunks), len(contents))
    return {"status": "ready", "repo": repo_full, "index_sha": head_sha}


async def _incremental_index(
    client: httpx.AsyncClient,
    token: str,
    owner: str,
    repo: str,
    branch: str,
    s3_key: str,
    base_sha: str,
    head_sha: str,
    db,
) -> dict:
    repo_full = f"{owner}/{repo}"

    changes = await get_changed_files(client, token, owner, repo, base_sha, head_sha)
    changed_paths = set(changes["added"] + changes["modified"])
    removed_paths = set(changes["removed"])

    if not changed_paths and not removed_paths:
        await db[COLLECTION].update_one(
            {"repo": repo_full, "branch": branch},
            {"$set": {"status": "ready", "last_indexed_sha": head_sha}},
        )
        return {"status": "current", "repo": repo_full, "index_sha": head_sha}

    # Load existing index
    existing_data = await download_vectors(s3_key)
    if not existing_data:
        return await _full_index(client, token, owner, repo, branch, s3_key, head_sha, db)

    old_embeddings, old_paths, old_chunks, old_types = _unpack_index(existing_data)

    # Filter out chunks for removed/changed files
    affected_paths = changed_paths | removed_paths
    keep_mask = [p not in affected_paths for p in old_paths]

    kept_embeddings = old_embeddings[keep_mask] if any(keep_mask) else np.empty((0, 384), dtype=np.float32)
    kept_paths = [p for p, k in zip(old_paths, keep_mask) if k]
    kept_chunks = [c for c, k in zip(old_chunks, keep_mask) if k]
    kept_types = [t for t, k in zip(old_types, keep_mask) if k]

    # Fetch and process changed files
    new_all_chunks = []
    if changed_paths:
        contents = await get_file_contents_batch(
            client, token, owner, repo, list(changed_paths), branch,
        )
        for path, content in contents.items():
            new_all_chunks.extend(_extract_chunks_from_file(path, content))

    if new_all_chunks:
        new_texts = [c["text"] for c in new_all_chunks]
        new_embeddings = _embed_texts(new_texts)

        all_embeddings = np.vstack([kept_embeddings, new_embeddings]) if len(kept_embeddings) > 0 else new_embeddings
        all_paths = kept_paths + [c["file_path"] for c in new_all_chunks]
        all_chunks_text = kept_chunks + new_texts
        all_types = kept_types + [c["type"] for c in new_all_chunks]
    else:
        all_embeddings = kept_embeddings
        all_paths = kept_paths
        all_chunks_text = kept_chunks
        all_types = kept_types

    data = _pack_index(all_embeddings, all_paths, all_chunks_text, all_types)
    await upload_vectors(s3_key, data)

    tree = await get_repo_tree(client, token, owner, repo, branch)

    await db[COLLECTION].update_one(
        {"repo": repo_full, "branch": branch},
        {"$set": {
            "status": "ready",
            "last_indexed_sha": head_sha,
            "last_indexed_at": datetime.now(timezone.utc),
            "file_count": len(tree),
            "chunk_count": len(all_paths),
            "s3_key": s3_key,
        }},
    )

    logger.info(
        "Incremental index %s: +%d -%d changed, %d total chunks",
        repo_full, len(new_all_chunks), len(removed_paths), len(all_paths),
    )
    return {"status": "ready", "repo": repo_full, "index_sha": head_sha}
