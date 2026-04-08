"""Pre-flight check service for PR Bot dispatch.

Route: POST /api/v1/prbot/preflight

Two response formats are built from the same data:
  - JSON     — for the frontend app
  - Markdown — for LLM agent callers (returned in the `markdown` field)

Pre-flight steps (in order):
  1. GitHub token available (persisted OAuth or App installation)
  2. workspace_agent_key (LLM API key) stored for user
"""

import logging
from typing import Any, Dict, List

from .dispatch import resolve_github_token

logger = logging.getLogger(__name__)

# ── Step metadata ─────────────────────────────────────────────────────────────

_STEP_NAMES = {
    1: "GitHub connected",
    2: "LLM API key stored",
}

# ── Preflight core ────────────────────────────────────────────────────────────

async def run_preflight(
    repo: str,
    ref: str,
    user_id: str,
    instance_id: str | None = None,
) -> List[Dict[str, Any]]:
    """Run all pre-flight checks and return a list of step result dicts.

    Each step dict:
        number      int     1–2
        name        str     human label
        status      str     "ok" | "missing"
        code        str     snake_case code for the issue (empty if ok)
        details     dict    step-specific data (guidance, URLs, etc.)
    """
    from ..database.api_keys import get_service_api_key

    steps: List[Dict[str, Any]] = []

    # ── Step 1: GitHub token available ────────────────────────────────────────
    token = await resolve_github_token(user_id=user_id, instance_id=instance_id, repo=repo)

    if not token:
        steps.append({
            "number": 1,
            "name": _STEP_NAMES[1],
            "status": "missing",
            "code": "github_not_connected",
            "details": {
                "oauth_url": "/api/oauth/start/github_rest_api",
                "guidance": (
                    "The user must connect their GitHub account via OAuth so the platform "
                    "has a token with repo scope. Guide the user to the OAuth URL."
                ),
            },
        })
    else:
        steps.append({"number": 1, "name": _STEP_NAMES[1], "status": "ok", "code": "", "details": {}})

    # ── Step 2: LLM API key ──────────────────────────────────────────────────
    agent_key = await get_service_api_key(user_id, "workspace_agent_key")
    if agent_key:
        steps.append({"number": 2, "name": _STEP_NAMES[2], "status": "ok", "code": "", "details": {}})
    else:
        steps.append({
            "number": 2,
            "name": _STEP_NAMES[2],
            "status": "missing",
            "code": "agent_key_required",
            "details": {
                "user_guidance": (
                    "The workspace agent requires an Anthropic API key. "
                    "Get your API key at: https://console.anthropic.com/settings/keys\n"
                    "Keys start with `sk-ant-...`."
                ),
                "store_endpoint": {
                    "method": "POST",
                    "path": "/api/service-api-keys",
                    "body": {
                        "service_name": "workspace_agent_key",
                        "api_key": "<sk-ant-...>",
                    },
                    "note": (
                        "Once the user provides their key, call this endpoint to store it. "
                        "The key is AES-256-GCM encrypted at rest, scoped to this user."
                    ),
                },
            },
        })

    return steps


# ── Response formatters ───────────────────────────────────────────────────────

def _status_icon(status: str) -> str:
    return {
        "ok": "✅",
        "missing": "❌",
    }.get(status, "❓")


def format_markdown(repo: str, ref: str, steps: List[Dict[str, Any]]) -> str:
    """Format preflight results as rich markdown guidance for an LLM agent."""
    complete = sum(1 for s in steps if s["status"] == "ok")
    total = len(steps)
    all_clear = complete == total

    lines = [
        f"## Workspace Pre-flight: `{repo}` @ `{ref}`",
        "",
        f"**{'✅ All systems go — ready to dispatch.' if all_clear else f'⚠️  {complete}/{total} steps complete — not ready to dispatch.'}**",
        "",
        "---",
        "",
    ]

    for step in steps:
        icon = _status_icon(step["status"])
        lines.append(f"### {icon} Step {step['number']}: {step['name']}")
        lines.append("")

        status = step["status"]
        details = step.get("details", {})

        if status == "ok":
            lines.append("No action needed.")

        elif step["code"] == "github_not_connected":
            lines.append("**User must act.** GitHub is not connected.")
            lines.append("")
            lines.append(f"> OAuth URL: {details.get('oauth_url', '')}")
            lines.append(">")
            lines.append("> Guide the user to connect their GitHub account via OAuth.")
            lines.append("> The platform needs a token with repo scope to dispatch workflows.")

        elif step["code"] == "agent_key_required":
            store = details.get("store_endpoint", {})
            lines.append("**User must provide their Anthropic API key.** Tell the user:")
            lines.append("")
            lines.append(f'> {details.get("user_guidance", "")}')
            lines.append("")
            lines.append(
                "Once the user shares their key, store it by calling "
                f"`{store.get('method')} {store.get('path')}` with:"
            )
            lines.append("```json")
            lines.append(f'{{"service_name": "workspace_agent_key", "api_key": "<their sk-ant-... key>"}}')
            lines.append("```")
            lines.append(f"_{store.get('note', '')}_")

        lines.append("")

    lines.append("---")
    lines.append("")
    if all_clear:
        lines.append("**All pre-flight checks passed.** Call `run_pr_agent` with `execution=\"github_actions\"` to start the PR agent.")
    else:
        missing = [s for s in steps if s["status"] != "ok"]
        codes = ", ".join(s["code"] for s in missing if s["code"])
        lines.append(f"**Resolve the steps above, then retry pre-flight.** Outstanding: `{codes}`")

    return "\n".join(lines)


def format_json(repo: str, ref: str, steps: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Format preflight results as structured JSON for the frontend app."""
    complete = sum(1 for s in steps if s["status"] == "ok")
    all_clear = complete == len(steps)

    return {
        "all_clear": all_clear,
        "repo": repo,
        "ref": ref,
        "steps_complete": complete,
        "steps_total": len(steps),
        "steps": [
            {
                "number": s["number"],
                "name": s["name"],
                "status": s["status"],
                "code": s.get("code", ""),
                "details": s.get("details", {}),
            }
            for s in steps
        ],
    }
