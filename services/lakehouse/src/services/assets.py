"""Asset storage service — upload, retrieval, search, delete.

Binaries stored in Hetzner S3, metadata + extracted text in MongoDB `assets` collection.
Full-text search via MongoDB text index on `extracted_text`.
"""

import base64
import hashlib
import io
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

THUMBNAIL_MAX_SIZE = 200
THUMBNAIL_DPI = 72

_TEXT_PREFIXES = ("text/",)
_TEXT_EXACT = frozenset({
    "application/json", "application/xml", "application/yaml",
    "application/x-yaml", "application/csv",
})


def _is_text_type(content_type: str) -> bool:
    return content_type.startswith(_TEXT_PREFIXES) or content_type in _TEXT_EXACT


def _generate_asset_id() -> str:
    try:
        from nanoid import generate as nanoid
        return f"ast_{nanoid(size=12)}"
    except ImportError:
        import uuid
        return f"ast_{uuid.uuid4().hex[:12]}"


async def detect_content_type(file_bytes: bytes, filename: str) -> str:
    detected = None
    try:
        import magic
        detected = magic.Magic(mime=True).from_buffer(file_bytes[:2048])
    except (ImportError, Exception):
        pass

    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    ext_map = {
        "pdf": "application/pdf", "png": "image/png", "jpg": "image/jpeg",
        "jpeg": "image/jpeg", "gif": "image/gif", "webp": "image/webp",
        "svg": "image/svg+xml", "txt": "text/plain", "md": "text/markdown",
        "json": "application/json", "csv": "text/csv", "html": "text/html",
        "xml": "application/xml", "doc": "application/msword",
        "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "xls": "application/vnd.ms-excel",
        "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "zip": "application/zip", "dxf": "application/dxf",
        "py": "text/x-python", "js": "text/javascript", "ts": "text/typescript",
        "yaml": "text/yaml", "yml": "text/yaml", "sql": "text/x-sql",
    }
    ext_type = ext_map.get(ext, "application/octet-stream")

    if not detected:
        return ext_type
    if detected == "application/octet-stream" and ext_type.startswith("text/"):
        return ext_type
    if ext in ext_map and detected in ("text/plain", "application/octet-stream"):
        return ext_type
    return detected


async def _generate_thumbnail(file_bytes: bytes, content_type: str) -> Optional[bytes]:
    try:
        from PIL import Image

        if content_type == "application/pdf":
            try:
                import fitz
                doc = fitz.open(stream=file_bytes, filetype="pdf")
                if len(doc) == 0:
                    doc.close()
                    return None
                pix = doc[0].get_pixmap(dpi=THUMBNAIL_DPI)
                img = Image.open(io.BytesIO(pix.tobytes("png")))
                doc.close()
            except ImportError:
                return None
        elif content_type.startswith("image/"):
            img = Image.open(io.BytesIO(file_bytes))
            if img.mode in ("RGBA", "P", "LA"):
                bg = Image.new("RGB", img.size, (255, 255, 255))
                if img.mode == "P":
                    img = img.convert("RGBA")
                bg.paste(img, mask=img.split()[-1] if img.mode == "RGBA" else None)
                img = bg
            elif img.mode != "RGB":
                img = img.convert("RGB")
        else:
            return None

        img.thumbnail((THUMBNAIL_MAX_SIZE, THUMBNAIL_MAX_SIZE), Image.Resampling.LANCZOS)
        out = io.BytesIO()
        img.save(out, format="PNG", optimize=True)
        out.seek(0)
        return out.read()
    except Exception:
        return None


async def _extract_pdf_text(file_bytes: bytes) -> Dict[str, Any]:
    try:
        import fitz
        doc = fitz.open(stream=file_bytes, filetype="pdf")
        pages = []
        needs_ocr = False

        for i, page in enumerate(doc):
            text = page.get_text()
            if not text.strip():
                needs_ocr = True
            pages.append({"num": i + 1, "text": text, "char_count": len(text)})

        if needs_ocr:
            logger.info("PDF has %d blank page(s), attempting OCR...", sum(1 for p in pages if not p["text"].strip()))
            ocr_pages = []
            for i, page in enumerate(doc):
                if pages[i]["text"].strip():
                    ocr_pages.append(pages[i])
                    continue
                try:
                    tp = page.get_textpage_ocr(language="eng", dpi=300, full=True)
                    text = page.get_text(textpage=tp)
                    ocr_pages.append({"num": i + 1, "text": text, "char_count": len(text)})
                except Exception as ocr_exc:
                    logger.warning("OCR failed on page %d: %s", i + 1, ocr_exc)
                    ocr_pages.append(pages[i])
            pages = ocr_pages

        page_count = len(doc)
        doc.close()
        return {
            "page_count": page_count,
            "pages": pages,
            "full_text": "\n\n".join(p["text"] for p in pages),
        }
    except Exception as exc:
        logger.error("PDF text extraction failed: %s", exc)
        return {"page_count": 0, "pages": [], "full_text": ""}


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------

