"""GitHub REST API client for fetching repository content.

Uses the caller's OAuth token (forwarded via X-Dependency-Tokens) for all requests.
We never store, refresh, or manage tokens — just use what we're given.
"""

import asyncio
import base64
import logging
from typing import Any, Optional

import httpx

logger = logging.getLogger(__name__)

API_BASE = "https://api.github.com"

SKIP_DIRS = frozenset({
    "node_modules", "vendor", ".git", "__pycache__", ".next", ".nuxt",
    "dist", "build", ".tox", ".mypy_cache", ".pytest_cache", "venv",
    ".venv", "env", ".env", "coverage", ".coverage",
})

SKIP_EXTENSIONS = frozenset({
    ".png", ".jpg", ".jpeg", ".gif", ".ico", ".svg", ".webp",
    ".woff", ".woff2", ".ttf", ".eot",
    ".pdf", ".zip", ".tar", ".gz", ".bz2",
    ".exe", ".dll", ".so", ".dylib",
    ".pyc", ".pyo", ".class",
    ".lock", ".min.js", ".min.css",
    ".map",
})

MAX_FILE_SIZE = 100_000  # 100 KB


def _headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github.v3+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _should_skip(path: str) -> bool:
    parts = path.split("/")
    for part in parts:
        if part in SKIP_DIRS:
            return True
    ext_idx = path.rfind(".")
    if ext_idx != -1:
        ext = path[ext_idx:].lower()
        if ext in SKIP_EXTENSIONS:
            return True
    return False


class GitHubAPIError(Exception):
    """Wraps a non-2xx GitHub response so routes can bubble it up."""

    def __init__(self, status_code: int, body: dict):
        self.status_code = status_code
        self.body = body
        super().__init__(f"GitHub API {status_code}: {body}")


async def _check_response(resp: httpx.Response) -> dict:
    if resp.status_code >= 400:
        try:
            body = resp.json()
        except Exception:
            body = {"message": resp.text}
        raise GitHubAPIError(resp.status_code, body)
    return resp.json()


async def get_repo_tree(
    client: httpx.AsyncClient,
    token: str,
    owner: str,
    repo: str,
    branch: str,
) -> list[dict[str, Any]]:
    """Fetch the full recursive tree for a repo branch. Returns file entries only."""
    resp = await client.get(
        f"{API_BASE}/repos/{owner}/{repo}/git/trees/{branch}",
        params={"recursive": "1"},
        headers=_headers(token),
    )
    data = await _check_response(resp)
    files = []
    for entry in data.get("tree", []):
        if entry.get("type") != "blob":
            continue
        path = entry.get("path", "")
        size = entry.get("size", 0)
        if _should_skip(path):
            continue
        if size > MAX_FILE_SIZE:
            continue
        files.append({"path": path, "size": size, "sha": entry.get("sha")})
    return files


async def get_file_content(
    client: httpx.AsyncClient,
    token: str,
    owner: str,
    repo: str,
    path: str,
    ref: str,
) -> Optional[str]:
    """Fetch and base64-decode a single file's content."""
    resp = await client.get(
        f"{API_BASE}/repos/{owner}/{repo}/contents/{path}",
        params={"ref": ref},
        headers=_headers(token),
    )
    if resp.status_code == 404:
        return None
    data = await _check_response(resp)
    content_b64 = data.get("content")
    if not content_b64:
        return None
    try:
        return base64.b64decode(content_b64).decode("utf-8", errors="replace")
    except Exception:
        return None


async def get_file_contents_batch(
    client: httpx.AsyncClient,
    token: str,
    owner: str,
    repo: str,
    paths: list[str],
    ref: str,
    concurrency: int = 10,
) -> dict[str, str]:
    """Fetch multiple files concurrently. Returns {path: content} for successful fetches."""
    semaphore = asyncio.Semaphore(concurrency)
    results: dict[str, str] = {}

    async def _fetch(path: str):
        async with semaphore:
            content = await get_file_content(client, token, owner, repo, path, ref)
            if content is not None:
                results[path] = content

    await asyncio.gather(*[_fetch(p) for p in paths], return_exceptions=True)
    return results


async def get_head_sha(
    client: httpx.AsyncClient,
    token: str,
    owner: str,
    repo: str,
    branch: str,
) -> str:
    """Get the HEAD commit SHA for a branch."""
    resp = await client.get(
        f"{API_BASE}/repos/{owner}/{repo}/commits/{branch}",
        headers=_headers(token),
    )
    data = await _check_response(resp)
    return data["sha"]


async def get_changed_files(
    client: httpx.AsyncClient,
    token: str,
    owner: str,
    repo: str,
    base_sha: str,
    head_sha: str,
) -> dict[str, list[str]]:
    """Compare two SHAs and return categorized file changes.

    Returns {"added": [...], "modified": [...], "removed": [...]}.
    """
    resp = await client.get(
        f"{API_BASE}/repos/{owner}/{repo}/compare/{base_sha}...{head_sha}",
        headers=_headers(token),
    )
    data = await _check_response(resp)
    result: dict[str, list[str]] = {"added": [], "modified": [], "removed": []}
    for f in data.get("files", []):
        path = f.get("filename", "")
        status = f.get("status", "")
        if status == "added":
            result["added"].append(path)
        elif status == "removed":
            result["removed"].append(path)
        else:
            result["modified"].append(path)
    return result
