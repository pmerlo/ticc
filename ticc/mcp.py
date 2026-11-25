"""Minimal MCP (Model Context Protocol) server over stdio, stdlib only.

Run with ``ticc mcp``. The agent's identity is ``TICC_ACTOR`` (default
``agent``); its kind is always ``agent``, so it can never approve its own
requests. Point it at a shared tracker with ``TICC_URL`` or ``TICC_DB``.
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any, Callable

from . import __version__
from .client import open_backend
from .store import ISSUE_KINDS, PRIORITIES, REQUEST_KINDS, STATUSES, Actor, TiccError

PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")

INSTRUCTIONS = """\
ticc is the team's issue tracker, shared by humans and agents.

Workflow for agents:
1. Find work: `list_issues`, or `next_issue` to atomically claim the top
   unassigned open issue. Claim a specific issue with `claim_issue` before
   working on it so other agents don't duplicate the effort.
2. While working, call `report_progress` at meaningful milestones (what you
   did, what's next, optional percent). Keep updates short and factual.
3. File anything you discover but won't fix now with `create_issue`
   (bug/feature/task) instead of silently ignoring it.
4. If you are stuck, need a decision, or are about to do something risky or
   irreversible (deleting data, deploying, spending money, changing public
   APIs, anything you were told needs sign-off), call `request_human` and then
   `wait_for_response`. Do NOT proceed with the gated action until the request
   is `approved` (approval) or `answered` (help/decision). If `rejected`,
   follow the human's note. Only humans can approve.
5. When finished, `update_issue` with status `in_review` (if a human should
   check) or `done`, with a short summary in `note`. If you must stop early,
   `release_issue` with a note explaining where you left off.
"""

_ISSUE_ID = {"type": "integer", "description": "Issue number"}


def _tool(name: str, description: str, props: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {
        "name": name,
        "description": description,
        "inputSchema": {
            "type": "object",
            "properties": props,
            "required": required,
            "additionalProperties": False,
        },
    }


TOOLS = [
    _tool(
        "list_issues",
        "List issues. By default returns open (not done/closed) issues, highest priority first.",
        {
            "status": {"type": "string", "description": f"Comma-separated, any of {', '.join(STATUSES)}"},
            "kind": {"type": "string", "enum": list(ISSUE_KINDS)},
            "assignee": {"type": "string", "description": "Name, 'me', or 'none' for unassigned"},
            "label": {"type": "string"},
            "query": {"type": "string", "description": "Text search in title/body"},
            "include_closed": {"type": "boolean"},
            "limit": {"type": "integer", "default": 50},
        },
        [],
    ),
    _tool(
        "get_issue",
        "Get an issue with its full timeline (comments, progress, status changes) and requests.",
        {"issue_id": _ISSUE_ID},
        ["issue_id"],
    ),
    _tool(
        "create_issue",
        "File a new bug report, feature request, or task.",
        {
            "title": {"type": "string"},
            "kind": {"type": "string", "enum": list(ISSUE_KINDS)},
            "body": {"type": "string", "description": "Details: repro steps, expected vs actual, context"},
            "priority": {"type": "string", "enum": list(PRIORITIES), "default": "medium"},
            "labels": {"type": "array", "items": {"type": "string"}},
        },
        ["title", "kind"],
    ),
    _tool(
        "update_issue",
        "Change an issue's status, priority, labels, title or body. Use `note` to explain a status change.",
        {
            "issue_id": _ISSUE_ID,
            "status": {"type": "string", "enum": list(STATUSES)},
            "note": {"type": "string", "description": "Why the status changed / summary of work"},
            "resolution": {"type": "string", "description": "e.g. fixed, wontfix, duplicate"},
            "priority": {"type": "string", "enum": list(PRIORITIES)},
            "labels": {"type": "array", "items": {"type": "string"}},
            "title": {"type": "string"},
            "body": {"type": "string"},
        },
        ["issue_id"],
    ),
    _tool(
        "claim_issue",
        "Assign an issue to yourself and mark it in progress. Fails if someone else holds it.",
        {"issue_id": _ISSUE_ID},
        ["issue_id"],
    ),
    _tool(
        "release_issue",
        "Unassign yourself from an issue so others can pick it up.",
        {"issue_id": _ISSUE_ID, "note": {"type": "string", "description": "Where you left off"}},
        ["issue_id"],
    ),
    _tool(
        "next_issue",
        "Atomically claim the highest-priority unassigned open issue. Returns null if none.",
        {"kind": {"type": "string", "enum": list(ISSUE_KINDS)}, "label": {"type": "string"}},
        [],
    ),
    _tool(
        "comment",
        "Add a comment to an issue.",
        {"issue_id": _ISSUE_ID, "body": {"type": "string"}},
        ["issue_id", "body"],
    ),
    _tool(
        "report_progress",
        "Post a progress update on an issue you're working on.",
        {
            "issue_id": _ISSUE_ID,
            "message": {"type": "string", "description": "What was done / what's next"},
            "percent": {"type": "integer", "minimum": 0, "maximum": 100},
        },
        ["issue_id", "message"],
    ),
    _tool(
        "request_human",
        "Ask a human for help (free-form answer), approval (approve/reject), or a decision "
        "(pick one of `options`). Blocking requests mark the issue blocked until resolved. "
        "Follow up with wait_for_response.",
        {
            "issue_id": _ISSUE_ID,
            "kind": {"type": "string", "enum": list(REQUEST_KINDS), "default": "help"},
            "question": {
                "type": "string",
                "description": "Self-contained: what you need, why, what you tried, and the impact of each answer",
            },
            "options": {"type": "array", "items": {"type": "string"}, "description": "Required for decision"},
            "blocking": {"type": "boolean", "default": True},
        },
        ["issue_id", "question"],
    ),
    _tool(
        "wait_for_response",
        "Wait for a human to resolve a request. Returns the request; check `status` "
        "(pending = timed out, call again; approved/answered/rejected/cancelled = resolved) and `response`.",
        {
            "request_id": {"type": "integer"},
            "timeout_seconds": {"type": "number", "default": 120, "maximum": 600},
        },
        ["request_id"],
    ),
    _tool(
        "list_requests",
        "List human-intervention requests (default: pending ones).",
        {
            "status": {"type": "string", "description": "pending (default), answered, approved, rejected, cancelled, or all"},
            "issue_id": _ISSUE_ID,
            "mine": {"type": "boolean", "description": "Only requests you made"},
        },
        [],
    ),
    _tool(
        "cancel_request",
        "Withdraw one of your pending requests (e.g. you figured it out yourself).",
        {"request_id": {"type": "integer"}, "reason": {"type": "string"}},
        ["request_id"],
    ),
]


class McpServer:
    def __init__(self, backend: Any, actor: Actor) -> None:
        self.be = backend
        self.actor = actor
        self.handlers: dict[str, Callable[[dict[str, Any]], Any]] = {
            "list_issues": self.list_issues,
            "get_issue": lambda a: self.be.get_issue(a["issue_id"]),
            "create_issue": lambda a: self.be.create_issue(
                self.actor,
                title=a["title"],
                kind=a["kind"],
                body=a.get("body", ""),
                priority=a.get("priority", "medium"),
                labels=a.get("labels"),
            ),
            "update_issue": self.update_issue,
            "claim_issue": lambda a: self.be.claim(self.actor, a["issue_id"]),
            "release_issue": lambda a: self.be.release(self.actor, a["issue_id"], a.get("note", "")),
            "next_issue": lambda a: self.be.next_issue(self.actor, a.get("kind"), a.get("label")),
            "comment": lambda a: self.be.comment(self.actor, a["issue_id"], a["body"]),
            "report_progress": lambda a: self.be.report_progress(
                self.actor, a["issue_id"], a["message"], a.get("percent")
            ),
            "request_human": lambda a: self.be.request_human(
                self.actor,
                a["issue_id"],
                a["question"],
                kind=a.get("kind", "help"),
                options=a.get("options"),
                blocking=a.get("blocking", True),
            ),
            "wait_for_response": lambda a: self.be.wait_for_request(
                a["request_id"], timeout=min(float(a.get("timeout_seconds", 120)), 600.0)
            ),
            "list_requests": lambda a: self.be.list_requests(
                status=a.get("status", "pending"),
                issue_id=a.get("issue_id"),
                requested_by=self.actor.name if a.get("mine") else None,
            ),
            "cancel_request": lambda a: self.be.cancel_request(
                self.actor, a["request_id"], a.get("reason", "")
            ),
        }

    def list_issues(self, a: dict[str, Any]) -> Any:
        assignee = a.get("assignee")
        if assignee == "me":
            assignee = self.actor.name
        issues = self.be.list_issues(
            status=a.get("status"),
            kind=a.get("kind"),
            assignee=assignee,
            label=a.get("label"),
            query=a.get("query"),
            include_closed=bool(a.get("include_closed")),
            limit=int(a.get("limit", 50)),
        )
        # Keep listings compact for the model's context window.
        return [
            {k: i.get(k) for k in (
                "id", "kind", "title", "status", "priority", "labels", "assignee",
                "progress", "pending_requests", "updated_at",
            )}
            for i in issues
        ]

    def update_issue(self, a: dict[str, Any]) -> Any:
        fields = {k: v for k, v in a.items() if k != "issue_id"}
        return self.be.update_issue(self.actor, a["issue_id"], **fields)

    # -- JSON-RPC ----------------------------------------------------------

    def handle(self, msg: dict[str, Any]) -> dict[str, Any] | None:
        method = msg.get("method")
        mid = msg.get("id")
        params = msg.get("params") or {}
        if mid is None:  # notification
            return None
        try:
            if method == "initialize":
                requested = params.get("protocolVersion")
                version = requested if requested in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0]
                result: Any = {
                    "protocolVersion": version,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": "ticc", "version": __version__},
                    "instructions": INSTRUCTIONS
                    + f"\nYou are acting as '{self.actor.name}' (agent).",
                }
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": TOOLS}
            elif method == "tools/call":
                result = self.call_tool(params.get("name", ""), params.get("arguments") or {})
            else:
                return _error(mid, -32601, f"method not found: {method}")
        except Exception as e:  # pragma: no cover - defensive
            return _error(mid, -32603, f"internal error: {e}")
        return {"jsonrpc": "2.0", "id": mid, "result": result}

    def call_tool(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        handler = self.handlers.get(name)
        if handler is None:
            return _tool_result({"error": f"unknown tool {name}"}, is_error=True)
        try:
            return _tool_result(handler(args))
        except TiccError as e:
            return _tool_result({"error": str(e), "type": type(e).__name__}, is_error=True)
        except (KeyError, TypeError, ValueError) as e:
            return _tool_result({"error": f"invalid arguments: {e}"}, is_error=True)


def _tool_result(data: Any, is_error: bool = False) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": json.dumps(data, indent=1)}], "isError": is_error}


def _error(mid: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}


def main(db: str | None = None, url: str | None = None) -> int:
    actor = Actor(name=os.environ.get("TICC_ACTOR") or "agent", kind="agent")
    server = McpServer(open_backend(db, url), actor)
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            reply: Any = _error(None, -32700, "parse error")
        else:
            if isinstance(msg, list):
                reply = [r for r in (server.handle(m) for m in msg) if r is not None] or None
            else:
                reply = server.handle(msg)
        if reply is not None:
            sys.stdout.write(json.dumps(reply) + "\n")
            sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
