"""Shared dispatch logic for PR Bot runs via GitHub Actions.

Route: POST /api/v1/prbot/dispatch

Flow:
  1. Resolve GitHub token (persisted OAuth first, App installation fallback).
     → Neither available: 422 github_not_connected with resolution.
  2. Load workspace_agent_key (LLM API key).
     → Missing: 422 agent_key_required with resolution.
  3. Mint single-use job token.
  4. Sync workflow file (auto-create or update).
  5. workflow_dispatch — fire-and-forget.
  6. Best-effort run URL fetch (2s delay).
  7. Return 202.
"""

import asyncio
import base64
import logging
import os
from typing import Any, Dict, Optional, Tuple

from ..prompts import load_prompt
from .github_api import (
    check_branch_protection,
    create_or_update_file,
    file_exists,
    get_default_branch,
    get_latest_run_url,
    workflow_dispatch,
)

logger = logging.getLogger(__name__)

WORKSPACE_WORKFLOW_FILE = os.environ.get("WORKSPACE_WORKFLOW_FILE", "mcp-workspace.yml")
PLATFORM_BASE_URL = os.environ.get("MCP_BRIDGE_BASE_URL", "https://tool.apifunnel.ai")

# ---- workflow YAML template v2 (no Docker — agent runs on runner directly) ---

_WORKFLOW_TEMPLATE = """\
name: MCP workspace (PR agent)

on:
  workflow_dispatch:
    inputs:
      task_context:
        description: "Task for the agent"
        required: true
        type: string
      job_token:
        description: "Single-use platform token — exchanges for LLM API key"
        required: true
        type: string
      platform_url:
        description: "Platform base URL for secrets fetch"
        required: false
        type: string
        default: "https://tool.apifunnel.ai"
      target_branch:
        description: "Base branch the PR bot should branch from"
        required: true
        type: string
      agent:
        description: "Agent CLI to use (claude or gemini)"
        required: false
        type: string
        default: "claude"

permissions:
  contents: write
  pull-requests: write

jobs:
  agent:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          ref: ${{ inputs.target_branch }}
          fetch-depth: 0

      - name: Setup agent
        run: |
          REPO="${{ github.repository }}"
          TOKEN="${{ inputs.job_token }}"
          BASE_URL="${{ inputs.platform_url }}"

          RESPONSE=$(curl -sf "$BASE_URL/api/v1/prbot/job-secrets?token=$TOKEN&repo=$REPO")
          if [ $? -ne 0 ]; then
            echo "::error::Failed to fetch platform secrets"
            exit 1
          fi
          API_KEY=$(echo "$RESPONSE" | python3 -c "import sys,json; print(json.load(sys.stdin)['ANTHROPIC_API_KEY'])")
          echo "::add-mask::$API_KEY"
          echo "ANTHROPIC_API_KEY=$API_KEY" >> $GITHUB_ENV

          AGENT="${{ inputs.agent }}"
          if [ "$AGENT" = "gemini" ]; then
            npm install -g @google/gemini-cli
          else
            npm install -g @anthropic-ai/claude-code
          fi

      - name: Inject agent instructions
        run: echo '{{AGENT_PROMPT_B64}}' | base64 -d > CLAUDE.md

      - name: Run agent
        env:
          TASK: ${{ inputs.task_context }}
        run: |
          AGENT="${{ inputs.agent }}"
          if [ "$AGENT" = "gemini" ]; then
            CMD="gemini"
          else
            CMD="claude --dangerously-skip-permissions --output-format stream-json --verbose -p"
          fi
          $CMD "$TASK"
"""


# ---- token resolution -------------------------------------------------------

async def resolve_github_token(
    user_id: str,
    instance_id: Optional[str],
    repo: str,
) -> Optional[str]:
    """Resolve a GitHub token for workspace operations.

    Priority:
      1. User's persisted OAuth token (user_api_tokens, server_name=github_rest)
      2. GitHub App installation token (legacy fallback)

    Returns the token string, or None if neither path yields one.
    """
    from ..database.user_tokens import get_user_api_token

    token_doc = await get_user_api_token(
        user_id=user_id,
        instance_id=instance_id,
        server_name="github_rest",
    )
    if token_doc and token_doc.get("access_token"):
        logger.info("prbot_dispatch: using persisted OAuth token for user=%s", user_id[:12])
        return token_doc["access_token"]

    try:
        from ..database.github_app_installations import (
            get_installation_for_repo,
        )
        from .github_app import get_installation_token

        installation = await get_installation_for_repo(user_id=user_id, repo=repo)
        if installation:
            token = await get_installation_token(
                installation_id=installation["installation_id"],
                repo=repo,
            )
            logger.info("prbot_dispatch: using App installation token for user=%s repo=%s", user_id[:12], repo)
            return token
    except Exception as e:
        logger.warning("prbot_dispatch: App installation fallback failed: %s", e)

    return None


# ---- main entry point -------------------------------------------------------

