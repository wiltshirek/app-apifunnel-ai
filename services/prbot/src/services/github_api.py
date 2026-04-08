"""Thin GitHub REST API client for workspace dispatch operations.

Provides the small set of endpoints needed for workspace pre-flight and dispatch:
  - GET  /repos/{owner}/{repo}/contents/{path}  — check file exists at ref
  - GET  /repos/{owner}/{repo}                  — read repo metadata
  - POST /repos/{owner}/{repo}/actions/workflows/{workflow}/dispatches — trigger run
  - GET  /repos/{owner}/{repo}/actions/runs — find the run just triggered

Uses httpx async client. No PyGithub or other heavy SDK.
Auth: GitHub PAT in Authorization header (token <pat>).
"""

import base64
import logging
from typing import Any, Dict, Literal, Optional, Tuple

import httpx

logger = logging.getLogger(__name__)

GITHUB_API_BASE = "https://api.github.com"
_TIMEOUT = 15.0


def _headers(pat: str) -> Dict[str, str]:
    return {
        "Authorization": f"token {pat}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


async def file_exists(repo: str, path: str, ref: str, pat: str) -> bool:
    """Return True if the file exists in the repo at the given ref."""
    owner, name = _split_repo(repo)
    url = f"{GITHUB_API_BASE}/repos/{owner}/{name}/contents/{path}"
    params = {"ref": ref}

    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.get(url, headers=_headers(pat), params=params)

    if resp.status_code == 200:
        return True
    if resp.status_code == 404:
        return False

    logger.warning(
        "github_api.file_exists: unexpected status %d for %s/%s@%s",
        resp.status_code, repo, path, ref,
    )
    return False


async def get_default_branch(repo: str, pat: str) -> Optional[str]:
    """Return the repository's default branch name."""
    owner, name = _split_repo(repo)
    url = f"{GITHUB_API_BASE}/repos/{owner}/{name}"

    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.get(url, headers=_headers(pat))

    if resp.status_code != 200:
        logger.warning(
            "github_api.get_default_branch: %d for %s",
            resp.status_code, repo,
        )
        return None

    return resp.json().get("default_branch")


async def workflow_dispatch(
    repo: str,
    ref: str,
    workflow_file: str,
    inputs: Dict[str, str],
    pat: str,
) -> Tuple[bool, Optional[str]]:
    """Trigger a workflow_dispatch event.

    Returns (success, error_message).
    GitHub returns 204 No Content on success — there is no run_id in the response.
    """
    owner, name = _split_repo(repo)
    url = f"{GITHUB_API_BASE}/repos/{owner}/{name}/actions/workflows/{workflow_file}/dispatches"
    payload = {"ref": ref, "inputs": inputs}

    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.post(url, headers=_headers(pat), json=payload)

    if resp.status_code == 204:
        return True, None

    body = _safe_body(resp)
    msg = body.get("message", resp.text[:200])
    logger.warning("github_api.workflow_dispatch: %d %s", resp.status_code, msg)
    return False, f"GitHub API {resp.status_code}: {msg}"


async def get_latest_run_url(
    repo: str,
    workflow_file: str,
    ref: str,
    pat: str,
) -> Optional[str]:
    """Return the html_url of the most recent run for the given workflow + ref."""
    owner, name = _split_repo(repo)
    url = f"{GITHUB_API_BASE}/repos/{owner}/{name}/actions/workflows/{workflow_file}/runs"
    params = {"branch": ref, "per_page": "1"}

    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.get(url, headers=_headers(pat), params=params)

    if resp.status_code != 200:
        logger.warning(
            "github_api.get_latest_run_url: %d for %s/%s",
            resp.status_code, repo, workflow_file,
        )
        return None

    runs = resp.json().get("workflow_runs", [])
    if not runs:
        return None
    return runs[0].get("html_url")


async def create_or_update_file(
    repo: str,
    path: str,
    content: str,
    commit_message: str,
    branch: str,
    pat: str,
) -> Literal["created", "updated", "unchanged", "failed"]:
    """Create or update a file via the GitHub Contents API.

    If the file already exists, fetches its SHA first and includes it in the
    PUT body (required by GitHub). Skips the PUT if content is unchanged.
    """
    owner, name = _split_repo(repo)
    url = f"{GITHUB_API_BASE}/repos/{owner}/{name}/contents/{path}"
    encoded = base64.b64encode(content.encode()).decode()

    sha = None
    existed_before = False
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        get_resp = await client.get(
            url, headers=_headers(pat), params={"ref": branch},
        )
        if get_resp.status_code == 200:
            existed_before = True
            file_data = get_resp.json()
            sha = file_data.get("sha")
            existing_b64 = file_data.get("content", "").replace("\n", "")
            if existing_b64 and existing_b64 == encoded:
                logger.info(
                    "github_api.create_or_update_file: %s/%s content unchanged, skipping commit",
                    repo, path,
                )
                return "unchanged"

        body: Dict[str, Any] = {
            "message": commit_message,
            "content": encoded,
            "branch": branch,
        }
        if sha:
            body["sha"] = sha

        put_resp = await client.put(url, headers=_headers(pat), json=body)

    if put_resp.status_code in (200, 201):
        action = "updated" if existed_before else "created"
        logger.info("github_api.create_or_update_file: %s/%s %s", repo, path, action)
        return action

    if put_resp.status_code == 422:
        body_text = _safe_body(put_resp).get("message", "")
        if "sha" in body_text.lower() or "not changed" in body_text.lower():
            logger.info(
                "github_api.create_or_update_file: %s/%s already up to date",
                repo, path,
            )
            return "unchanged"

    logger.warning(
        "github_api.create_or_update_file: %d for %s/%s — %s",
        put_resp.status_code, repo, path, put_resp.text[:200],
    )
    return "failed"


async def check_branch_protection(repo: str, branch: str, pat: str) -> bool:
    """Check if a branch has protection rules enabled."""
    owner, name = _split_repo(repo)
    url = f"{GITHUB_API_BASE}/repos/{owner}/{name}/branches/{branch}/protection"

    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.get(url, headers=_headers(pat))

    if resp.status_code == 200:
        return True
    if resp.status_code == 404:
        return False

    logger.debug(
        "github_api.check_branch_protection: %d for %s/%s",
        resp.status_code, repo, branch,
    )
    return False


def _split_repo(repo: str) -> Tuple[str, str]:
    parts = repo.split("/", 1)
    if len(parts) != 2 or not parts[0] or not parts[1]:
        raise ValueError(f"Invalid repo format '{repo}' — expected 'owner/repo'")
    return parts[0], parts[1]


def _safe_body(resp: httpx.Response) -> Dict[str, Any]:
    try:
        return resp.json()
    except Exception:
        return {}
