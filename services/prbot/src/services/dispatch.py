"""Shared dispatch logic for PR Bot runs via GitHub Actions.

Route: POST /api/v1/prbot/dispatch

Flow:
  1. Validate credentials (github_token and api_key from JWT).
     → Missing: 401 unauthorized.
  2. Sync workflow file (auto-create or update).
  3. workflow_dispatch — fire-and-forget.
  4. Best-effort run URL fetch (2s delay).
  5. Return 202.

The workflow includes a final always-running step that POSTs a structured
run report back to our /callback endpoint so the UI can surface results.
"""

import asyncio
import base64
import logging
import os
import re
import secrets
import time
from typing import Any, Dict, Tuple

from ..prompts import load_asset, load_prompt
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

PRBOT_BASE_URL = os.environ.get("PRBOT_BASE_URL", "https://api.apifunnel.ai")

# ---- workflow YAML template v5 (secure, with run-report callback) -----------

_WORKFLOW_TEMPLATE = """\
name: MCP workspace (PR agent)

on:
  workflow_dispatch:
    inputs:
      task_context:
        description: "Task for the agent"
        required: true
        type: string
      api_key:
        description: "LLM API key for the coding agent"
        required: true
        type: string
      base_branch:
        description: "Base branch to checkout from and PR into"
        required: true
        type: string
      branch_name:
        description: "Feature branch name for the PR"
        required: true
        type: string
      agent:
        description: "Agent CLI to use (claude or gemini)"
        required: false
        type: string
        default: "claude"
      callback_url:
        description: "URL to POST run report on completion"
        required: false
        type: string
        default: ""
      report_token:
        description: "One-time token to authenticate the run report callback"
        required: false
        type: string
        default: ""

permissions:
  contents: write
  pull-requests: write

jobs:
  agent:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          ref: ${{ inputs.base_branch }}
          fetch-depth: 0

      - name: Setup agent
        run: |
          echo "::add-mask::${{ inputs.api_key }}"
          echo "::add-mask::${{ inputs.report_token }}"
          echo "ANTHROPIC_API_KEY=${{ inputs.api_key }}" >> $GITHUB_ENV

          AGENT="${{ inputs.agent }}"
          if [ "$AGENT" = "gemini" ]; then
            npm install -g @google/gemini-cli
          else
            npm install -g @anthropic-ai/claude-code
          fi

      - name: Inject agent artifacts
        run: |
          echo '{{AGENT_PROMPT_B64}}' | base64 -d > CLAUDE.md
          echo '{{MCP_SERVER_B64}}' | base64 -d > .workspace-mcp-server.py
          chmod +x .workspace-mcp-server.py
          echo '{{MCP_CONFIG_B64}}' | base64 -d > .mcp.json

          python3 -c "import json; cfg=json.load(open('.mcp.json')); print('MCP config OK:', list(cfg.get('mcpServers',{}).keys()))"
          echo "CLAUDE.md: $(wc -l < CLAUDE.md) lines"

      - name: Strip git credentials
        run: |
          git config --unset-all http.https://github.com/.extraheader || true
          git config --unset credential.helper || true
          git remote set-url origin https://github.com/${{ github.repository }}.git
          sudo mv /usr/bin/gh /usr/bin/.gh-disabled
          if git push --dry-run 2>/dev/null; then
            echo "::error::Credential stripping failed — git push still works"
            exit 1
          fi
          echo "Git credentials stripped. gh CLI disabled. Agent cannot push."

      - name: Run agent
        id: run_agent
        timeout-minutes: 10
        env:
          TASK: ${{ inputs.task_context }}
          GITHUB_TOKEN: ""
          GH_TOKEN: ""
        run: |
          AGENT="${{ inputs.agent }}"
          if [ "$AGENT" = "gemini" ]; then
            CMD="gemini"
          else
            CMD="claude --dangerously-skip-permissions --output-format stream-json --verbose -p"
          fi
          $CMD "$TASK" 2>&1 | tee /tmp/agent-output.log
          echo "agent_exit_code=$?" >> $GITHUB_OUTPUT

      - name: Create draft PR
        id: create_pr
        if: always()
        env:
          GH_TOKEN: ${{ github.token }}
        run: |
          PR_STATUS="skipped"
          PR_URL=""
          COMMIT_SHA=""

          if [ ! -f .agent-result.json ]; then
            echo "::warning::Agent did not produce .agent-result.json — no PR created"
            PR_STATUS="no_agent_result"
            echo "pr_status=$PR_STATUS" >> $GITHUB_OUTPUT
            echo "pr_url=" >> $GITHUB_OUTPUT
            echo "commit_sha=" >> $GITHUB_OUTPUT
            exit 0
          fi

          TITLE=$(python3 -c "import json; print(json.load(open('.agent-result.json'))['title'])")
          SUMMARY=$(python3 -c "import json; print(json.load(open('.agent-result.json'))['summary'])")
          BRANCH="${{ inputs.branch_name }}"
          BASE="${{ inputs.base_branch }}"

          if echo "$BRANCH" | grep -qE '^(main|master|develop|staging|production)$'; then
            echo "::error::Agent tried to use protected branch name: $BRANCH"
            PR_STATUS="protected_branch"
            echo "pr_status=$PR_STATUS" >> $GITHUB_OUTPUT
            echo "pr_url=" >> $GITHUB_OUTPUT
            echo "commit_sha=" >> $GITHUB_OUTPUT
            exit 1
          fi

          sudo mv /usr/bin/.gh-disabled /usr/bin/gh
          git config user.name "MCP Workspace Bot"
          git config user.email "workspace-bot@apifunnel.ai"
          git config http.https://github.com/.extraheader \
            "AUTHORIZATION: basic $(echo -n "x-access-token:${{ github.token }}" | base64)"

          rm -f CLAUDE.md .workspace-mcp-server.py .mcp.json

          AGENT_RESULT=$(cat .agent-result.json)
          rm -f .agent-result.json

          if [ -z "$(git status --porcelain)" ]; then
            echo "::warning::No file changes detected — no PR created"
            PR_STATUS="no_changes"
            echo "pr_status=$PR_STATUS" >> $GITHUB_OUTPUT
            echo "pr_url=" >> $GITHUB_OUTPUT
            echo "commit_sha=" >> $GITHUB_OUTPUT
            exit 0
          fi

          git checkout -b "$BRANCH"
          git add -A
          git commit -m "$TITLE"
          COMMIT_SHA=$(git rev-parse HEAD)
          git push origin "$BRANCH"

          PR_URL=$(gh pr create \
            --base "$BASE" \
            --title "$TITLE" \
            --body "$SUMMARY" \
            --draft 2>&1) || true

          if echo "$PR_URL" | grep -q "^https://"; then
            PR_STATUS="created"
          else
            PR_STATUS="pr_create_failed"
            echo "::warning::gh pr create output: $PR_URL"
            PR_URL=""
          fi

          echo "pr_status=$PR_STATUS" >> $GITHUB_OUTPUT
          echo "pr_url=$PR_URL" >> $GITHUB_OUTPUT
          echo "commit_sha=$COMMIT_SHA" >> $GITHUB_OUTPUT

      - name: Report run
        if: always()
        env:
          CALLBACK_URL: ${{ inputs.callback_url }}
          REPORT_TOKEN: ${{ inputs.report_token }}
        run: |
          if [ -z "$CALLBACK_URL" ]; then
            echo "No callback_url — skipping run report"
            exit 0
          fi

          AGENT_LOG=""
          if [ -f /tmp/agent-output.log ]; then
            AGENT_LOG=$(tail -c 500000 /tmp/agent-output.log | base64 -w 0)
          fi

          AGENT_RESULT_RAW="{}"
          if [ -f .agent-result.json ]; then
            AGENT_RESULT_RAW=$(cat .agent-result.json)
          fi

          python3 -c "
import json, os, sys

report = {
    'repo': os.environ.get('GITHUB_REPOSITORY', ''),
    'run_id': os.environ.get('GITHUB_RUN_ID', ''),
    'run_url': f\\\"https://github.com/{os.environ.get('GITHUB_REPOSITORY','')}/actions/runs/{os.environ.get('GITHUB_RUN_ID','')}\\\",
    'run_number': os.environ.get('GITHUB_RUN_NUMBER', ''),
    'base_branch': '${{ inputs.base_branch }}',
    'branch_name': '${{ inputs.branch_name }}',
    'agent': '${{ inputs.agent }}',
    'report_token': os.environ.get('REPORT_TOKEN', ''),
    'steps': {
        'agent': {
            'exit_code': '${{ steps.run_agent.outcome }}',
        },
        'create_pr': {
            'exit_code': '${{ steps.create_pr.outcome }}',
            'pr_status': '${{ steps.create_pr.outputs.pr_status }}',
            'pr_url': '${{ steps.create_pr.outputs.pr_url }}',
            'commit_sha': '${{ steps.create_pr.outputs.commit_sha }}',
        },
    },
    'agent_log_b64': '''$AGENT_LOG'''.strip() if '''$AGENT_LOG'''.strip() else None,
    'status': 'success' if '${{ steps.create_pr.outputs.pr_status }}' == 'created' else 'failed',
}

payload = json.dumps(report)
with open('/tmp/run-report.json', 'w') as f:
    f.write(payload)
print(f'Report payload: {len(payload)} bytes, status={report[\"status\"]}')
"

          curl -sS -X POST "$CALLBACK_URL" \
            -H "Content-Type: application/json" \
            -d @/tmp/run-report.json \
            --max-time 15 \
            -o /tmp/callback-response.json \
            -w "\\nHTTP %{http_code}\\n" || echo "::warning::Callback POST failed"

          cat /tmp/callback-response.json 2>/dev/null || true
"""