async def upload_asset(
    db,
    file_bytes: bytes,
    filename: str,
    user_id: str,
    tenant_id: Optional[str] = None,
    subagent_task_id: Optional[str] = None,
    scheduled_task_id: Optional[str] = None,
    client_meta: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    from ..storage.s3 import upload_file, get_presigned_url

    content_type = await detect_content_type(file_bytes, filename)
    asset_id = _generate_asset_id()
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else "bin"
    s3_key = f"{user_id}/{asset_id}/original.{ext}"
    checksum = hashlib.sha256(file_bytes).hexdigest()

    ok = await upload_file(io.BytesIO(file_bytes), s3_key, content_type, {"asset_id": asset_id, "user_id": user_id})
    if not ok:
        return {"error": "S3 upload failed", "asset_id": asset_id}

    thumbnail_s3_key = None
    thumb_bytes = await _generate_thumbnail(file_bytes, content_type)
    if thumb_bytes:
        thumb_key = f"{user_id}/{asset_id}/thumb.png"
        if await upload_file(io.BytesIO(thumb_bytes), thumb_key, "image/png", {"asset_id": asset_id, "type": "thumbnail"}):
            thumbnail_s3_key = thumb_key

    document_metadata = None
    extracted_text = None
    page_text_store = None

    if content_type == "application/pdf":
        pdf = await _extract_pdf_text(file_bytes)
        document_metadata = {"page_count": pdf["page_count"], "pages": [{"num": p["num"], "char_count": p["char_count"]} for p in pdf["pages"]]}
        extracted_text = pdf["full_text"]
        page_text_store = {str(p["num"]): p["text"] for p in pdf["pages"]}
    elif _is_text_type(content_type):
        try:
            extracted_text = file_bytes.decode("utf-8")
        except UnicodeDecodeError:
            pass

    now = datetime.utcnow()
    asset_doc: Dict[str, Any] = {
        "_id": asset_id,
        "user_id": user_id,
        "tenant_id": tenant_id,
        "filename": filename,
        "content_type": content_type,
        "size_bytes": len(file_bytes),
        "checksum_sha256": checksum,
        "s3_key": s3_key,
        "thumbnail_s3_key": thumbnail_s3_key,
        "document": document_metadata,
        "extracted_text": extracted_text,
        "page_texts": page_text_store,
        "processing_status": "complete",
        "created_at": now,
        "updated_at": now,
    }
    if subagent_task_id:
        asset_doc["subagent_task_id"] = subagent_task_id
    if scheduled_task_id:
        asset_doc["scheduled_task_id"] = scheduled_task_id
    if client_meta:
        asset_doc["client_meta"] = client_meta

    await db.assets.insert_one(asset_doc)

    thumbnail_url = None
    if thumbnail_s3_key:
        thumbnail_url = await get_presigned_url(thumbnail_s3_key, expires_in=3600)

    snippet = None
    if extracted_text:
        preview = extracted_text[:200].strip()
        if len(extracted_text) > 200:
            last_sp = preview.rfind(" ")
            if last_sp > 100:
                preview = preview[:last_sp]
            snippet = preview + "..."
        else:
            snippet = preview

    resp: Dict[str, Any] = {
        "asset_id": asset_id, "filename": filename, "content_type": content_type,
        "size_bytes": len(file_bytes), "checksum_sha256": checksum, "s3_key": s3_key,
        "thumbnail_url": thumbnail_url, "snippet": snippet, "tenant_id": tenant_id,
        "status": "complete", "created_at": now.isoformat(),
    }
    if content_type == "application/pdf" and document_metadata:
        resp["page_count"] = document_metadata["page_count"]
    if subagent_task_id:
        resp["subagent_task_id"] = subagent_task_id
    if scheduled_task_id:
        resp["scheduled_task_id"] = scheduled_task_id
    if client_meta:
        resp["client_meta"] = client_meta
    return resp


async def get_asset(db, asset_id: str, user_id: str, tenant_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
    if not user_id:
        return None
    query: Dict[str, Any] = {"_id": asset_id, "user_id": user_id}
    if tenant_id:
        query["tenant_id"] = tenant_id

    asset = await db.assets.find_one(query)
    if asset:
        asset["asset_id"] = asset.pop("_id")
    return asset


async def list_assets(
    db,
    user_id: str,
    content_type: Optional[str] = None,
    tenant_id: Optional[str] = None,
    limit: int = 50,
    cursor: Optional[str] = None,
    tags: Optional[str] = None,
    query: Optional[str] = None,
) -> Dict[str, Any]:
    from ..storage.s3 import get_presigned_url
    import re as _re

    if not user_id:
        return {"assets": [], "next_cursor": None, "has_more": False}

    limit = max(1, limit)
    mongo_filter: Dict[str, Any] = {"user_id": user_id}
    if content_type:
        mongo_filter["content_type"] = content_type
    if tenant_id:
        mongo_filter["tenant_id"] = tenant_id
    if tags:
        mongo_filter["tags"] = tags
    else:
        mongo_filter["tags"] = {"$ne": "code_script"}
    if query:
        mongo_filter["filename"] = {"$regex": _re.escape(query), "$options": "i"}

    if cursor:
        try:
            from datetime import datetime as _dt, timezone
            cursor_dt = _dt.fromisoformat(cursor.replace("Z", "+00:00"))
            mongo_filter["created_at"] = {"$lt": cursor_dt}
        except (ValueError, AttributeError):
            return {"assets": [], "next_cursor": None, "has_more": False,
                    "error": f"Invalid cursor: expected ISO datetime, got '{cursor}'"}

    mongo_filter["$or"] = [
        {"session_id": {"$exists": False}},
        {"is_ephemeral": False},
    ]

    raw = await db.assets.find(mongo_filter).sort("created_at", -1).limit(limit + 1).to_list(length=limit + 1)
    has_more = len(raw) > limit
    if has_more:
        raw = raw[:limit]
    next_cursor = raw[-1]["created_at"].isoformat() if has_more and raw else None

    assets = []
    for a in raw:
        d: Dict[str, Any] = {
            "asset_id": a["_id"],
            "filename": a.get("filename"),
            "content_type": a.get("content_type"),
            "size_bytes": a.get("size_bytes"),
            "tenant_id": a.get("tenant_id"),
            "created_at": a["created_at"].isoformat() if a.get("created_at") else None,
        }
        ct = a.get("content_type", "")
        if ct == "application/pdf" and a.get("document"):
            d["page_count"] = a["document"].get("page_count")

        thumb_key = a.get("thumbnail_s3_key")
        if thumb_key:
            d["thumbnail_url"] = await get_presigned_url(thumb_key, expires_in=3600)

        for field in ("subagent_task_id", "scheduled_task_id", "session_id", "source", "artifact_type", "tags", "client_meta"):
            if a.get(field):
                d[field] = a[field]
        assets.append(d)

    return {"assets": assets, "next_cursor": next_cursor, "has_more": has_more}


async def search_assets(
    db,
    user_id: str,
    query_text: str,
    content_type: Optional[str] = None,
    tenant_id: Optional[str] = None,
    limit: int = 20,
) -> List[Dict[str, Any]]:
    if not user_id:
        return []
    limit = min(limit, 100)
    match: Dict[str, Any] = {"$text": {"$search": query_text}, "user_id": user_id}
    if content_type:
        match["content_type"] = content_type
    if tenant_id:
        match["tenant_id"] = tenant_id

    pipeline = [
        {"$match": match},
        {"$addFields": {"score": {"$meta": "textScore"}}},
        {"$sort": {"score": -1}},
        {"$limit": limit},
        {"$project": {
            "_id": 1, "filename": 1, "content_type": 1, "size_bytes": 1,
            "document.page_count": 1, "tenant_id": 1, "created_at": 1, "score": 1,
            "snippet": {"$substrCP": [{"$ifNull": ["$extracted_text", ""]}, 0, 500]},
        }},
    ]

    try:
        results = await db.assets.aggregate(pipeline).to_list(length=limit)
    except Exception as exc:
        logger.error("Search failed: %s", exc)
        return []

    return [
        {
            "asset_id": r["_id"],
            "filename": r.get("filename"),
            "content_type": r.get("content_type"),
            "size_bytes": r.get("size_bytes"),
            "page_count": r.get("document", {}).get("page_count") if r.get("document") else None,
            "tenant_id": r.get("tenant_id"),
            "snippet": (r.get("snippet", "")[:200] + "...") if r.get("snippet") else None,
            "score": round(r.get("score", 0), 3),
            "created_at": r["created_at"].isoformat() if r.get("created_at") else None,
        }
        for r in results
    ]


async def update_asset(
    db,
    asset_id: str,
    file_bytes: bytes,
    user_id: str,
    filename: Optional[str] = None,
    tenant_id: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Replace an asset's stored bytes in-place, preserving all provenance metadata."""
    from ..storage.s3 import upload_file, get_presigned_url

    if not user_id:
        return None
    query: Dict[str, Any] = {"_id": asset_id, "user_id": user_id}
    if tenant_id:
        query["tenant_id"] = tenant_id

    existing = await db.assets.find_one(query)
    if not existing:
        return None

    effective_filename = filename or existing["filename"]
    content_type = await detect_content_type(file_bytes, effective_filename)
    checksum = hashlib.sha256(file_bytes).hexdigest()
    s3_key = existing["s3_key"]

    ok = await upload_file(
        io.BytesIO(file_bytes), s3_key, content_type,
        {"asset_id": asset_id, "user_id": existing["user_id"]},
    )
    if not ok:
        return {"error": "S3 upload failed", "asset_id": asset_id}

    thumbnail_s3_key = existing.get("thumbnail_s3_key")
    thumb_bytes = await _generate_thumbnail(file_bytes, content_type)
    if thumb_bytes:
        thumb_key = thumbnail_s3_key or f"{existing['user_id']}/{asset_id}/thumb.png"
        if await upload_file(io.BytesIO(thumb_bytes), thumb_key, "image/png", {"asset_id": asset_id, "type": "thumbnail"}):
            thumbnail_s3_key = thumb_key

    document_metadata = existing.get("document")
    extracted_text = existing.get("extracted_text")
    page_text_store = existing.get("page_texts")

    if content_type == "application/pdf":
        pdf = await _extract_pdf_text(file_bytes)
        document_metadata = {
            "page_count": pdf["page_count"],
            "pages": [{"num": p["num"], "char_count": p["char_count"]} for p in pdf["pages"]],
        }
        extracted_text = pdf["full_text"]
        page_text_store = {str(p["num"]): p["text"] for p in pdf["pages"]}
    elif _is_text_type(content_type):
        try:
            extracted_text = file_bytes.decode("utf-8")
        except UnicodeDecodeError:
            extracted_text = None
        document_metadata = None
        page_text_store = None
    else:
        if existing.get("content_type") == "application/pdf":
            document_metadata = None
            extracted_text = None
            page_text_store = None

    now = datetime.utcnow()
    updates: Dict[str, Any] = {
        "filename": effective_filename,
        "content_type": content_type,
        "size_bytes": len(file_bytes),
        "checksum_sha256": checksum,
        "thumbnail_s3_key": thumbnail_s3_key,
        "document": document_metadata,
        "extracted_text": extracted_text,
        "page_texts": page_text_store,
        "updated_at": now,
    }

    await db.assets.update_one({"_id": asset_id}, {"$set": updates})

    thumbnail_url = None
    if thumbnail_s3_key:
        thumbnail_url = await get_presigned_url(thumbnail_s3_key, expires_in=3600)

    snippet = None
    if extracted_text:
        preview = extracted_text[:200].strip()
        if len(extracted_text) > 200:
            last_sp = preview.rfind(" ")
            if last_sp > 100:
                preview = preview[:last_sp]
            snippet = preview + "..."
        else:
            snippet = preview

    resp: Dict[str, Any] = {
        "asset_id": asset_id,
        "filename": effective_filename,
        "content_type": content_type,
        "size_bytes": len(file_bytes),
        "checksum_sha256": checksum,
        "s3_key": s3_key,
        "thumbnail_url": thumbnail_url,
        "snippet": snippet,
        "tenant_id": existing.get("tenant_id"),
        "status": "complete",
        "updated_at": now.isoformat(),
    }
    if content_type == "application/pdf" and document_metadata:
        resp["page_count"] = document_metadata["page_count"]
    return resp


async def promote_session_artifact(
    db,
    asset_id: str,
    user_id: str,
    tenant_id: Optional[str] = None,
) -> bool:
    """Remove ephemeral session metadata from an asset, making it permanent."""
    if not user_id:
        return False
    query: Dict[str, Any] = {"_id": asset_id, "user_id": user_id}
    if tenant_id:
        query["tenant_id"] = tenant_id

    result = await db.assets.update_one(
        query,
        {
            "$unset": {"session_id": "", "is_ephemeral": ""},
            "$set": {"updated_at": datetime.utcnow()},
        },
    )
    return result.matched_count > 0


async def delete_asset(db, asset_id: str, user_id: str) -> bool:
    from ..storage.s3 import delete_file

    if not user_id:
        return False
    query: Dict[str, Any] = {"_id": asset_id, "user_id": user_id}

    asset = await db.assets.find_one(query)
    if not asset:
        return False

    s3_key = asset.get("s3_key")
    if s3_key:
        await delete_file(s3_key)

    result = await db.assets.delete_one(query)
    return result.deleted_count > 0


# ---------------------------------------------------------------------------
# Write endpoints for code-execution sessions (retires bridge's direct Mongo/S3)
# ---------------------------------------------------------------------------

CODE_SCRIPT_CAP_PER_USER = 5


async def persist_session_artifact(
    db,
    user_id: str,
    session_id: str,
    artifact_path: str,
    file_bytes: bytes,
    artifact_type: Optional[str] = None,
    source: Optional[str] = None,
    is_ephemeral: bool = False,
    container_id: Optional[str] = None,
    tenant_id: Optional[str] = None,
    subagent_task_id: Optional[str] = None,
    scheduled_task_id: Optional[str] = None,
    client_meta: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Upsert an asset produced inside a code-execution session.

    Keyed on (user_id, session_id, artifact_path): repeated writes to the same
    path within the same session overwrite rather than accumulate. The S3 object
    is stored at `artifacts/{user_id}/{session_id}/{artifact_path}`.
    """
    from ..storage.s3 import upload_file

    if not user_id or not session_id or not artifact_path:
        return {"error": "user_id, session_id, and artifact_path are required"}

    filename = artifact_path.rsplit("/", 1)[-1] or artifact_path
    content_type = await detect_content_type(file_bytes, filename)
    checksum = hashlib.sha256(file_bytes).hexdigest()
    s3_key = f"artifacts/{user_id}/{session_id}/{artifact_path}"

    ok = await upload_file(
        io.BytesIO(file_bytes), s3_key, content_type,
        {"user_id": user_id, "session_id": session_id, "artifact_path": artifact_path},
    )
    if not ok:
        return {"error": "S3 upload failed"}

    now = datetime.utcnow()
    upsert_filter = {"user_id": user_id, "session_id": session_id, "artifact_path": artifact_path}

    existing = await db.assets.find_one(upsert_filter, {"_id": 1, "created_at": 1})
    asset_id = existing["_id"] if existing else _generate_asset_id()
    created_at = existing["created_at"] if existing else now

    doc: Dict[str, Any] = {
        "_id": asset_id,
        "user_id": user_id,
        "session_id": session_id,
        "artifact_path": artifact_path,
        "filename": filename,
        "content_type": content_type,
        "size_bytes": len(file_bytes),
        "checksum_sha256": checksum,
        "s3_key": s3_key,
        "is_ephemeral": bool(is_ephemeral),
        "processing_status": "complete",
        "created_at": created_at,
        "updated_at": now,
    }
    if artifact_type:
        doc["artifact_type"] = artifact_type
    if source:
        doc["source"] = source
    if container_id:
        doc["container_id"] = container_id
    if tenant_id:
        doc["tenant_id"] = tenant_id
    if subagent_task_id:
        doc["subagent_task_id"] = subagent_task_id
    if scheduled_task_id:
        doc["scheduled_task_id"] = scheduled_task_id
    if client_meta:
        doc["client_meta"] = client_meta

    await db.assets.replace_one({"_id": asset_id}, doc, upsert=True)

    return {
        "asset_id": asset_id,
        "user_id": user_id,
        "session_id": session_id,
        "artifact_path": artifact_path,
        "filename": filename,
        "content_type": content_type,
        "size_bytes": len(file_bytes),
        "checksum_sha256": checksum,
        "s3_key": s3_key,
        "is_ephemeral": bool(is_ephemeral),
        "artifact_type": artifact_type,
        "source": source,
        "created_at": created_at.isoformat(),
        "updated_at": now.isoformat(),
    }


async def create_code_script_asset(
    db,
    user_id: str,
    code: str,
    filename: str,
    tenant_id: Optional[str] = None,
    client_meta: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Store a code-execution script as an asset tagged `code_script`.

    Enforces a rolling FIFO cap per user: after insertion, any `code_script`
    assets beyond the newest {CODE_SCRIPT_CAP_PER_USER} are evicted (both Mongo
    doc and S3 object).
    """
    from ..storage.s3 import delete_file, upload_file

    if not user_id:
        return {"error": "user_id required"}

    safe_name = filename or "script.py"
    file_bytes = code.encode("utf-8")
    content_type = await detect_content_type(file_bytes, safe_name)
    asset_id = _generate_asset_id()
    ext = safe_name.rsplit(".", 1)[-1].lower() if "." in safe_name else "py"
    s3_key = f"{user_id}/{asset_id}/original.{ext}"
    checksum = hashlib.sha256(file_bytes).hexdigest()

    ok = await upload_file(
        io.BytesIO(file_bytes), s3_key, content_type,
        {"asset_id": asset_id, "user_id": user_id, "kind": "code_script"},
    )
    if not ok:
        return {"error": "S3 upload failed", "asset_id": asset_id}

    now = datetime.utcnow()
    doc: Dict[str, Any] = {
        "_id": asset_id,
        "user_id": user_id,
        "tenant_id": tenant_id,
        "filename": safe_name,
        "content_type": content_type,
        "size_bytes": len(file_bytes),
        "checksum_sha256": checksum,
        "s3_key": s3_key,
        "extracted_text": code,
        "tags": ["code_script"],
        "source": "code_execution_script",
        "processing_status": "complete",
        "created_at": now,
        "updated_at": now,
    }
    if client_meta:
        doc["client_meta"] = client_meta

    await db.assets.insert_one(doc)

    # FIFO eviction: keep only the newest CODE_SCRIPT_CAP_PER_USER code_script assets.
    cursor = db.assets.find(
        {"user_id": user_id, "tags": "code_script"},
        {"_id": 1, "s3_key": 1},
    ).sort("created_at", -1).skip(CODE_SCRIPT_CAP_PER_USER)
    evicted: List[str] = []
    async for stale in cursor:
        stale_key = stale.get("s3_key")
        if stale_key:
            try:
                await delete_file(stale_key)
            except Exception as exc:
                logger.warning("FIFO evict: S3 delete failed for %s: %s", stale_key, exc)
        await db.assets.delete_one({"_id": stale["_id"]})
        evicted.append(stale["_id"])

    return {
        "asset_id": asset_id,
        "filename": safe_name,
        "content_type": content_type,
        "size_bytes": len(file_bytes),
        "checksum_sha256": checksum,
        "s3_key": s3_key,
        "tags": ["code_script"],
        "source": "code_execution_script",
        "created_at": now.isoformat(),
        "evicted_asset_ids": evicted,
    }


async def delete_session_ephemerals(
    db,
    user_id: str,
    session_id: str,
    keep_outputs: bool = False,
) -> Dict[str, Any]:
    """Bulk-delete ephemeral session artifacts.

    Deletes every doc where {user_id, session_id, is_ephemeral=true}. When
    `keep_outputs` is true, artifacts whose `artifact_path` begins with
    `user_outputs/` or `drafts/` are preserved (they represent user-visible
    deliverables, not transient session state).
    """
    from ..storage.s3 import delete_file

    if not user_id or not session_id:
        return {"deleted_count": 0, "deleted_asset_ids": [], "error": "user_id and session_id required"}

    query: Dict[str, Any] = {
        "user_id": user_id,
        "session_id": session_id,
        "is_ephemeral": True,
    }
    if keep_outputs:
        query["artifact_path"] = {"$not": {"$regex": r"^(user_outputs/|drafts/)"}}

    cursor = db.assets.find(query, {"_id": 1, "s3_key": 1})
    targets: List[Dict[str, Any]] = [d async for d in cursor]

    for t in targets:
        s3_key = t.get("s3_key")
        if s3_key:
            try:
                await delete_file(s3_key)
            except Exception as exc:
                logger.warning("ephemeral delete: S3 delete failed for %s: %s", s3_key, exc)

    if targets:
        await db.assets.delete_many({"_id": {"$in": [t["_id"] for t in targets]}})

    return {
        "deleted_count": len(targets),
        "deleted_asset_ids": [t["_id"] for t in targets],
        "keep_outputs": bool(keep_outputs),
    }
