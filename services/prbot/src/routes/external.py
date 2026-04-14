"""PR Bot routes (/api/v1/prbot/*).

Auth strategy per route:

  /dispatch      — admin key (perimeter). No user identity needed.
  /callback      — report_token (one-time secret). No admin key or JWT.
  /runs, /runs/X — admin key OR JWT (perimeter + optional identity for UI).
  /install-link  — JWT required (needs user_id for GitHub state param).
  /webhook       — HMAC signature (X-Hub-Signature-256).
  /install-cb    — no auth (GitHub redirect, state param carries user_id).
  /openapi.yaml  — public.
"""

import json
import logging
import os
from pathlib import Path as _Path
from typing import Optional

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse

from ..auth import extract_dependency_tokens, extract_identity, verify_admin_key, Identity
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


def _require_perimeter(request: Request) -> bool:
    """Check that the caller passed the admin key OR a valid JWT.

    Admin key = internal service (bridge). JWT = direct user call.
    Either proves you're an authorized caller. This is the perimeter check.
    """
    if verify_admin_key(request):
        return True
    if extract_identity(request):
        return True
    return False


@router.post("/dispatch")
async def prbot_dispatch(request: Request):
    """Dispatch a GitHub Actions workflow to create a pull request.

    Auth: admin key (bridge) or JWT (direct). Perimeter only — no user_id needed.
    Credentials (github_token, api_key) come from the body or X-Dependency-Tokens.
    """
    from ..services.dispatch import dispatch_workspace

    if not _require_perimeter(request):
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

    base_branch = body.get("base_branch", "main")
    branch_name = body.get("branch_name") or None

    dep_tokens = extract_dependency_tokens(request)
    github_token = body.get("github_token") or dep_tokens.get("github_rest")
    api_key = body.get("api_key") or dep_tokens.get("agent_key")

    if not github_token:
        return JSONResponse(
            {"error": "Bad credentials — missing github_token.", "code": "unauthorized"},
            status_code=401,
        )
    if not api_key:
        return JSONResponse(
            {"error": "Bad credentials — missing api_key.", "code": "unauthorized"},
            status_code=401,
        )

    try:
        status, result = await dispatch_workspace(
            repo=repo, base_branch=base_branch, task_context=task_context,
            github_token=github_token, api_key=api_key, branch_name=branch_name,
        )
        return JSONResponse(result, status_code=status)
    except Exception as e:
        logger.exception("dispatch error: %s", e)
        return JSONResponse({"success": False, "error": str(e), "code": "internal_error"}, status_code=500)


@router.post("/callback")
async def prbot_callback(request: Request):
    """Accept run report from a completed workflow run.

    Called by the final workflow step (if: always) with the full run context:
    agent output, step results, PR metadata, errors.
    Validates the one-time report_token, then merges results into the
    existing dispatched record in MongoDB.
    """
    from ..database.run_reports import complete_run_report

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body", "code": "bad_request"}, status_code=400)

    report_token = body.get("report_token", "").strip()
    if not report_token:
        return JSONResponse(
            {"error": "Missing report_token", "code": "unauthorized"},
            status_code=401,
        )

    if not body.get("repo") or not body.get("run_id"):
        return JSONResponse(
            {"error": "Missing required fields: repo, run_id", "code": "bad_request"},
            status_code=400,
        )

    updated = await complete_run_report(report_token=report_token, report_data=body)
    if not updated:
        return JSONResponse(
            {"error": "Invalid or expired report_token", "code": "unauthorized"},
            status_code=401,
        )

    logger.info(
        "callback: run report completed for %s run_id=%s status=%s",
        body.get("repo"), body.get("run_id"), body.get("status"),
    )
    return JSONResponse({"success": True})


@router.get("/runs")
async def prbot_list_runs(request: Request):
    """List recent run reports for a repo.

    Auth: admin key or JWT (perimeter). No user_id needed — the repo
    itself is the scoping key.
    """
    from ..database.run_reports import get_run_reports

    if not _require_perimeter(request):
        return JSONResponse({"error": "Unauthorized", "code": "unauthorized"}, status_code=401)

    repo = request.query_params.get("repo", "").strip()
    if not repo:
        return JSONResponse({"error": "Missing query param: repo", "code": "bad_request"}, status_code=400)

    limit = min(int(request.query_params.get("limit", "20")), 100)
    skip = int(request.query_params.get("skip", "0"))

    reports = await get_run_reports(repo=repo, limit=limit, skip=skip)
    return JSONResponse({"repo": repo, "count": len(reports), "reports": reports}, default=str)


@router.get("/runs/{identifier}")
async def prbot_get_run(request: Request, identifier: str):
    """Get run status + metadata by dispatch_id or GitHub Actions run_id.

    Auth: admin key or JWT (perimeter).
    Returns a log summary (availability, line count, preview) — use
    GET /runs/{id}/logs for actual log lines.
    """
    from ..database.run_reports import get_run_report

    if not _require_perimeter(request):
        return JSONResponse({"error": "Unauthorized", "code": "unauthorized"}, status_code=401)

    report = await get_run_report(identifier)
    if not report:
        return JSONResponse({"error": "Run not found", "code": "not_found"}, status_code=404)

    report["logs"] = _build_log_summary(report)

    return JSONResponse(report, default=str)


@router.get("/runs/{identifier}/logs")
async def prbot_get_run_logs(request: Request, identifier: str):
    """Retrieve paginated log lines for a run.

    Three retrieval modes:
      - all=true: every line in one response (for archival / programmatic use)
      - page=N: specific page of lines (1-indexed)
      - page=-1: last page only (quick tail check)

    Auth: admin key or JWT (perimeter).
    """
    from ..database.run_reports import get_run_log_lines

    if not _require_perimeter(request):
        return JSONResponse({"error": "Unauthorized", "code": "unauthorized"}, status_code=401)

    all_lines = request.query_params.get("all", "").lower() in ("true", "1")
    page = int(request.query_params.get("page", "1"))
    page_size = min(int(request.query_params.get("page_size", "100")), 500)

    result = await get_run_log_lines(
        identifier=identifier,
        page=page,
        page_size=page_size,
        all_lines=all_lines,
    )
    if result is None:
        return JSONResponse({"error": "Run not found or no logs available", "code": "not_found"}, status_code=404)

    return JSONResponse(result, default=str)


def _build_log_summary(report: dict) -> dict:
    """Build the logs summary block for a run status response.

    Uses log_line_count from the DB record (log_lines themselves are
    excluded by the summary projection).
    """
    line_count = report.pop("log_line_count", 0) or 0
    truncated = report.pop("log_truncated", False)
    default_page_size = 100
    total_pages = -(-line_count // default_page_size) if line_count > 0 else 0

    return {
        "available": line_count > 0,
        "total_lines": line_count,
        "total_pages": total_pages,
        "truncated": truncated,
    }


@router.get("/install-link")
async def prbot_install_link(request: Request):
    """Return a GitHub App install URL with state=user_id embedded.

    Auth: JWT required — this is the one route that actually needs user_id
    because we embed it in the GitHub install state param.
    """
    ident = extract_identity(request)
    if not ident:
        return JSONResponse(
            {"error": "Unauthorized — JWT required (need user_id for install state)",
             "code": "unauthorized"},
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
