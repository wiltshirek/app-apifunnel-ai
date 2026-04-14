"""Asset routes (/api/v1/assets/*).

Single unified namespace. Auth via _resolve_auth handles all callers:
  - Bearer JWT (client UI / direct)
  - Bearer <MCP_ADMIN_KEY> + X-User-Token (bridge/MCP)
  - Bearer <MCP_ADMIN_KEY> alone (unscoped admin tooling)
"""

import base64
import logging
import re
import unicodedata
from typing import Optional

from pathlib import Path as _Path

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import JSONResponse, PlainTextResponse, Response

from ..auth import authenticate_jwt, verify_admin_key, Identity, _decode_jwt_payload, _identity_from_claims
from ..db import get_db
from ..services.assets import (
    delete_asset,
    detect_content_type,
    get_asset,
    list_assets,
    promote_session_artifact,
    search_assets,
    update_asset,
    upload_asset,
)
from ..storage.s3 import download_file, get_presigned_url

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/assets")


def _sanitize_filename(name: str) -> str:
    """Normalize a filename to printable ASCII so it survives HTTP headers and file systems."""
    name = unicodedata.normalize("NFKC", name)
    name = re.sub(r"[^\x20-\x7e]", "_", name)
    name = re.sub(r"[_\s]{2,}", "_", name)
    return name.strip("_ ")


_OPENAPI_SPEC = _Path(__file__).resolve().parent.parent.parent / "openapi" / "lakehouse.yaml"


@router.get("/openapi.yaml", include_in_schema=False)
async def openapi_spec():
    if _OPENAPI_SPEC.exists():
        return PlainTextResponse(_OPENAPI_SPEC.read_text(), media_type="application/yaml")
    return PlainTextResponse("spec not found", status_code=404)


def _resolve_auth(request: Request) -> tuple[Optional[Identity], bool]:
    """Return (identity, is_admin).

    Admin key + X-User-Token → scoped identity with is_admin=True (bridge/MCP calls).
    Admin key alone → identity=None, is_admin=True (unscoped admin tooling).
    Bearer JWT → user identity, is_admin=False.
    """
    if verify_admin_key(request):
        user_token = request.headers.get("x-user-token") or ""
        if user_token:
            claims = _decode_jwt_payload(user_token)
            if claims:
                ident = _identity_from_claims(claims)
                if ident:
                    ident.is_admin = True
                    return ident, True
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


@router.post("/search")
async def api_search_post(request: Request):
    """POST search with JSON body — used by agent/MCP callers."""
    ident, is_admin = _resolve_auth(request)
    if not ident and not is_admin:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    body = await request.json()
    query_text = body.get("query")
    if not query_text:
        return JSONResponse({"error": "Missing required field: query"}, status_code=400)

    db = await get_db()
    results = await search_assets(
        db,
        user_id=ident.user_id if ident else "",
        query_text=query_text,
        content_type=body.get("content_type"),
        tenant_id=ident.tenant_id if ident else None,
        limit=min(int(body.get("limit", 20)), 100),
        admin_access=is_admin,
    )
    return JSONResponse(results)


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
        result = await upload_asset(
            db, file_bytes, _sanitize_filename(f.filename or "untitled"), effective_user,
            tenant_id=effective_tenant,
            subagent_task_id=ident.subagent_task_id if ident else None,
            scheduled_task_id=ident.scheduled_task_id if ident else None,
            client_meta=ident.client_meta if ident else None,
        )
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
    client_meta = None
    if ident:
        subagent_task_id = ident.subagent_task_id
        scheduled_task_id = ident.scheduled_task_id
        client_meta = ident.client_meta

    db = await get_db()
    result = await upload_asset(
        db, file_bytes, _sanitize_filename(filename), effective_user,
        tenant_id=effective_tenant,
        subagent_task_id=subagent_task_id,
        scheduled_task_id=scheduled_task_id,
        client_meta=client_meta,
    )
    if result.get("error"):
        return JSONResponse(result, status_code=500)
    return JSONResponse(result)


@router.put("/{asset_id}")
async def api_update(asset_id: str, request: Request):
    ident, is_admin = _resolve_auth(request)
    if not ident and not is_admin:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    body = await request.json()
    b64 = body.get("base64_content")
    if not b64:
        return JSONResponse({"error": "Missing base64_content"}, status_code=400)

    try:
        file_bytes = base64.b64decode(b64)
    except Exception:
        return JSONResponse({"error": "Invalid base64_content"}, status_code=400)

    effective_user = body.get("user_id") if is_admin else (ident.user_id if ident else None)
    if not effective_user and not is_admin:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    effective_tenant = body.get("tenant_id") or (ident.tenant_id if ident else None)

    db = await get_db()
    result = await update_asset(
        db, asset_id, file_bytes,
        user_id=effective_user or "",
        filename=body.get("filename"),
        tenant_id=effective_tenant,
        admin_access=is_admin,
    )
    if result is None:
        return JSONResponse({"error": "Asset not found"}, status_code=404)
    if result.get("error"):
        return JSONResponse(result, status_code=500)
    return JSONResponse(result)


