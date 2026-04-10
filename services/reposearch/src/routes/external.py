"""Repo search routes (/api/v1/repo-search/*).

Auth: admin key in Authorization header (same pattern as lakehouse).
GitHub token: forwarded via X-Dependency-Tokens header.
User identity: X-User-Token header (JWT).
"""

import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from ..auth import require_admin, require_github_token, get_github_token
from ..models import SearchRequest, ReindexRequest
from ..services.github import GitHubAPIError
from ..services.indexer import check_index, get_index_status, delete_index
from ..services.searcher import search

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/repo-search")


def _parse_repo(repo: str) -> tuple[str, str]:
    parts = repo.split("/", 1)
    if len(parts) != 2 or not parts[0] or not parts[1]:
        raise ValueError(f"Invalid repo format: {repo!r} — expected owner/repo")
    return parts[0], parts[1]


def _github_error_response(exc: GitHubAPIError) -> JSONResponse:
    """Convert a GitHub API error to a dependency-token error or passthrough."""
    if exc.status_code in (401, 403):
        return JSONResponse(
            {"error": "dependency_token_expired", "service": "github_rest"},
            status_code=401,
        )
    return JSONResponse(exc.body, status_code=exc.status_code)


@router.post("/search")
async def api_search(body: SearchRequest, request: Request):
    admin_err = require_admin(request)
    if admin_err:
        return admin_err

    try:
        owner, repo = _parse_repo(body.repo)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)

    results = await search(owner, repo, body.query, body.branch, body.top_k)

    if results is not None:
        status = await get_index_status(owner, repo, body.branch)
        return JSONResponse({
            "repo": body.repo,
            "query": body.query,
            "results": results,
            "index_sha": status.get("last_indexed_sha", "") if status else "",
        })

    # Not indexed yet — kick off background indexing, return 202
    github_token = get_github_token(request)
    if not github_token:
        return JSONResponse(
            {"error": "missing_dependency_token", "service": "github_rest"},
            status_code=401,
        )

    try:
        index_result = await check_index(github_token, owner, repo, body.branch)
    except GitHubAPIError as exc:
        return _github_error_response(exc)

    if index_result.get("status") in ("ready", "current"):
        results = await search(owner, repo, body.query, body.branch, body.top_k)
        return JSONResponse({
            "repo": body.repo,
            "query": body.query,
            "results": results or [],
            "index_sha": index_result.get("index_sha", ""),
        })

    return JSONResponse({
        "repo": body.repo,
        "status": "indexing",
        "message": index_result.get("message", "Indexing in progress. Retry in ~30 seconds."),
        "estimated_files": index_result.get("estimated_files"),
    }, status_code=202)


@router.get("/repos/{owner}/{repo}")
async def api_repo_status(owner: str, repo: str, request: Request):
    admin_err = require_admin(request)
    if admin_err:
        return admin_err

    branch = request.query_params.get("branch", "main")
    status = await get_index_status(owner, repo, branch)

    if not status:
        return JSONResponse({"error": "Not Found"}, status_code=404)

    for field in ("last_indexed_at", "created_at", "updated_at"):
        if status.get(field) and hasattr(status[field], "isoformat"):
            status[field] = status[field].isoformat()

    return JSONResponse(status)


@router.post("/repos/{owner}/{repo}/reindex")
async def api_reindex(owner: str, repo: str, body: ReindexRequest, request: Request):
    admin_err = require_admin(request)
    if admin_err:
        return admin_err

    github_token, token_err = require_github_token(request)
    if token_err:
        return token_err

    try:
        result = await check_index(github_token, owner, repo, body.branch, force=True)
    except GitHubAPIError as exc:
        return _github_error_response(exc)

    return JSONResponse({
        "repo": f"{owner}/{repo}",
        "status": result.get("status"),
        "message": result.get("message", "Re-indexing started."),
    }, status_code=202)


@router.delete("/repos/{owner}/{repo}")
async def api_delete(owner: str, repo: str, request: Request):
    admin_err = require_admin(request)
    if admin_err:
        return admin_err

    branch = request.query_params.get("branch", "main")
    deleted = await delete_index(owner, repo, branch)

    if not deleted:
        return JSONResponse({"error": "Not Found"}, status_code=404)

    return JSONResponse({"repo": f"{owner}/{repo}", "branch": branch, "deleted": True})