def _generate_branch_name(task_context: str) -> str:
    """Derive a deterministic branch name from the task context."""
    slug = re.sub(r"[^a-z0-9]+", "-", task_context[:50].lower()).strip("-")
    ts = int(time.time())
    return f"prbot/{slug}-{ts}"


# ---- main entry point -------------------------------------------------------

async def dispatch_workspace(
    repo: str,
    base_branch: str,
    task_context: str,
    github_token: str,
    api_key: str,
    branch_name: str | None = None,
) -> Tuple[int, Dict[str, Any]]:
    """Sync workflow, fire workflow_dispatch.

    All credentials come from the caller (JWT claims). No database access.
    Returns (http_status, response_dict).
    """
    token = github_token

    workflow_ref = await get_default_branch(repo, token)
    if not workflow_ref:
        return 401, {
            "error": "Bad credentials — could not access repository.",
            "code": "unauthorized",
            "repo": repo,
        }

    # Sync workflow file
    workflow_path = f".github/workflows/{WORKSPACE_WORKFLOW_FILE}"

    workflow_ok = await file_exists(repo, workflow_path, workflow_ref, token)

    commit_msg = (
        "chore: add MCP workspace workflow"
        if not workflow_ok
        else "chore: update MCP workspace workflow"
    )
    logger.info(
        "prbot_dispatch: %s workflow file for %s on default branch %s (base_branch %s)",
        "creating" if not workflow_ok else "updating",
        repo, workflow_ref, base_branch,
    )
    prompt_b64 = base64.b64encode(load_prompt("coding_agent").encode()).decode()
    mcp_server_b64 = base64.b64encode(load_asset("workspace_mcp_server.py").encode()).decode()
    mcp_config_b64 = base64.b64encode(load_asset("mcp_config.json").encode()).decode()
    workflow_content = (
        _WORKFLOW_TEMPLATE
        .replace("{{AGENT_PROMPT_B64}}", prompt_b64)
        .replace("{{MCP_SERVER_B64}}", mcp_server_b64)
        .replace("{{MCP_CONFIG_B64}}", mcp_config_b64)
    )
    sync_result = await create_or_update_file(
        repo=repo,
        path=workflow_path,
        content=workflow_content,
        commit_message=commit_msg,
        branch=workflow_ref,
        pat=token,
    )
    if sync_result == "failed":
        return 401, {
            "error": "Bad credentials — could not write workflow file to repository.",
            "code": "unauthorized",
            "repo": repo,
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
            "base_branch": base_branch,
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

    # Dispatch
    effective_branch = branch_name or _generate_branch_name(task_context)

    report_token = secrets.token_urlsafe(32)
    callback_url = f"{PRBOT_BASE_URL}/api/v1/prbot/callback"

    ok, err = await workflow_dispatch(
        repo=repo,
        ref=workflow_ref,
        workflow_file=WORKSPACE_WORKFLOW_FILE,
        inputs={
            "task_context": task_context,
            "api_key": api_key,
            "base_branch": base_branch,
            "branch_name": effective_branch,
            "callback_url": callback_url,
            "report_token": report_token,
        },
        pat=token,
    )
    if not ok:
        return 401, {
            "error": err or "Bad credentials — workflow dispatch rejected by GitHub.",
            "code": "unauthorized",
            "repo": repo,
        }

    dispatch_id = f"dsp_{secrets.token_urlsafe(16)}"

    from ..database.run_reports import save_run_report
    await save_run_report({
        "dispatch_id": dispatch_id,
        "repo": repo,
        "status": "dispatched",
        "base_branch": base_branch,
        "branch_name": effective_branch,
        "report_token": report_token,
        "task_context": task_context[:500],
    })

    # Fetch run URL (best-effort)
    await asyncio.sleep(2)
    run_url = await get_latest_run_url(
        repo=repo,
        workflow_file=WORKSPACE_WORKFLOW_FILE,
        ref=workflow_ref,
        pat=token,
    )

    logger.info(
        "prbot_dispatch: dispatched %s workflow_ref=%s base_branch=%s run_url=%s",
        repo, workflow_ref, base_branch, run_url or "unknown",
    )

    # Best-effort branch protection warning
    branch_protected = await check_branch_protection(repo, base_branch, token)

    result: Dict[str, Any] = {
        "success": True,
        "status": "dispatched",
        "dispatch_id": dispatch_id,
        "repo": repo,
        "base_branch": base_branch,
        "branch_name": effective_branch,
        "workflow_ref": workflow_ref,
        "github": {
            "run_url": run_url or f"https://github.com/{repo}/actions",
        },
    }

    if not branch_protected:
        result["warnings"] = [{
            "code": "branch_unprotected",
            "message": (
                f"Branch '{base_branch}' has no protection rules. The agent could push directly "
                f"to it. Enable branch protection in repo settings to require PRs."
            ),
            "settings_url": f"https://github.com/{repo}/settings/branches",
        }]

    return 202, result
