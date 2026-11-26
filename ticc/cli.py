"""Command-line interface for ticc.

Identity comes from ``TICC_ACTOR`` / ``TICC_ACTOR_KIND`` (or ``--as`` /
``--agent``). Storage is ``TICC_DB`` (default ``./.ticc/ticc.db``, found by
walking up like git) or a remote server given by ``TICC_URL``.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any

from . import __version__
from .client import open_backend
from .store import (
    ISSUE_KINDS,
    PRIORITIES,
    REQUEST_KINDS,
    REQUEST_STATUSES,
    STATUSES,
    Actor,
    Store,
    TiccError,
)

# Exit codes for `ticc wait` so scripts/agents can branch on the outcome.
EXIT_WAIT = {"approved": 0, "answered": 0, "rejected": 3, "cancelled": 4, "pending": 5}

_COLORS = {
    "open": "36",
    "in_progress": "33",
    "blocked": "31;1",
    "in_review": "35",
    "done": "32",
    "closed": "90",
    "critical": "31;1",
    "high": "31",
    "medium": "0",
    "low": "90",
    "pending": "31;1",
    "approved": "32",
    "answered": "32",
    "rejected": "31",
    "cancelled": "90",
}


def _c(text: str, key: str | None = None) -> str:
    if not sys.stdout.isatty() or os.environ.get("NO_COLOR"):
        return text
    code = _COLORS.get(key or text)
    return f"\033[{code}m{text}\033[0m" if code else text


def _who(name: str | None, kind: str | None) -> str:
    if not name:
        return "-"
    return f"{name}{' 🤖' if kind == 'agent' else ''}"


def _print_issue_row(i: dict[str, Any]) -> None:
    flag = _c(f" ⚑{i['pending_requests']}", "pending") if i.get("pending_requests") else ""
    pct = f" {i['progress']}%" if i.get("progress") is not None and i["status"] not in ("done", "closed") else ""
    labels = f" [{', '.join(i['labels'])}]" if i.get("labels") else ""
    print(
        f"#{i['id']:<4} {i['kind']:<7} {_c(i['priority'], i['priority']):<8} "
        f"{_c(i['status'], i['status']):<11}{pct} {i['title']}{labels}"
        f"  @{_who(i.get('assignee'), i.get('assignee_kind'))}{flag}"
    )


def _print_request(r: dict[str, Any], verbose: bool = True) -> None:
    head = (
        f"R{r['id']} [{r['kind']}] {_c(r['status'], r['status'])} on #{r['issue_id']}"
        f" {r.get('issue_title', '')}"
    )
    print(head)
    print(f"    asked by {_who(r['requested_by'], r['requested_by_kind'])} at {r['created_at']}"
          f"{'' if r['blocking'] else ' (non-blocking)'}")
    for line in r["question"].splitlines():
        print(f"    > {line}")
    if r["options"]:
        for n, opt in enumerate(r["options"], 1):
            print(f"      {n}. {opt}")
    if verbose and r["status"] != "pending":
        print(f"    {r['status']} by {_who(r.get('responded_by'), r.get('responded_by_kind'))}"
              f" at {r.get('resolved_at')}")
        for line in (r.get("response") or "").splitlines():
            print(f"    < {line}")


def _print_issue(i: dict[str, Any]) -> None:
    print(f"#{i['id']} {i['title']}")
    print(
        f"  {i['kind']} · {_c(i['priority'], i['priority'])} · {_c(i['status'], i['status'])}"
        + (f" · {i['progress']}%" if i.get("progress") is not None else "")
        + (f" · resolution: {i['resolution']}" if i.get("resolution") else "")
    )
    print(f"  reported by {_who(i['reporter'], i['reporter_kind'])} at {i['created_at']}")
    print(f"  assignee: {_who(i.get('assignee'), i.get('assignee_kind'))}"
          + (f"   labels: {', '.join(i['labels'])}" if i["labels"] else ""))
    if i.get("body"):
        print()
        for line in i["body"].splitlines():
            print(f"  {line}")
    pending = [r for r in i.get("requests", []) if r["status"] == "pending"]
    if pending:
        print("\n" + _c("Waiting on humans:", "pending"))
        for r in pending:
            _print_request(r)
    if i.get("events"):
        print("\nTimeline:")
        for e in i["events"]:
            _print_event(e)


def _print_event(e: dict[str, Any]) -> None:
    who = _who(e["actor"], e["actor_kind"])
    ts = e["created_at"].replace("T", " ").rstrip("Z")
    d = e.get("data") or {}
    t = e["type"]
    if t == "created":
        summary = "created the issue"
    elif t == "status":
        summary = f"status {d.get('from')} → {_c(d.get('to', ''), d.get('to'))}"
    elif t == "assign":
        summary = f"assigned to {d.get('to')}" if d.get("to") else "unassigned"
    elif t == "edit":
        summary = "edited " + ", ".join(f"{k}={v}" for k, v in d.items())
    elif t == "progress":
        summary = "progress" + (f" {d['percent']}%" if d.get("percent") is not None else "")
    elif t == "request":
        summary = f"requested {d.get('kind')} (R{d.get('request_id')})"
    elif t == "response":
        summary = f"R{d.get('request_id')} {d.get('outcome')}"
    else:
        summary = "commented"
    print(f"  {ts}  {who}: {summary}")
    if e.get("body") and t != "edit":
        for line in e["body"].splitlines():
            print(f"      {line}")


def _out(args: argparse.Namespace, data: Any, printer=None) -> None:
    if args.json or printer is None:
        print(json.dumps(data, indent=2))
    else:
        printer(data)


def _actor(args: argparse.Namespace) -> Actor:
    base = Actor.from_env()
    name = args.actor or base.name
    kind = "agent" if args.agent else (args.actor_kind or base.kind)
    return Actor(name=name, kind=kind)


def _read_body(value: str | None) -> str:
    if value == "-":
        return sys.stdin.read()
    return value or ""


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="ticc",
        description="Issue tracking for humans and agents: file bugs and features, "
        "report progress, and ask humans for help or approval.",
    )
    p.add_argument("--version", action="version", version=f"ticc {__version__}")
    p.add_argument("--db", help="SQLite database path (env TICC_DB)")
    p.add_argument("--url", help="remote ticc server URL (env TICC_URL)")
    p.add_argument("--as", dest="actor", help="act as this name (env TICC_ACTOR)")
    p.add_argument("--kind", dest="actor_kind", choices=("human", "agent"),
                   help="actor kind (env TICC_ACTOR_KIND)")
    p.add_argument("--agent", action="store_true", help="shorthand for --kind agent")
    p.add_argument("--json", action="store_true", help="machine-readable JSON output")
    sub = p.add_subparsers(dest="cmd", required=True, metavar="COMMAND")

    s = sub.add_parser("init", help="create a tracker in ./.ticc")

    s = sub.add_parser("new", aliases=["file"], help="file a bug, feature or task")
    s.add_argument("kind", choices=ISSUE_KINDS)
    s.add_argument("title")
    s.add_argument("-b", "--body", help="description ('-' reads stdin)")
    s.add_argument("-p", "--priority", choices=PRIORITIES, default="medium")
    s.add_argument("-l", "--labels", help="comma-separated labels")
    s.add_argument("-a", "--assignee")

    s = sub.add_parser("list", aliases=["ls"], help="list issues (open by default)")
    s.add_argument("-s", "--status", help="comma-separated statuses")
    s.add_argument("-k", "--kind", choices=ISSUE_KINDS)
    s.add_argument("-a", "--assignee", help="name, 'me', or 'none'")
    s.add_argument("-l", "--label")
    s.add_argument("-q", "--query", help="text search")
    s.add_argument("--all", action="store_true", help="include done/closed")
    s.add_argument("-n", "--limit", type=int, default=200)

    s = sub.add_parser("show", help="show an issue with its timeline")
    s.add_argument("id", type=int)

    s = sub.add_parser("edit", help="edit issue fields")
    s.add_argument("id", type=int)
    s.add_argument("-t", "--title")
    s.add_argument("-b", "--body", help="description ('-' reads stdin)")
    s.add_argument("-k", "--kind", dest="issue_kind", choices=ISSUE_KINDS)
    s.add_argument("-p", "--priority", choices=PRIORITIES)
    s.add_argument("-l", "--labels", help="comma-separated labels (replaces)")

    s = sub.add_parser("status", help="change issue status")
    s.add_argument("id", type=int)
    s.add_argument("status", choices=STATUSES)
    s.add_argument("-m", "--message", default="", help="note explaining the change")
    s.add_argument("-r", "--resolution", help="e.g. fixed, wontfix, duplicate")

    for name, status, helptext in (
        ("done", "done", "mark an issue done"),
        ("close", "closed", "close an issue without completing it"),
        ("reopen", "open", "reopen an issue"),
    ):
        s = sub.add_parser(name, help=helptext)
        s.add_argument("id", type=int)
        s.add_argument("-m", "--message", default="")
        if name != "reopen":
            s.add_argument("-r", "--resolution")
        s.set_defaults(target_status=status)

    s = sub.add_parser("assign", help="assign an issue (omit name to unassign)")
    s.add_argument("id", type=int)
    s.add_argument("name", nargs="?")
    s.add_argument("--to-agent", action="store_true", help="assignee is an agent")

    s = sub.add_parser("claim", help="take an issue and start working on it")
    s.add_argument("id", type=int)
    s.add_argument("-f", "--force", action="store_true", help="steal from current assignee")

    s = sub.add_parser("release", help="give an issue back to the queue")
    s.add_argument("id", type=int)
    s.add_argument("-m", "--message", default="")

    s = sub.add_parser("next", help="claim the highest-priority unassigned open issue")
    s.add_argument("-k", "--kind", choices=ISSUE_KINDS)
    s.add_argument("-l", "--label")

    s = sub.add_parser("comment", help="add a comment")
    s.add_argument("id", type=int)
    s.add_argument("body", help="text ('-' reads stdin)")

    s = sub.add_parser("progress", help="report progress on an issue")
    s.add_argument("id", type=int)
    s.add_argument("message", help="text ('-' reads stdin)")
    s.add_argument("-P", "--percent", type=int, help="percent complete (0-100)")

    s = sub.add_parser("ask", help="request human help, approval, or a decision")
    s.add_argument("id", type=int, help="issue id")
    s.add_argument("question", help="text ('-' reads stdin)")
    g = s.add_mutually_exclusive_group()
    g.add_argument("--approval", action="store_const", dest="req_kind", const="approval",
                   help="ask for approve/reject")
    g.add_argument("--decision", action="store_const", dest="req_kind", const="decision",
                   help="ask to pick one of --option")
    s.add_argument("-o", "--option", action="append", dest="options", default=[],
                   help="an option for --decision (repeatable)")
    s.add_argument("--non-blocking", action="store_true",
                   help="don't mark the issue blocked while waiting")
    s.add_argument("-w", "--wait", type=float, metavar="SECONDS",
                   help="wait up to SECONDS for the answer (exit code reflects outcome)")

    s = sub.add_parser("inbox", aliases=["requests"], help="list requests awaiting humans")
    s.add_argument("-s", "--status", choices=(*REQUEST_STATUSES, "all"), default="pending")
    s.add_argument("-i", "--issue", type=int)
    s.add_argument("--mine", action="store_true", help="only requests I made")

    s = sub.add_parser("request", help="show one request")
    s.add_argument("id", type=int)

    s = sub.add_parser("approve", help="approve an approval request")
    s.add_argument("id", type=int)
    s.add_argument("-m", "--message", default="")
    s = sub.add_parser("reject", help="reject an approval request")
    s.add_argument("id", type=int)
    s.add_argument("-m", "--message", default="")

    s = sub.add_parser("answer", help="answer a help or decision request")
    s.add_argument("id", type=int)
    s.add_argument("text", nargs="?", default="", help="answer text ('-' reads stdin)")
    s.add_argument("-c", "--choose", help="option text or number for decision requests")

    s = sub.add_parser("cancel", help="withdraw a pending request")
    s.add_argument("id", type=int)
    s.add_argument("-m", "--message", default="")

    s = sub.add_parser("wait", help="block until a request is resolved")
    s.add_argument("id", type=int)
    s.add_argument("-t", "--timeout", type=float, default=3600)

    s = sub.add_parser("activity", help="recent activity across all issues")
    s.add_argument("-n", "--limit", type=int, default=30)

    sub.add_parser("stats", help="summary counts")

    s = sub.add_parser("serve", help="run the web UI and HTTP API")
    s.add_argument("--host", default=os.environ.get("TICC_HOST", "127.0.0.1"),
                   help="address to bind (env TICC_HOST, default 127.0.0.1)")
    s.add_argument("--port", type=int, default=int(os.environ.get("PORT") or 8765),
                   help="port to listen on (env PORT, default 8765)")

    sub.add_parser("mcp", help="run an MCP server on stdio for AI agents")
    return p


def _finish_wait(args: argparse.Namespace, req: dict[str, Any]) -> int:
    _out(args, req, _print_request)
    return EXIT_WAIT.get(req["status"], 1)


def run(args: argparse.Namespace) -> int:
    if args.cmd == "init":
        store = Store(args.db or os.path.join(".ticc", "ticc.db"))
        print(f"initialised ticc tracker at {store.path}")
        return 0
    if args.cmd == "serve":
        from .server import serve

        serve(Store(args.db), host=args.host, port=args.port)
        return 0
    if args.cmd == "mcp":
        from .mcp import main as mcp_main

        return mcp_main(db=args.db, url=args.url)

    be = open_backend(args.db, args.url)
    me = _actor(args)
    cmd = args.cmd

    if cmd in ("new", "file"):
        issue = be.create_issue(
            me,
            title=args.title,
            kind=args.kind,
            body=_read_body(args.body),
            priority=args.priority,
            labels=args.labels.split(",") if args.labels else None,
            assignee=args.assignee,
        )
        _out(args, issue, lambda i: print(f"filed #{i['id']}: {i['title']}"))
    elif cmd in ("list", "ls"):
        assignee = me.name if args.assignee == "me" else args.assignee
        issues = be.list_issues(
            status=args.status, kind=args.kind, assignee=assignee, label=args.label,
            query=args.query, include_closed=args.all, limit=args.limit,
        )
        _out(args, issues, lambda xs: [_print_issue_row(i) for i in xs] or print("no issues"))
    elif cmd == "show":
        _out(args, be.get_issue(args.id), _print_issue)
    elif cmd == "edit":
        fields: dict[str, Any] = {}
        if args.title is not None:
            fields["title"] = args.title
        if args.body is not None:
            fields["body"] = _read_body(args.body)
        if args.issue_kind:
            fields["kind"] = args.issue_kind
        if args.priority:
            fields["priority"] = args.priority
        if args.labels is not None:
            fields["labels"] = args.labels.split(",")
        if not fields:
            raise TiccError("nothing to edit")
        _out(args, be.update_issue(me, args.id, **fields), _print_issue_row)
    elif cmd in ("status", "done", "close", "reopen"):
        status = getattr(args, "target_status", None) or args.status
        issue = be.set_status(
            me, args.id, status, note=args.message, resolution=getattr(args, "resolution", None)
        )
        _out(args, issue, _print_issue_row)
    elif cmd == "assign":
        issue = be.update_issue(
            me, args.id, assignee=args.name,
            assignee_kind=("agent" if args.to_agent else "human") if args.name else None,
        )
        _out(args, issue, _print_issue_row)
    elif cmd == "claim":
        _out(args, be.claim(me, args.id, force=args.force), _print_issue_row)
    elif cmd == "release":
        _out(args, be.release(me, args.id, note=args.message), _print_issue_row)
    elif cmd == "next":
        issue = be.next_issue(me, kind=args.kind, label=args.label)
        if issue is None:
            if args.json:
                print("null")
            else:
                print("nothing to do", file=sys.stderr)
            return 2
        _out(args, issue, _print_issue)
    elif cmd == "comment":
        _out(args, be.comment(me, args.id, _read_body(args.body)), _print_event)
    elif cmd == "progress":
        ev = be.report_progress(me, args.id, _read_body(args.message), args.percent)
        _out(args, ev, _print_event)
    elif cmd == "ask":
        req = be.request_human(
            me, args.id, _read_body(args.question), kind=args.req_kind or "help",
            options=args.options, blocking=not args.non_blocking,
        )
        if args.wait:
            if not args.json:
                print(f"R{req['id']} filed; waiting up to {args.wait:g}s for a human…",
                      file=sys.stderr)
            return _finish_wait(args, be.wait_for_request(req["id"], timeout=args.wait))
        _out(args, req, _print_request)
    elif cmd in ("inbox", "requests"):
        reqs = be.list_requests(
            status=args.status, issue_id=args.issue,
            requested_by=me.name if args.mine else None,
        )

        def show(rs: list[dict[str, Any]]) -> None:
            if not rs:
                print("nothing waiting on humans" if args.status == "pending" else "no requests")
            for r in rs:
                _print_request(r)
                print()

        _out(args, reqs, show)
    elif cmd == "request":
        _out(args, be.get_request(args.id), _print_request)
    elif cmd in ("approve", "reject"):
        req = be.respond(me, args.id, response=args.message, decision=cmd)
        _out(args, req, _print_request)
    elif cmd == "answer":
        text = _read_body(args.text)
        req = be.respond(me, args.id, response=text, decision=args.choose)
        _out(args, req, _print_request)
    elif cmd == "cancel":
        _out(args, be.cancel_request(me, args.id, reason=args.message), _print_request)
    elif cmd == "wait":
        return _finish_wait(args, be.wait_for_request(args.id, timeout=args.timeout))
    elif cmd == "activity":
        evs = be.events_since(0, args.limit)

        def show_events(es: list[dict[str, Any]]) -> None:
            for e in reversed(es):
                print(f"#{e['issue_id']} {e.get('issue_title', '')}")
                _print_event(e)

        _out(args, evs, show_events)
    elif cmd == "stats":
        def show_stats(s: dict[str, Any]) -> None:
            print("status:   " + "  ".join(f"{k}={v}" for k, v in s["by_status"].items()))
            print("open:     " + "  ".join(f"{k}={v}" for k, v in s["open_by_kind"].items()))
            print(f"waiting on humans: {s['pending_requests']}")

        _out(args, be.stats(), show_stats)
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return run(args)
    except TiccError as e:
        if args.json:
            print(json.dumps({"error": str(e), "type": type(e).__name__}), file=sys.stderr)
        else:
            print(f"ticc: error: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