@router.get("/{asset_id}")
async def api_get(asset_id: str, request: Request):
    ident, is_admin = _resolve_auth(request)
    if not ident and not is_admin:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    include_page_text = request.query_params.get("include_page_text", "false").lower() == "true"

    db = await get_db()
    asset = await get_asset(
        db, asset_id,
        user_id=ident.user_id if ident else "",
        tenant_id=ident.tenant_id if ident else None,
        admin_access=is_admin,
    )
    if not asset:
        return JSONResponse({"error": "Asset not found"}, status_code=404)

    if not include_page_text:
        asset.pop("page_texts", None)
        asset.pop("extracted_text", None)

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


@router.post("/{asset_id}/promote")
async def api_promote(asset_id: str, request: Request):
    ident, is_admin = _resolve_auth(request)
    if not ident and not is_admin:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    db = await get_db()
    promoted = await promote_session_artifact(
        db, asset_id,
        user_id=ident.user_id if ident else None,
        tenant_id=ident.tenant_id if ident else None,
        admin_access=is_admin,
    )
    if not promoted:
        return JSONResponse({"error": "Asset not found or already promoted"}, status_code=404)
    return JSONResponse({"success": True, "asset_id": asset_id,
                         "message": "Asset promoted to permanent storage"})


@router.get("/{asset_id}/view")
async def api_view(asset_id: str, request: Request):
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

    s3_key = asset.get("s3_key")
    if not s3_key:
        return JSONResponse({"error": "No file stored"}, status_code=404)

    file_bytes = await download_file(s3_key)
    if not file_bytes:
        return JSONResponse({"error": "S3 download failed"}, status_code=502)

    fmt = request.query_params.get("format", "auto")
    ct = asset.get("content_type", "")
    filename = asset.get("filename", "")
    max_pages = int(request.query_params.get("max_pages", "5"))

    is_dxf = ct in ("application/dxf", "application/acad", "image/vnd.dxf") or \
             filename.lower().endswith(".dxf")
    is_dwg = ct in ("application/dwg", "application/acad") or \
             filename.lower().endswith(".dwg")

    if (is_dxf or is_dwg) and fmt in ("pages", "auto"):
        dxf_bytes = file_bytes

        if is_dwg and not is_dxf:
            import shutil, subprocess, tempfile, os
            if shutil.which("dwg2dxf"):
                try:
                    with tempfile.TemporaryDirectory() as tmp:
                        dwg_path = os.path.join(tmp, "input.dwg")
                        dxf_path = os.path.join(tmp, "input.dxf")
                        with open(dwg_path, "wb") as f:
                            f.write(file_bytes)
                        subprocess.run(["dwg2dxf", dwg_path, "-o", dxf_path],
                                       check=True, capture_output=True, timeout=30)
                        with open(dxf_path, "rb") as f:
                            dxf_bytes = f.read()
                        is_dxf = True
                except Exception as exc:
                    logger.warning("DWG→DXF conversion failed: %s", exc)
            else:
                logger.warning("dwg2dxf not found — cannot render DWG; returning raw bytes")

        if is_dxf:
            try:
                import io as _io
                import ezdxf
                from ezdxf.addons.drawing import RenderContext, Frontend
                from ezdxf.addons.drawing.matplotlib import MatplotlibBackend
                import matplotlib
                matplotlib.use("Agg")
                import matplotlib.pyplot as plt

                dxf_text = dxf_bytes.decode("utf-8", errors="replace")
                doc = ezdxf.read(_io.StringIO(dxf_text))
                msp = doc.modelspace()
                fig = plt.figure(figsize=(12, 12), dpi=150)
                ax = fig.add_axes([0, 0, 1, 1])
                ax.set_aspect("equal")
                ctx = RenderContext(doc)
                backend = MatplotlibBackend(ax)
                Frontend(ctx, backend).draw_layout(msp, finalize=True)
                buf = _io.BytesIO()
                fig.savefig(buf, format="png", bbox_inches="tight",
                            facecolor=fig.get_facecolor())
                plt.close(fig)
                buf.seek(0)
                png_bytes = buf.read()
                w_in, h_in = fig.get_size_inches()
                dpi = fig.get_dpi()
                return JSONResponse({
                    "asset_id": asset_id, "filename": filename, "content_type": ct,
                    "format": "pages", "total_pages": 1,
                    "pages": [{"page_num": 1,
                                "base64": base64.b64encode(png_bytes).decode(),
                                "width": int(w_in * dpi), "height": int(h_in * dpi)}],
                })
            except Exception as exc:
                logger.error("DXF rendering failed: %s", exc)
                return JSONResponse({"error": f"DXF rendering failed: {exc}"}, status_code=500)

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
                    "width": pix.width, "height": pix.height,
                })
            doc.close()
            return JSONResponse({
                "asset_id": asset_id, "content_type": ct,
                "page_count": asset.get("document", {}).get("page_count"),
                "pages": pages,
            })
        except ImportError:
            pass

    return JSONResponse({
        "asset_id": asset_id, "content_type": ct,
        "base64": base64.b64encode(file_bytes).decode(),
        "size_bytes": len(file_bytes),
    })


@router.get("/{asset_id}/bytes")
async def api_bytes(asset_id: str, request: Request):
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
async def api_text(asset_id: str, request: Request):
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
