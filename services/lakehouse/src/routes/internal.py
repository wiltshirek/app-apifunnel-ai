"""Internal asset routes (/internal/assets/*).

Authenticated via MCP_ADMIN_KEY + X-User-Token (same pattern as the bridge).
Used by sandbox containers and agent tool calls.
"""

import base64
import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from ..auth import authenticate_internal, Identity
from ..db import get_db
from ..services.assets import (
    delete_asset,
    get_asset,
    list_assets,
    search_assets,
    upload_asset,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/internal/assets")


def _fail_auth() -> JSONResponse:
    return JSONResponse({"error": "Unauthorized"}, status_code=401)


@router.post("/search")
async def internal_search(request: Request):
    ident = authenticate_internal(request)
    if not ident:
        return _fail_auth()

    body = await request.json()
    query_text = body.get("query")
    if not query_text:
        return JSONResponse({"error": "Missing required field: query"}, status_code=400)

    db = await get_db()
    results = await search_assets(
        db,
        user_id=ident.user_id,
        query_text=query_text,
        content_type=body.get("content_type"),
        tenant_id=ident.tenant_id,
        limit=min(int(body.get("limit", 20)), 100),
    )
    return JSONResponse(results)


@router.get("")
async def internal_list(request: Request):
    ident = authenticate_internal(request)
    if not ident:
        return _fail_auth()

    db = await get_db()
    result = await list_assets(
        db,
        user_id=ident.user_id,
        content_type=request.query_params.get("content_type"),
        tenant_id=ident.tenant_id,
        limit=int(request.query_params.get("limit", "50")),
        cursor=request.query_params.get("cursor"),
    )
    return JSONResponse(result)


@router.get("/{asset_id}")
async def internal_get(asset_id: str, request: Request):
    ident = authenticate_internal(request)
    if not ident:
        return _fail_auth()

    include_text = request.query_params.get("include_page_text", "false").lower() == "true"
    db = await get_db()
    asset = await get_asset(db, asset_id, ident.user_id, tenant_id=ident.tenant_id)
    if not asset:
        return JSONResponse({"error": "Asset not found"}, status_code=404)

    if not include_text:
        asset.pop("page_texts", None)
        asset.pop("extracted_text", None)

    if asset.get("created_at"):
        asset["created_at"] = asset["created_at"].isoformat() if hasattr(asset["created_at"], "isoformat") else str(asset["created_at"])
    if asset.get("updated_at"):
        asset["updated_at"] = asset["updated_at"].isoformat() if hasattr(asset["updated_at"], "isoformat") else str(asset["updated_at"])
    return JSONResponse(asset)


@router.get("/{asset_id}/view")
async def internal_view(asset_id: str, request: Request):
    ident = authenticate_internal(request)
    if not ident:
        return _fail_auth()

    from ..storage.s3 import download_file

    db = await get_db()
    asset = await get_asset(db, asset_id, ident.user_id, tenant_id=ident.tenant_id)
    if not asset:
        return JSONResponse({"error": "Asset not found"}, status_code=404)

    s3_key = asset.get("s3_key")
    if not s3_key:
        return JSONResponse({"error": "No file stored"}, status_code=404)

    file_bytes = await download_file(s3_key)
    if not file_bytes:
        return JSONResponse({"error": "S3 download failed"}, status_code=502)

    fmt = request.query_params.get("format", "auto")
    ct = asset.get("content_type", "")
    max_pages = int(request.query_params.get("max_pages", "5"))

    if ct == "application/pdf" and fmt in ("pages", "auto"):
        try:
            import fitz
            doc = fitz.open(stream=file_bytes, filetype="pdf")
            pages = []
            for i, page in enumerate(doc):
                if i >= max_pages:
                    break
                pix = page.get_pixmap(dpi=150)
                pages.append({
                    "page": i + 1,
                    "base64": base64.b64encode(pix.tobytes("png")).decode(),
                    "width": pix.width,
                    "height": pix.height,
                })
            doc.close()
            return JSONResponse({
                "asset_id": asset_id, "content_type": ct,
                "page_count": asset.get("document", {}).get("page_count"),
                "pages": pages,
            })
        except ImportError:
            pass

    b64 = base64.b64encode(file_bytes).decode()
    return JSONResponse({"asset_id": asset_id, "content_type": ct, "base64": b64, "size_bytes": len(file_bytes)})


@router.get("/{asset_id}/bytes")
async def internal_bytes(asset_id: str, request: Request):
    ident = authenticate_internal(request)
    if not ident:
        return _fail_auth()

    from ..storage.s3 import download_file

    db = await get_db()
    asset = await get_asset(db, asset_id, ident.user_id, tenant_id=ident.tenant_id)
    if not asset:
        return JSONResponse({"error": "Asset not found"}, status_code=404)

    s3_key = asset.get("s3_key")
    if not s3_key:
        return JSONResponse({"error": "No file stored"}, status_code=404)

    file_bytes = await download_file(s3_key)
    if not file_bytes:
        return JSONResponse({"error": "S3 download failed"}, status_code=502)

    return JSONResponse({
        "asset_id": asset_id,
        "content_type": asset.get("content_type"),
        "size_bytes": len(file_bytes),
        "base64": base64.b64encode(file_bytes).decode(),
    })


@router.get("/{asset_id}/text")
async def internal_text(asset_id: str, request: Request):
    ident = authenticate_internal(request)
    if not ident:
        return _fail_auth()

    db = await get_db()
    asset = await get_asset(db, asset_id, ident.user_id, tenant_id=ident.tenant_id)
    if not asset:
        return JSONResponse({"error": "Asset not found"}, status_code=404)

    text = asset.get("extracted_text")
    if not text:
        return JSONResponse({"error": "No extracted text available for this asset"}, status_code=400)

    return JSONResponse({
        "asset_id": asset_id,
        "filename": asset.get("filename"),
        "content_type": asset.get("content_type"),
        "text": text,
        "char_count": len(text),
    })


@router.post("")
async def internal_upload(request: Request):
    ident = authenticate_internal(request)
    if not ident:
        return _fail_auth()

    body = await request.json()
    filename = body.get("filename")
    b64_content = body.get("base64_content")
    if not filename or not b64_content:
        return JSONResponse({"error": "Missing filename or base64_content"}, status_code=400)

    try:
        file_bytes = base64.b64decode(b64_content)
    except Exception:
        return JSONResponse({"error": "Invalid base64_content"}, status_code=400)

    db = await get_db()
    result = await upload_asset(
        db, file_bytes, filename, ident.user_id,
        tenant_id=ident.tenant_id,
        subagent_task_id=ident.subagent_task_id,
        scheduled_task_id=ident.scheduled_task_id,
    )
    if result.get("error"):
        return JSONResponse(result, status_code=500)
    return JSONResponse(result)
