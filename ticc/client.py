"""HTTP client exposing the same interface as :class:`ticc.store.Store`.

Set ``TICC_URL`` (and optionally ``TICC_TOKEN``) to make the CLI and MCP
server talk to a shared ``ticc serve`` instance instead of a local database.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any, Iterable
from urllib.parse import urlencode

from .store import Actor, Conflict, Forbidden, NotFound, Store, TiccError

_ERRORS = {404: NotFound, 409: Conflict, 403: Forbidden}


class RemoteStore:
    def __init__(self, url: str, token: str | None = None, timeout: float = 30.0) -> None:
        self.url = url.rstrip("/")
        self.path = self.url
        self.token = token
        self.timeout = timeout

    def close(self) -> None:
        pass

    def _call(
        self,
        method: str,
        path: str,
        actor: Actor | None = None,
        body: dict[str, Any] | None = None,
        query: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> Any:
        url = self.url + path
        if query:
            q = {k: v for k, v in query.items() if v not in (None, "", False)}
            if q:
                url += "?" + urlencode(q)
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        if actor is not None:
            req.add_header("X-Ticc-Actor", actor.name)
            req.add_header("X-Ticc-Actor-Kind", actor.kind)
        if self.token:
            req.add_header("Authorization", f"Bearer {self.token}")
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout) as resp:
                return json.loads(resp.read() or b"null")
        except urllib.error.HTTPError as e:
            try:
                msg = json.loads(e.read()).get("error", str(e))
            except Exception:
                msg = str(e)
            raise _ERRORS.get(e.code, TiccError)(msg) from None
        except urllib.error.URLError as e:
            raise TiccError(f"cannot reach ticc server at {self.url}: {e.reason}") from None

    # -- issues ------------------------------------------------------------

    def create_issue(self, actor: Actor, title: str, **kw: Any) -> dict[str, Any]:
        if isinstance(kw.get("labels"), str):
            kw["labels"] = kw["labels"].split(",")
        return self._call("POST", "/api/issues", actor, {"title": title, **kw})

    def get_issue(self, issue_id: int, timeline: bool = True) -> dict[str, Any]:
        return self._call("GET", f"/api/issues/{int(issue_id)}")

    def list_issues(
        self,
        status: str | Iterable[str] | None = None,
        kind: str | None = None,
        assignee: str | None = None,
        label: str | None = None,
        query: str | None = None,
        reporter: str | None = None,
        include_closed: bool = False,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        if status is not None and not isinstance(status, str):
            status = ",".join(status)
        return self._call(
            "GET",
            "/api/issues",
            query={
                "status": status,
                "kind": kind,
                "assignee": assignee,
                "label": label,
                "q": query,
                "reporter": reporter,
                "all": "1" if include_closed else None,
                "limit": limit,
            },
        )

    def update_issue(self, actor: Actor, issue_id: int, **fields: Any) -> dict[str, Any]:
        return self._call("PATCH", f"/api/issues/{int(issue_id)}", actor, fields)

    def set_status(
        self, actor: Actor, issue_id: int, status: str, note: str = "", resolution: str | None = None
    ) -> dict[str, Any]:
        fields: dict[str, Any] = {"status": status, "note": note}
        if resolution is not None:
            fields["resolution"] = resolution
        return self.update_issue(actor, issue_id, **fields)

    def comment(self, actor: Actor, issue_id: int, body: str) -> dict[str, Any]:
        return self._call("POST", f"/api/issues/{int(issue_id)}/comments", actor, {"body": body})

    def report_progress(
        self, actor: Actor, issue_id: int, message: str, percent: int | None = None
    ) -> dict[str, Any]:
        return self._call(
            "POST",
            f"/api/issues/{int(issue_id)}/progress",
            actor,
            {"message": message, "percent": percent},
        )

    def claim(self, actor: Actor, issue_id: int, force: bool = False) -> dict[str, Any]:
        return self._call("POST", f"/api/issues/{int(issue_id)}/claim", actor, {"force": force})

    def release(self, actor: Actor, issue_id: int, note: str = "") -> dict[str, Any]:
        return self._call("POST", f"/api/issues/{int(issue_id)}/release", actor, {"note": note})

    def next_issue(
        self, actor: Actor, kind: str | None = None, label: str | None = None
    ) -> dict[str, Any] | None:
        return self._call("POST", "/api/issues/next", actor, {"kind": kind, "label": label})

    # -- requests ----------------------------------------------------------

    def request_human(
        self,
        actor: Actor,
        issue_id: int,
        question: str,
        kind: str = "help",
        options: Iterable[str] | None = None,
        blocking: bool = True,
    ) -> dict[str, Any]:
        return self._call(
            "POST",
            f"/api/issues/{int(issue_id)}/requests",
            actor,
            {"question": question, "kind": kind, "options": list(options or []), "blocking": blocking},
        )

    def get_request(self, request_id: int) -> dict[str, Any]:
        return self._call("GET", f"/api/requests/{int(request_id)}")

    def list_requests(
        self,
        status: str | None = "pending",
        issue_id: int | None = None,
        requested_by: str | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        return self._call(
            "GET",
            "/api/requests",
            query={
                "status": status or "all",
                "issue": issue_id,
                "requested_by": requested_by,
                "limit": limit,
            },
        )

    def respond(
        self, actor: Actor, request_id: int, response: str = "", decision: str | None = None
    ) -> dict[str, Any]:
        return self._call(
            "POST",
            f"/api/requests/{int(request_id)}/respond",
            actor,
            {"response": response, "decision": decision},
        )

    def cancel_request(self, actor: Actor, request_id: int, reason: str = "") -> dict[str, Any]:
        return self._call(
            "POST", f"/api/requests/{int(request_id)}/cancel", actor, {"reason": reason}
        )

    def wait_for_request(
        self, request_id: int, timeout: float = 60.0, poll: float = 1.0
    ) -> dict[str, Any]:
        import time

        deadline = time.monotonic() + max(0.0, float(timeout))
        while True:
            remaining = deadline - time.monotonic()
            chunk = max(0.0, min(remaining, 60.0))
            req = self._call(
                "GET",
                f"/api/requests/{int(request_id)}",
                query={"wait": f"{chunk:.1f}" if chunk > 0 else None},
                timeout=chunk + 15,
            )
            if req["status"] != "pending" or time.monotonic() >= deadline:
                return req

    # -- misc --------------------------------------------------------------

    def events_since(self, after_id: int = 0, limit: int = 200) -> list[dict[str, Any]]:
        return self._call("GET", "/api/events", query={"after": after_id, "limit": limit})

    def stats(self) -> dict[str, Any]:
        return self._call("GET", "/api/stats")


def open_backend(db: str | None = None, url: str | None = None) -> Store | RemoteStore:
    """Return a remote client if a URL is configured, else a local Store."""
    url = url or os.environ.get("TICC_URL")
    if url:
        return RemoteStore(url, token=os.environ.get("TICC_TOKEN"))
    return Store(db)
