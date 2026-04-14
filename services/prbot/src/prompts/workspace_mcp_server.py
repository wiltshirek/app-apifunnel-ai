#!/usr/bin/env python3
"""Minimal stdio MCP server providing a single `submit_pr` tool.

Runs on the GitHub Actions runner. Zero network access, zero credentials.
Writes `.agent-result.json` so the post-agent workflow step can create the PR.
"""

import json
import sys

TOOL_SCHEMA = {
    "name": "submit_pr",
    "description": (
        "Submit your completed work for PR creation. "
        "Infrastructure will handle git commit, push, and PR creation."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "title": {
                "type": "string",
                "description": "PR title. Concise, under 70 chars. Use conventional commit prefix.",
            },
            "summary": {
                "type": "string",
                "description": "Markdown summary of what changed and why. This becomes the PR body.",
            },
            "branch_name": {
                "type": "string",
                "description": "Short branch slug like 'feat/add-rate-limiting'. No spaces.",
            },
        },
        "required": ["title", "summary", "branch_name"],
    },
}

RESULT_FILE = ".agent-result.json"


def _read_message():
    """Read a JSON-RPC message from stdin using Content-Length framing."""
    content_length = -1
    while True:
        line = sys.stdin.buffer.readline()
        if not line:
            return None
        line_str = line.decode("utf-8").strip()
        if line_str == "":
            if content_length >= 0:
                break
            continue
        if line_str.lower().startswith("content-length:"):
            content_length = int(line_str.split(":", 1)[1].strip())

    if content_length <= 0:
        return None

    body = b""
    while len(body) < content_length:
        chunk = sys.stdin.buffer.read(content_length - len(body))
        if not chunk:
            return None
        body += chunk

    return json.loads(body.decode("utf-8"))


def _write_message(msg):
    """Write a JSON-RPC message to stdout using Content-Length framing."""
    body = json.dumps(msg).encode("utf-8")
    header = f"Content-Length: {len(body)}\r\n\r\n".encode("utf-8")
    sys.stdout.buffer.write(header + body)
    sys.stdout.buffer.flush()


def _respond(req_id, result):
    _write_message({"jsonrpc": "2.0", "id": req_id, "result": result})


def _error(req_id, code, message):
    _write_message({"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}})


def _handle_initialize(req_id, _params):
    _respond(req_id, {
        "protocolVersion": "2024-11-05",
        "capabilities": {"tools": {"listChanged": False}},
        "serverInfo": {"name": "workspace-submit", "version": "1.0.0"},
    })


def _handle_tools_list(req_id, _params):
    _respond(req_id, {"tools": [TOOL_SCHEMA]})


def _handle_tools_call(req_id, params):
    name = params.get("name")
    args = params.get("arguments", {})

    if name != "submit_pr":
        _error(req_id, -32601, f"Unknown tool: {name}")
        return

    title = (args.get("title") or "").strip()
    summary = (args.get("summary") or "").strip()
    branch_name = (args.get("branch_name") or "").strip()

    if not title or not summary or not branch_name:
        _respond(req_id, {
            "content": [{"type": "text", "text": "Error: title, summary, and branch_name are all required and must be non-empty."}],
            "isError": True,
        })
        return

    if " " in branch_name:
        _respond(req_id, {
            "content": [{"type": "text", "text": f"Error: branch_name must not contain spaces. Got: '{branch_name}'"}],
            "isError": True,
        })
        return

    result = {
        "status": "ready_for_pr",
        "title": title,
        "summary": summary,
        "branch_name": branch_name,
    }
    with open(RESULT_FILE, "w") as f:
        json.dump(result, f, indent=2)

    _respond(req_id, {
        "content": [{"type": "text", "text": f"PR submission saved to {RESULT_FILE}. Infrastructure will create the PR."}],
    })


HANDLERS = {
    "initialize": _handle_initialize,
    "tools/list": _handle_tools_list,
    "tools/call": _handle_tools_call,
}


def main():
    while True:
        msg = _read_message()
        if msg is None:
            break

        method = msg.get("method", "")
        req_id = msg.get("id")

        if method.startswith("notifications/"):
            continue

        handler = HANDLERS.get(method)
        if handler and req_id is not None:
            handler(req_id, msg.get("params", {}))
        elif req_id is not None:
            _error(req_id, -32601, f"Method not found: {method}")


if __name__ == "__main__":
    main()
