"""HTTP JSON API and web UI for ticc (stdlib only).

Identity is passed per request with ``X-Ticc-Actor`` / ``X-Ticc-Actor-Kind``
headers (or an ``actor`` object in the JSON body). If ``TICC_TOKEN`` is set,
every API call must carry ``Authorization: Bearer <token>``.
"""

from __future__ import annotations

import hmac
import json
import os
import re
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

from . import __version__
from .store import (
    ACTOR_KINDS,
    ISSUE_KINDS,
    PRIORITIES,
    REQUEST_KINDS,
    REQUEST_STATUSES,
    STATUSES,
    Actor,
    Store,
    TiccError,
)

MAX_BODY = 1 << 20  # 1 MiB
MAX_WAIT = 300.0

Route = tuple[str, "re.Pattern[str]", Callable[..., Any]]


def _bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).lower() in ("1", "true", "yes", "on")


class Api:
    """Maps HTTP routes onto :class:`Store` calls."""

    def __init__(self, store: Store) -> None:
        self.store = store
        self.routes: list[Route] = []
        r = self._route
        r("GET", r"/api/meta", self.meta)
        r("GET", r"/api/stats", lambda a, q, b: self.store.stats())
        r("GET", r"/api/events", self.events)
        r("GET", r"/api/issues", self.list_issues)
        r("POST", r"/api/issues", self.create_issue)
        r("POST", r"/api/issues/next", self.next_issue)
        r("GET", r"/api/issues/(\d+)", lambda a, q, b, i: self.store.get_issue(int(i)))
        r("PATCH", r"/api/issues/(\d+)", self.update_issue)
        r("POST", r"/api/issues/(\d+)/comments", self.comment)
        r("POST", r"/api/issues/(\d+)/progress", self.progress)
        r("POST", r"/api/issues/(\d+)/claim", self.claim)
        r("POST", r"/api/issues/(\d+)/release", self.release)
        r("POST", r"/api/issues/(\d+)/requests", self.request_human)
        r("GET", r"/api/requests", self.list_requests)
        r("GET", r"/api/requests/(\d+)", self.get_request)
        r("POST", r"/api/requests/(\d+)/respond", self.respond)
        r("POST", r"/api/requests/(\d+)/cancel", self.cancel)

    def _route(self, method: str, pattern: str, fn: Callable[..., Any]) -> None:
        self.routes.append((method, re.compile(f"^{pattern}$"), fn))

    def dispatch(
        self, method: str, path: str, actor: Actor, query: dict[str, str], body: dict[str, Any]
    ) -> tuple[int, Any]:
        path_matched = False
        for m, pattern, fn in self.routes:
            match = pattern.match(path)
            if not match:
                continue
            path_matched = True
            if m != method:
                continue
            result = fn(actor, query, body, *match.groups())
            status = 201 if method == "POST" and fn in (self.create_issue, self.request_human) else 200
            return status, result
        if path_matched:
            return 405, {"error": "method not allowed"}
        return 404, {"error": f"no route for {method} {path}"}

    # -- handlers ----------------------------------------------------------

    def meta(self, actor: Actor, q: dict, b: dict) -> dict[str, Any]:
        return {
            "version": __version__,
            "you": actor.as_dict(),
            "issue_kinds": ISSUE_KINDS,
            "priorities": PRIORITIES,
            "statuses": STATUSES,
            "request_kinds": REQUEST_KINDS,
            "request_statuses": REQUEST_STATUSES,
            "actor_kinds": ACTOR_KINDS,
        }

    def events(self, actor: Actor, q: dict, b: dict) -> list[dict[str, Any]]:
        return self.store.events_since(int(q.get("after", 0)), int(q.get("limit", 100)))

    def list_issues(self, actor: Actor, q: dict, b: dict) -> list[dict[str, Any]]:
        return self.store.list_issues(
            status=q.get("status") or None,
            kind=q.get("kind") or None,
            assignee=q.get("assignee") or None,
            label=q.get("label") or None,
            query=q.get("q") or None,
            reporter=q.get("reporter") or None,
            include_closed=_bool(q.get("all", False)),
            limit=int(q.get("limit", 200)),
        )

    def create_issue(self, actor: Actor, q: dict, b: dict) -> dict[str, Any]:
        return self.store.create_issue(
            actor,
            title=b.get("title", ""),
            kind=b.get("kind", "task"),
            body=b.get("body", ""),
            priority=b.get("priority", "medium"),
            labels=b.get("labels"),
            assignee=b.get("assignee"),
            assignee_kind=b.get("assignee_kind"),
        )

    def next_issue(self, actor: Actor, q: dict, b: dict) -> dict[str, Any] | None:
        return self.store.next_issue(actor, kind=b.get("kind"), label=b.get("label"))

    def update_issue(self, actor: Actor, q: dict, b: dict, issue_id: str) -> dict[str, Any]:
        fields = {k: v for k, v in b.items() if k != "actor"}
        return self.store.update_issue(actor, int(issue_id), **fields)

    def comment(self, actor: Actor, q: dict, b: dict, issue_id: str) -> dict[str, Any]:
        return self.store.comment(actor, int(issue_id), b.get("body", ""))

    def progress(self, actor: Actor, q: dict, b: dict, issue_id: str) -> dict[str, Any]:
        return self.store.report_progress(
            actor, int(issue_id), b.get("message", b.get("body", "")), b.get("percent")
        )

    def claim(self, actor: Actor, q: dict, b: dict, issue_id: str) -> dict[str, Any]:
        return self.store.claim(actor, int(issue_id), force=_bool(b.get("force", False)))

    def release(self, actor: Actor, q: dict, b: dict, issue_id: str) -> dict[str, Any]:
        return self.store.release(actor, int(issue_id), b.get("note", ""))

    def request_human(self, actor: Actor, q: dict, b: dict, issue_id: str) -> dict[str, Any]:
        return self.store.request_human(
            actor,
            int(issue_id),
            question=b.get("question", ""),
            kind=b.get("kind", "help"),
            options=b.get("options"),
            blocking=_bool(b.get("blocking", True)),
        )

    def list_requests(self, actor: Actor, q: dict, b: dict) -> list[dict[str, Any]]:
        return self.store.list_requests(
            status=q.get("status", "pending"),
            issue_id=int(q["issue"]) if q.get("issue") else None,
            requested_by=q.get("requested_by") or None,
            limit=int(q.get("limit", 200)),
        )

    def get_request(self, actor: Actor, q: dict, b: dict, request_id: str) -> dict[str, Any]:
        wait = min(float(q.get("wait", 0) or 0), MAX_WAIT)
        if wait > 0:
            return self.store.wait_for_request(int(request_id), timeout=wait)
        return self.store.get_request(int(request_id))

    def respond(self, actor: Actor, q: dict, b: dict, request_id: str) -> dict[str, Any]:
        return self.store.respond(
            actor, int(request_id), response=b.get("response", ""), decision=b.get("decision")
        )

    def cancel(self, actor: Actor, q: dict, b: dict, request_id: str) -> dict[str, Any]:
        return self.store.cancel_request(actor, int(request_id), b.get("reason", ""))


