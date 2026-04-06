"""External asset routes (/api/v1/assets/*).

Authenticated via Bearer JWT (user-facing) or MCP_ADMIN_KEY (admin).
Used by the client web app and direct API consumers.
"""

import base64
import logging
from typing import Optional

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import JSONResponse, Response

from ..auth import authenticate_jwt, verify_admin_key, Identity, _decode_jwt_payload, _identity_from_claims
from ..db import get_db
from ..services.assets import (
    delete_asset,
    detect_content_type,
    get_asset,
    list_assets,
    search_assets,
    upload_asset,
)
from ..storage.s3 import download_file, get_presigned_url

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/assets")


def _resolve_auth(request: Request) -> tuple[Optional[Identity], bool]:
    """Return (identity, is_admin). Admin callers get identity=None."""
    if verify_admin_key(request):
        return None, True
    ident = authenticate_jwt(request)
    return ident, False


@router.get("")
async def api_list(request: Request):
    ident, is_admin = _resolve_auth(request)
    if not ident and not is_admin:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    db = await get_db()
    result = await list_assets(
        db,
        user_id=ident.user_id if ident else "",
        content_type=request.query_params.get("content_type"),
        tenant_id=ident.tenant_id if ident else None,
        limit=int(request.query_params.get("limit", "50")),
        cursor=request.query_params.get("cursor"),
        admin_access=is_admin,
    )

    for asset in result.get("assets", []):
        asset.pop("thumbnail_s3_key", None)

    return JSONResponse(result)


@router.get("/search")
async def api_search(request: Request):
    ident, is_admin = _resolve_auth(request)
    if not ident and not is_admin:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    q = request.query_params.get("q")
    if not q:
        return JSONResponse({"error": "Missing required query parameter: q"}, status_code=400)

    db = await get_db()
    results = await search_assets(
        db,
        user_id=ident.user_id if ident else "",
        query_text=q,
        content_type=request.query_params.get("content_type"),
        tenant_id=ident.tenant_id if ident else None,
        limit=min(int(request.query_params.get("limit", "20")), 100),
        admin_access=is_admin,
    )
    return JSONResponse({"results": results})


@router.post("/upload")
async def api_upload(
    request: Request,
    files: list[UploadFile] = File(...),
    tenant_id: Optional[str] = Form(None),
    user_id: Optional[str] = Form(None),
):
    ident, is_admin = _resolve_auth(request)
    if not ident and not is_admin:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    effective_user = user_id if is_admin and user_id else (ident.user_id if ident else None)
    if not effective_user:
        return JSONResponse({"error": "user_id required for admin uploads"}, status_code=400)
    effective_tenant = tenant_id or (ident.tenant_id if ident else None)

    db = await get_db()
    uploaded = []
    for f in files:
        file_bytes = await f.read()
        result = await upload_asset(db, file_bytes, f.filename or "untitled", effective_user, tenant_id=effective_tenant)
        uploaded.append(result)

    return JSONResponse({"assets": uploaded})


@router.post("/ingest")
async def api_ingest(request: Request):
    ident, is_admin = _resolve_auth(request)
    if not ident and not is_admin:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    body = await request.json()
    filename = body.get("filename")
    b64 = body.get("base64_content")
    if not filename or not b64:
        return JSONResponse({"error": "Missing filename or base64_content"}, status_code=400)

    try:
        file_bytes = base64.b64decode(b64)
    except Exception:
        return JSONResponse({"error": "Invalid base64_content"}, status_code=400)

    effective_user = body.get("user_id") if is_admin else (ident.user_id if ident else None)
    if not effective_user:
        return JSONResponse({"error": "user_id required for admin ingest"}, status_code=400)
    effective_tenant = body.get("tenant_id") or (ident.tenant_id if ident else None)

    subagent_task_id = None
    scheduled_task_id = None
    if ident:
        subagent_task_id = ident.subagent_task_id
        scheduled_task_id = ident.scheduled_task_id

    db = await get_db()
    result = await upload_asset(
        db, file_bytes, filename, effective_user,
        tenant_id=effective_tenant,
        subagent_task_id=subagent_task_id,
        scheduled_task_id=scheduled_task_id,
    )
    if result.get("error"):
        return JSONResponse(result, status_code=500)
    return JSONResponse(result)


@router.get("/{asset_id}")
async def api_get(asset_id: str, request: Request):
    ident, is_admin = _resolve_auth(request)
    if not ident and not is_admin:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    db = await get_db()
    asset = await get_asset(
        db, asset_id,
        user_id=ident.user_id if ident else "",
        tenant_id=ident.tenant_id if ident else None,
        admin_access=is_admin,
    )
    if not asset:
        return JSONResponse({"error": "Asset not found"}, status_code=404)

    thumb_key = asset.pop("thumbnail_s3_key", None)
    if thumb_key:
        asset["thumbnail_url"] = await get_presigned_url(thumb_key, expires_in=3600)

    for field in ("created_at", "updated_at"):
        if asset.get(field) and hasattr(asset[field], "isoformat"):
            asset[field] = asset[field].isoformat()

    return JSONResponse(asset)


@router.get("/{asset_id}/download")
async def api_download(asset_id: str, request: Request):
    ident, is_admin = _resolve_auth(request)
    if not ident and not is_admin:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    db = await get_db()
    asset = await get_asset(
        db, asset_id,
        user_id=ident.user_id if ident else "",
        admin_access=is_admin,
    )
    if not asset:
        return JSONResponse({"error": "Asset not found"}, status_code=404)

    s3_key = asset.get("s3_key")
    if not s3_key:
        return JSONResponse({"error": "No file stored"}, status_code=404)

    file_bytes = await download_file(s3_key)
    if not file_bytes:
        return JSONResponse({"error": "S3 download failed"}, status_code=502)

    ct = asset.get("content_type", "application/octet-stream")
    filename = asset.get("filename", "download")
    return Response(
        content=file_bytes,
        media_type=ct,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.delete("/{asset_id}")
async def api_delete(asset_id: str, request: Request):
    ident, is_admin = _resolve_auth(request)
    if not ident and not is_admin:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    db = await get_db()
    deleted = await delete_asset(
        db, asset_id,
        user_id=ident.user_id if ident else "",
        admin_access=is_admin,
    )
    if not deleted:
        return JSONResponse({"error": "Asset not found"}, status_code=404)
    return JSONResponse({"asset_id": asset_id, "deleted": True})
