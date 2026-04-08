"""PR Bot routes (/api/v1/prbot/*).

All endpoints accept both auth patterns:
  - Bearer JWT (user-facing)
  - MCP_ADMIN_KEY + X-User-Token (sandbox / internal callers)

No /internal/ prefix — same URLs for everyone, auth determines identity.
"""

import json
import logging
import os
from pathlib import Path as _Path
from typing import Optional

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse

from ..auth import authenticate_internal, authenticate_jwt, verify_admin_key, Identity
from ..services.github_app import APP_INSTALL_URL, verify_webhook_signature

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/prbot")

_OPENAPI_SPEC = _Path(__file__).resolve().parent.parent.parent / "openapi" / "prbot.yaml"


@router.get("/openapi.yaml", include_in_schema=False)
async def openapi_spec():
    if _OPENAPI_SPEC.exists():
        return PlainTextResponse(_OPENAPI_SPEC.read_text(), media_type="application/yaml")
    return PlainTextResponse("spec not found", status_code=404)


_RETURN_URL = os.environ.get(
    "WORKSPACE_INSTALL_RETURN_URL",
    "https://app.apifunnel.ai/free-tools/prbot/app",
)


def _resolve_identity(request: Request) -> tuple[Optional[Identity], bool]:
    """Return (identity, is_admin). Accepts admin-key or JWT."""
    ident = authenticate_internal(request)
    if ident and ident.is_admin:
        return ident, True

    ident = authenticate_jwt(request)
    if ident:
        return ident, False

    return None, False


def _effective_user_id(
    ident: Optional[Identity],
    is_admin: bool,
    body: dict,
) -> Optional[str]:
    """Resolve the acting user_id. Admins must supply user_id in the body."""
    if is_admin:
        return body.get("user_id") or (ident.user_id if ident else None)
    return ident.user_id if ident else None


@router.post("/preflight")
async def prbot_preflight(request: Request):
    """Check all dispatch prerequisites.

    Returns structured JSON with step-by-step status + a markdown summary
    for agent callers. Both fields are always present.
    """
    from ..services.preflight import format_json, format_markdown, run_preflight

    ident, is_admin = _resolve_identity(request)
    if not ident:
        return JSONResponse(
            {"error": "Unauthorized", "code": "unauthorized"}, status_code=401,
        )

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body", "code": "bad_request"}, status_code=400)

    repo = body.get("repo")
    if not repo:
        return JSONResponse({"error": "Missing required field: repo", "code": "bad_request"}, status_code=400)

    ref = body.get("ref", "main")
    user_id = _effective_user_id(ident, is_admin, body)
    if not user_id:
        return JSONResponse({"error": "Could not resolve user_id", "code": "bad_request"}, status_code=400)

    try:
        steps = await run_preflight(
            repo=repo, ref=ref, user_id=user_id,
            instance_id=ident.instance_id if ident else None,
        )
        result = format_json(repo=repo, ref=ref, steps=steps)
        result["markdown"] = format_markdown(repo=repo, ref=ref, steps=steps)
        return JSONResponse(result)
    except Exception as e:
        logger.exception("preflight error: %s", e)
        return JSONResponse({"success": False, "error": str(e), "code": "internal_error"}, status_code=500)


@router.post("/dispatch")
async def prbot_dispatch(request: Request):
    """Dispatch a GitHub Actions workflow to create a pull request."""
    from ..services.dispatch import dispatch_workspace

    ident, is_admin = _resolve_identity(request)
    if not ident:
        return JSONResponse(
            {"error": "Unauthorized", "code": "unauthorized"}, status_code=401,
        )

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body", "code": "bad_request"}, status_code=400)

    repo = body.get("repo")
    task_context = body.get("task_context")
    if not repo or not task_context:
        return JSONResponse(
            {"error": "Missing required fields: repo, task_context", "code": "bad_request"},
            status_code=400,
        )

    ref = body.get("ref", "main")
    user_id = _effective_user_id(ident, is_admin, body)
    if not user_id:
        return JSONResponse({"error": "Could not resolve user_id", "code": "bad_request"}, status_code=400)

    try:
        status, result = await dispatch_workspace(
            repo=repo, ref=ref, task_context=task_context,
            user_id=user_id, instance_id=ident.instance_id if ident else None,
        )
        return JSONResponse(result, status_code=status)
    except Exception as e:
        logger.exception("dispatch error: %s", e)
        return JSONResponse({"success": False, "error": str(e), "code": "internal_error"}, status_code=500)


@router.post("/callback")
async def prbot_callback(request: Request):
    """Accept completion callback from a workflow run. Placeholder — logs and ACKs."""
    ident, is_admin = _resolve_identity(request)
    if not ident:
        return JSONResponse({"error": "Unauthorized", "code": "unauthorized"}, status_code=401)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body", "code": "bad_request"}, status_code=400)

    logger.info("callback received: %s", body)
    return JSONResponse({"success": True, "status": "ack"})