def _load_index() -> bytes:
    return resources.files("ticc").joinpath("web/index.html").read_bytes()


def make_handler(api: Api, token: str | None) -> type[BaseHTTPRequestHandler]:
    index_html = _load_index()

    class Handler(BaseHTTPRequestHandler):
        server_version = f"ticc/{__version__}"
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt: str, *args: Any) -> None:  # quieter logs
            if os.environ.get("TICC_QUIET"):
                return
            super().log_message(fmt, *args)

        def _send(self, status: int, payload: Any, content_type: str = "application/json") -> None:
            data = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def _handle(self, method: str) -> None:
            url = urlparse(self.path)
            if method == "GET" and url.path in ("/", "/index.html"):
                self._send(200, index_html, "text/html; charset=utf-8")
                return
            if not url.path.startswith("/api/"):
                self._send(404, {"error": "not found"})
                return
            if token:
                auth = self.headers.get("Authorization", "")
                if not hmac.compare_digest(auth, f"Bearer {token}"):
                    self._send(401, {"error": "missing or invalid bearer token"})
                    return
            try:
                length = int(self.headers.get("Content-Length") or 0)
                if length > MAX_BODY:
                    self._send(413, {"error": "request body too large"})
                    return
                raw = self.rfile.read(length) if length else b""
                body = json.loads(raw) if raw else {}
                if not isinstance(body, dict):
                    raise TiccError("request body must be a JSON object")
                query = {k: v[-1] for k, v in parse_qs(url.query).items()}
                body_actor = body.pop("actor", None) or {}
                actor = Actor(
                    name=self.headers.get("X-Ticc-Actor") or body_actor.get("name") or "anonymous",
                    kind=self.headers.get("X-Ticc-Actor-Kind") or body_actor.get("kind") or "human",
                )
                status, result = api.dispatch(method, url.path, actor, query, body)
                self._send(status, result)
            except TiccError as e:
                self._send(e.http_status, {"error": str(e)})
            except (ValueError, KeyError, TypeError) as e:
                self._send(HTTPStatus.BAD_REQUEST, {"error": f"bad request: {e}"})
            except Exception as e:  # pragma: no cover - last resort
                self.log_error("internal error: %r", e)
                self._send(500, {"error": "internal server error"})

        def do_GET(self) -> None:
            self._handle("GET")

        def do_POST(self) -> None:
            self._handle("POST")

        def do_PATCH(self) -> None:
            self._handle("PATCH")

    return Handler


def make_server(store: Store, host: str = "127.0.0.1", port: int = 8765, token: str | None = None):
    token = token if token is not None else os.environ.get("TICC_TOKEN") or None
    httpd = ThreadingHTTPServer((host, port), make_handler(Api(store), token))
    httpd.daemon_threads = True
    return httpd


def serve(store: Store, host: str = "127.0.0.1", port: int = 8765, token: str | None = None) -> None:
    httpd = make_server(store, host, port, token)
    print(f"ticc serving {store.path} on http://{host}:{httpd.server_address[1]}/", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