async def dispatch_workspace(
    repo: str,
    ref: str,
    task_context: str,
    user_id: str,
    instance_id: str | None = None,
) -> Tuple[int, Dict[str, Any]]:
    """Resolve token, sync workflow, fire workflow_dispatch.

    Returns (http_status, response_dict).
    """
    # 1. Resolve GitHub token (OAuth first, App fallback)
    token = await resolve_github_token(user_id, instance_id, repo)
    if not token:
        return 422, {
            "success": False,
            "error": "No GitHub token available. Connect GitHub via OAuth to enable workspace dispatch.",
            "code": "github_not_connected",
            "repo": repo,
            "resolution": {
                "action": "connect_github",
                "description": "Connect your GitHub account via OAuth so the platform can access your repository.",
                "oauth_url": "/api/oauth/start/github_rest_api",
            },
        }

    # 2. Load user's workspace_agent_key
    from ..database.api_keys import get_service_api_key
    from ..database.job_tokens import mint_job_token

    agent_key = await get_service_api_key(user_id, "workspace_agent_key")
    if not agent_key:
        return 422, {
            "success": False,
            "error": "An LLM API key is required to run the coding agent.",
            "code": "agent_key_required",
            "repo": repo,
            "resolution": {
                "action": "store_key",
                "endpoint": "POST /api/service-api-keys",
                "payload": {
                    "service_name": "workspace_agent_key",
                    "api_key": "sk-ant-...",
                },
                "note": (
                    "Use your Anthropic key (sk-ant-...) for Claude Code, "
                    "or your Google AI key for Gemini CLI."
                ),
            },
        }

    # 3. Mint a one-time job token
    job_token = await mint_job_token(
        user_id=user_id,
        repo=repo,
        anthropic_api_key=agent_key,
    )

    workflow_ref = await get_default_branch(repo, token)
    if not workflow_ref:
        return 422, {
            "success": False,
            "error": "Failed to determine the repository default branch via GitHub API.",
            "code": "github_api_error",
            "repo": repo,
            "ref": ref,
        }

    # 4. Sync workflow file (auto-create or update)
    workflow_path = f".github/workflows/{WORKSPACE_WORKFLOW_FILE}"

    workflow_ok = await file_exists(repo, workflow_path, workflow_ref, token)

    commit_msg = (
        "chore: add MCP workspace workflow"
        if not workflow_ok
        else "chore: update MCP workspace workflow"
    )
    logger.info(
        "prbot_dispatch: %s workflow file for %s on default branch %s (target ref %s)",
        "creating" if not workflow_ok else "updating",
        repo, workflow_ref, ref,
    )
    prompt_b64 = base64.b64encode(load_prompt("coding_agent").encode()).decode()
    sync_result = await create_or_update_file(
        repo=repo,
        path=workflow_path,
        content=_WORKFLOW_TEMPLATE.replace("{{AGENT_PROMPT_B64}}", prompt_b64),
        commit_message=commit_msg,
        branch=workflow_ref,
        pat=token,
    )
    if sync_result == "failed":
        return 422, {
            "success": False,
            "error": "Failed to sync workflow file via GitHub API.",
            "code": "workflow_sync_failed",
            "repo": repo,
            "ref": ref,
        }
    logger.info(
        "prbot_dispatch: workflow file sync_result=%s at %s",
        sync_result,
        workflow_path,
    )

    if sync_result in {"created", "updated"}:
        return 202, {
            "success": True,
            "status": "repo_just_configured",
            "message": (
                "The MCP workspace workflow was just "
                f"{'added' if sync_result == 'created' else 'updated'} on the default branch "
                f"'{workflow_ref}'. "
                "GitHub needs a few seconds to register the workflow_dispatch trigger."
            ),
            "repo": repo,
            "ref": ref,
            "workflow_file": workflow_path,
            "workflow_ref": workflow_ref,
            "github": {
                "commit_url": f"https://github.com/{repo}/commits/{workflow_ref}",
            },
            "retry": {
                "action": "CALL_THIS_TOOL_AGAIN_WITH_SAME_ARGUMENTS",
                "delay_seconds": 8,
                "max_attempts": 3,
                "reason": "GitHub registers workflow_dispatch triggers asynchronously after a file commit. The next call will dispatch normally.",
            },
        }

    # 5. Dispatch
    ok, err = await workflow_dispatch(
        repo=repo,
        ref=workflow_ref,
        workflow_file=WORKSPACE_WORKFLOW_FILE,
        inputs={
            "task_context": task_context,
            "job_token": job_token,
            "platform_url": PLATFORM_BASE_URL,
            "target_branch": ref,
        },
        pat=token,
    )
    if not ok:
        return 422, {
            "success": False,
            "error": err or "workflow_dispatch failed",
            "code": "github_api_error",
            "repo": repo,
            "ref": ref,
        }

    # 6. Fetch run URL (best-effort)
    await asyncio.sleep(2)
    run_url = await get_latest_run_url(
        repo=repo,
        workflow_file=WORKSPACE_WORKFLOW_FILE,
        ref=workflow_ref,
        pat=token,
    )

    logger.info(
        "prbot_dispatch: dispatched %s workflow_ref=%s target_ref=%s run_url=%s",
        repo, workflow_ref, ref, run_url or "unknown",
    )

    # 7. Best-effort branch protection warning
    branch_protected = await check_branch_protection(repo, ref, token)

    result: Dict[str, Any] = {
        "success": True,
        "status": "dispatched",
        "repo": repo,
        "ref": ref,
        "workflow_ref": workflow_ref,
        "github": {
            "run_url": run_url or f"https://github.com/{repo}/actions",
        },
    }

    if not branch_protected:
        result["warnings"] = [{
            "code": "branch_unprotected",
            "message": (
                f"Branch '{ref}' has no protection rules. The agent could push directly "
                f"to it. Enable branch protection in repo settings to require PRs."
            ),
            "settings_url": f"https://github.com/{repo}/settings/branches",
        }]

    return 202, result