@router.get("/install-link")
async def prbot_install_link(request: Request):
    """Return a GitHub App install URL with state=user_id embedded."""
    ident = authenticate_jwt(request)
    if not ident:
        return JSONResponse(
            {"error": "Unauthorized - JWT required", "code": "unauthorized"},
            status_code=401,
        )

    install_url = f"{APP_INSTALL_URL}/installations/new?state={ident.user_id}"
    return JSONResponse({"install_url": install_url})


@router.get("/install-callback")
async def prbot_install_callback(request: Request):
    """Handle GitHub's post-installation redirect. Links installation to platform user."""
    from ..database.github_app_installations import link_installation_by_id

    installation_id_raw = request.query_params.get("installation_id", "")
    state = request.query_params.get("state", "").strip()
    setup_action = request.query_params.get("setup_action", "")

    logger.info(
        "install-callback: installation_id=%s state=%s action=%s",
        installation_id_raw, state[:8] + "..." if state else "", setup_action,
    )

    if not installation_id_raw or not state:
        return JSONResponse(
            {"error": "Missing installation_id or state", "code": "bad_request"},
            status_code=400,
        )

    try:
        installation_id = int(installation_id_raw)
    except ValueError:
        return JSONResponse(
            {"error": "Invalid installation_id", "code": "bad_request"},
            status_code=400,
        )

    linked = await link_installation_by_id(installation_id=installation_id, user_id=state)
    if linked:
        logger.info("install-callback: linked installation_id=%s to user_id=%s", installation_id, state)
    else:
        logger.warning(
            "install-callback: installation_id=%s not found in DB (webhook may not have arrived yet)",
            installation_id,
        )

    return_url = f"{_RETURN_URL}?installation_linked=1&installation_id={installation_id}"
    return RedirectResponse(url=return_url, status_code=302)


@router.get("/job-secrets")
async def prbot_job_secrets(request: Request):
    """Exchange a single-use job token for the user's LLM API key.

    Called from inside the GitHub Actions workflow. No JWT auth — the
    token IS the credential.
    """
    from ..database.job_tokens import consume_job_token

    token = request.query_params.get("token", "").strip()
    repo = request.query_params.get("repo", "").strip()

    if not token or not repo:
        return JSONResponse(
            {"error": "Missing token or repo", "code": "bad_request"},
            status_code=400,
        )

    api_key = await consume_job_token(token=token, repo=repo)
    if not api_key:
        logger.warning("job-secrets: invalid/expired/consumed token for repo=%s", repo)
        return JSONResponse(
            {"error": "Invalid, expired, or already-used token", "code": "unauthorized"},
            status_code=401,
        )

    logger.info("job-secrets: issued ANTHROPIC_API_KEY for repo=%s", repo)
    return JSONResponse({"ANTHROPIC_API_KEY": api_key})


@router.post("/webhook")
async def prbot_webhook(request: Request):
    """Receive and process GitHub App installation events."""
    body_bytes = await request.body()

    sig = request.headers.get("X-Hub-Signature-256", "")
    if not verify_webhook_signature(body_bytes, sig):
        logger.warning("webhook: invalid signature — rejected")
        return JSONResponse({"error": "invalid signature"}, status_code=401)

    event = request.headers.get("X-GitHub-Event", "")
    delivery = request.headers.get("X-GitHub-Delivery", "")
    logger.info("webhook: event=%s delivery=%s", event, delivery)

    try:
        payload = json.loads(body_bytes)
    except Exception:
        return JSONResponse({"error": "invalid JSON"}, status_code=400)

    try:
        await _handle_webhook_event(event, payload)
    except Exception as e:
        logger.exception("webhook: error handling event=%s: %s", event, e)
        return JSONResponse({"status": "error", "detail": str(e)}, status_code=200)

    return JSONResponse({"status": "ok"})


async def _handle_webhook_event(event: str, payload: dict) -> None:
    from ..database.github_app_installations import (
        add_repos,
        delete_installation,
        remove_repos,
        set_suspended,
        upsert_installation,
    )

    installation = payload.get("installation", {})
    installation_id = installation.get("id")
    account = installation.get("account", {})
    account_login = account.get("login", "")
    account_type = account.get("type", "User")

    if event == "installation":
        action = payload.get("action")

        if action == "created":
            repos = [r["full_name"] for r in payload.get("repositories", []) if r.get("full_name")]
            permissions = installation.get("permissions", {})
            await upsert_installation(
                installation_id=installation_id,
                account_login=account_login,
                account_type=account_type,
                user_id=None,
                repos=repos,
                permissions=permissions,
            )
            logger.info("webhook: installed id=%s account=%s repos=%s", installation_id, account_login, repos)

        elif action == "deleted":
            await delete_installation(installation_id)
        elif action == "suspended":
            await set_suspended(installation_id, suspended=True)
        elif action == "unsuspended":
            await set_suspended(installation_id, suspended=False)

    elif event == "installation_repositories":
        action = payload.get("action")

        if action == "added":
            repos = [r["full_name"] for r in payload.get("repositories_added", []) if r.get("full_name")]
            await add_repos(installation_id, repos)
        elif action == "removed":
            repos = [r["full_name"] for r in payload.get("repositories_removed", []) if r.get("full_name")]
            await remove_repos(installation_id, repos)
