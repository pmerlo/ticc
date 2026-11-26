"""Core domain logic and SQLite persistence for ticc.

Everything that mutates state goes through :class:`Store`, so the CLI, the
HTTP API, the web UI and the MCP server all share identical rules.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

# --------------------------------------------------------------------------
# Vocabulary
# --------------------------------------------------------------------------

ISSUE_KINDS = ("bug", "feature", "task")
PRIORITIES = ("critical", "high", "medium", "low")
STATUSES = ("open", "in_progress", "blocked", "in_review", "done", "closed")
TERMINAL_STATUSES = ("done", "closed")
ACTOR_KINDS = ("human", "agent")

# help     -> free-form answer from a human ("I'm stuck, how do I ...?")
# approval -> approve / reject ("May I drop the legacy table?")
# decision -> pick one of the supplied options
REQUEST_KINDS = ("help", "approval", "decision")
REQUEST_STATUSES = ("pending", "answered", "approved", "rejected", "cancelled")

EVENT_TYPES = (
    "created",
    "comment",
    "progress",
    "status",
    "assign",
    "edit",
    "request",
    "response",
)

_PRIORITY_RANK = {p: i for i, p in enumerate(PRIORITIES)}


class TiccError(Exception):
    """Base error. ``http_status`` lets the API map errors to responses."""

    http_status = 400


class NotFound(TiccError):
    http_status = 404


class Conflict(TiccError):
    http_status = 409


class Forbidden(TiccError):
    http_status = 403


@dataclass(frozen=True)
class Actor:
    """Who is performing an action. Agents and humans are first-class."""

    name: str
    kind: str = "human"

    def __post_init__(self) -> None:
        if not self.name or not self.name.strip():
            raise TiccError("actor name is required")
        if self.kind not in ACTOR_KINDS:
            raise TiccError(f"actor kind must be one of {ACTOR_KINDS}, got {self.kind!r}")

    @classmethod
    def from_env(cls, default_kind: str = "human") -> "Actor":
        kind = os.environ.get("TICC_ACTOR_KIND", default_kind)
        name = os.environ.get("TICC_ACTOR") or (
            os.environ.get("USER") or os.environ.get("USERNAME") or kind
        )
        return cls(name=name, kind=kind)

    def as_dict(self) -> dict[str, str]:
        return {"name": self.name, "kind": self.kind}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def default_db_path() -> Path:
    env = os.environ.get("TICC_DB")
    if env:
        return Path(env)
    # On Railway, a volume attached to the service is the only storage that
    # survives redeploys; Railway exposes its mount point in this variable.
    volume = os.environ.get("RAILWAY_VOLUME_MOUNT_PATH")
    if volume:
        return Path(volume) / "ticc.db"
    # Walk up from cwd looking for an existing .ticc directory (like git).
    cwd = Path.cwd()
    for d in (cwd, *cwd.parents):
        candidate = d / ".ticc" / "ticc.db"
        if candidate.exists():
            return candidate
    return cwd / ".ticc" / "ticc.db"


SCHEMA = """
CREATE TABLE IF NOT EXISTS issues (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    kind          TEXT NOT NULL,
    title         TEXT NOT NULL,
    body          TEXT NOT NULL DEFAULT '',
    status        TEXT NOT NULL DEFAULT 'open',
    priority      TEXT NOT NULL DEFAULT 'medium',
    labels        TEXT NOT NULL DEFAULT '[]',
    reporter      TEXT NOT NULL,
    reporter_kind TEXT NOT NULL,
    assignee      TEXT,
    assignee_kind TEXT,
    progress      INTEGER,
    resolution    TEXT,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    closed_at     TEXT
);
CREATE INDEX IF NOT EXISTS issues_status ON issues(status);
CREATE INDEX IF NOT EXISTS issues_assignee ON issues(assignee);

CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    issue_id    INTEGER NOT NULL REFERENCES issues(id) ON DELETE CASCADE,
    type        TEXT NOT NULL,
    actor       TEXT NOT NULL,
    actor_kind  TEXT NOT NULL,
    body        TEXT NOT NULL DEFAULT '',
    data        TEXT NOT NULL DEFAULT '{}',
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS events_issue ON events(issue_id, id);

CREATE TABLE IF NOT EXISTS requests (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    issue_id            INTEGER NOT NULL REFERENCES issues(id) ON DELETE CASCADE,
    kind                TEXT NOT NULL,
    question            TEXT NOT NULL,
    options             TEXT NOT NULL DEFAULT '[]',
    blocking            INTEGER NOT NULL DEFAULT 1,
    status              TEXT NOT NULL DEFAULT 'pending',
    requested_by        TEXT NOT NULL,
    requested_by_kind   TEXT NOT NULL,
    response            TEXT,
    responded_by        TEXT,
    responded_by_kind   TEXT,
    prior_status        TEXT,
    created_at          TEXT NOT NULL,
    resolved_at         TEXT
);
CREATE INDEX IF NOT EXISTS requests_status ON requests(status);
CREATE INDEX IF NOT EXISTS requests_issue ON requests(issue_id);
"""


def _clean_labels(labels: Iterable[str] | str | None) -> list[str]:
    if labels is None:
        return []
    if isinstance(labels, str):
        labels = labels.split(",")
    out: list[str] = []
    for label in labels:
        label = str(label).strip()
        if label and label not in out:
            out.append(label)
    return out


def _check(value: str | None, allowed: tuple[str, ...], field: str) -> None:
    if value is not None and value not in allowed:
        raise TiccError(f"{field} must be one of {', '.join(allowed)}; got {value!r}")


class Store:
    """Thread-safe facade over a SQLite database."""

    def __init__(self, path: str | os.PathLike[str] | None = None) -> None:
        self.path = Path(path) if path is not None else default_db_path()
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            str(self.path), check_same_thread=False, isolation_level=None, timeout=30
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        if str(self.path) != ":memory:":
            self._conn.execute("PRAGMA journal_mode = WAL")
        self._conn.executescript(SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # -- transaction helper ------------------------------------------------

    class _Tx:
        def __init__(self, store: "Store") -> None:
            self.store = store

        def __enter__(self) -> sqlite3.Connection:
            self.store._lock.acquire()
            # IMMEDIATE takes the write lock up front so concurrent processes
            # (e.g. two agents claiming the same issue) serialise correctly.
            self.store._conn.execute("BEGIN IMMEDIATE")
            return self.store._conn

        def __exit__(self, exc_type, exc, tb) -> None:
            try:
                if exc_type is None:
                    self.store._conn.execute("COMMIT")
                else:
                    self.store._conn.execute("ROLLBACK")
            finally:
                self.store._lock.release()

    def _tx(self) -> "Store._Tx":
        return Store._Tx(self)

    def _query(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, tuple(params)).fetchall()

    # -- row conversion ----------------------------------------------------

    @staticmethod
    def _issue_dict(row: sqlite3.Row) -> dict[str, Any]:
        d = dict(row)
        d["labels"] = json.loads(d["labels"] or "[]")
        return d

    @staticmethod
    def _event_dict(row: sqlite3.Row) -> dict[str, Any]:
        d = dict(row)
        d["data"] = json.loads(d["data"] or "{}")
        return d

    @staticmethod
    def _request_dict(row: sqlite3.Row) -> dict[str, Any]:
        d = dict(row)
        d["options"] = json.loads(d["options"] or "[]")
        d["blocking"] = bool(d["blocking"])
        return d

    @staticmethod
    def _add_event(
        conn: sqlite3.Connection,
        issue_id: int,
        actor: Actor,
        type_: str,
        body: str = "",
        data: dict[str, Any] | None = None,
        ts: str | None = None,
    ) -> int:
        ts = ts or now_iso()
        cur = conn.execute(
            "INSERT INTO events (issue_id, type, actor, actor_kind, body, data, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (issue_id, type_, actor.name, actor.kind, body, json.dumps(data or {}), ts),
        )
        conn.execute("UPDATE issues SET updated_at = ? WHERE id = ?", (ts, issue_id))
        return int(cur.lastrowid)

    def _issue_row(self, conn: sqlite3.Connection, issue_id: int) -> sqlite3.Row:
        row = conn.execute("SELECT * FROM issues WHERE id = ?", (int(issue_id),)).fetchone()
        if row is None:
            raise NotFound(f"issue #{issue_id} not found")
        return row

    def _request_row(self, conn: sqlite3.Connection, request_id: int) -> sqlite3.Row:
        row = conn.execute("SELECT * FROM requests WHERE id = ?", (int(request_id),)).fetchone()
        if row is None:
            raise NotFound(f"request R{request_id} not found")
        return row

    @staticmethod
    def _set_status(
        conn: sqlite3.Connection, issue_id: int, old: str, new: str, actor: Actor, note: str = ""
    ) -> None:
        if old == new:
            return
        ts = now_iso()
        closed_at = ts if new in TERMINAL_STATUSES else None
        conn.execute(
            "UPDATE issues SET status = ?, closed_at = ?, updated_at = ? WHERE id = ?",
            (new, closed_at, ts, issue_id),
        )
        Store._add_event(conn, issue_id, actor, "status", note, {"from": old, "to": new}, ts)

    # -- issues ------------------------------------------------------------

    def create_issue(
        self,
        actor: Actor,
        title: str,
        kind: str = "task",
        body: str = "",
        priority: str = "medium",
        labels: Iterable[str] | str | None = None,
        assignee: str | None = None,
        assignee_kind: str | None = None,
    ) -> dict[str, Any]:
        title = (title or "").strip()
        if not title:
            raise TiccError("title is required")
        _check(kind, ISSUE_KINDS, "kind")
        _check(priority, PRIORITIES, "priority")
        _check(assignee_kind, ACTOR_KINDS, "assignee_kind")
        if assignee and not assignee_kind:
            assignee_kind = "human"
        ts = now_iso()
        with self._tx() as conn:
            cur = conn.execute(
                "INSERT INTO issues (kind, title, body, status, priority, labels, reporter,"
                " reporter_kind, assignee, assignee_kind, created_at, updated_at)"
                " VALUES (?, ?, ?, 'open', ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    kind,
                    title,
                    body or "",
                    priority,
                    json.dumps(_clean_labels(labels)),
                    actor.name,
                    actor.kind,
                    assignee or None,
                    assignee_kind if assignee else None,
                    ts,
                    ts,
                ),
            )
            issue_id = int(cur.lastrowid)
            self._add_event(conn, issue_id, actor, "created", "", {"kind": kind}, ts)
        return self.get_issue(issue_id, timeline=False)

    def get_issue(self, issue_id: int, timeline: bool = True) -> dict[str, Any]:
        with self._lock:
            issue = self._issue_dict(self._issue_row(self._conn, issue_id))
            reqs = self._conn.execute(
                "SELECT * FROM requests WHERE issue_id = ? ORDER BY id", (issue["id"],)
            ).fetchall()
            issue["requests"] = [self._request_dict(r) for r in reqs]
            issue["pending_requests"] = sum(1 for r in reqs if r["status"] == "pending")
            if timeline:
                evs = self._conn.execute(
                    "SELECT * FROM events WHERE issue_id = ? ORDER BY id", (issue["id"],)
                ).fetchall()
                issue["events"] = [self._event_dict(e) for e in evs]
        return issue

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
        where: list[str] = []
        params: list[Any] = []
        if status:
            statuses = [status] if isinstance(status, str) else list(status)
            statuses = [s for part in statuses for s in str(part).split(",") if s]
            for s in statuses:
                _check(s, STATUSES, "status")
            where.append(f"status IN ({','.join('?' * len(statuses))})")
            params.extend(statuses)
        elif not include_closed:
            where.append(f"status NOT IN ({','.join('?' * len(TERMINAL_STATUSES))})")
            params.extend(TERMINAL_STATUSES)
        if kind:
            _check(kind, ISSUE_KINDS, "kind")
            where.append("kind = ?")
            params.append(kind)
        if assignee:
            if assignee in ("none", "-"):
                where.append("assignee IS NULL")
            else:
                where.append("assignee = ?")
                params.append(assignee)
        if reporter:
            where.append("reporter = ?")
            params.append(reporter)
        if label:
            where.append("EXISTS (SELECT 1 FROM json_each(issues.labels) WHERE value = ?)")
            params.append(label)
        if query:
            where.append("(title LIKE ? OR body LIKE ?)")
            params.extend([f"%{query}%", f"%{query}%"])
        sql = (
            "SELECT issues.*, (SELECT COUNT(*) FROM requests r WHERE r.issue_id = issues.id"
            " AND r.status = 'pending') AS pending_requests FROM issues"
        )
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += (
            " ORDER BY CASE priority WHEN 'critical' THEN 0 WHEN 'high' THEN 1"
            " WHEN 'medium' THEN 2 ELSE 3 END, updated_at DESC, id DESC LIMIT ?"
        )
        params.append(int(limit))
        return [self._issue_dict(r) for r in self._query(sql, params)]

    def update_issue(self, actor: Actor, issue_id: int, **fields: Any) -> dict[str, Any]:
        allowed = {
            "title",
            "body",
            "kind",
            "priority",
            "labels",
            "status",
            "assignee",
            "assignee_kind",
            "resolution",
            "note",
        }
        unknown = set(fields) - allowed
        if unknown:
            raise TiccError(f"cannot update field(s): {', '.join(sorted(unknown))}")
        note = fields.pop("note", None) or ""
        fields = {k: v for k, v in fields.items() if v is not None or k == "assignee"}
        _check(fields.get("kind"), ISSUE_KINDS, "kind")
        _check(fields.get("priority"), PRIORITIES, "priority")
        _check(fields.get("status"), STATUSES, "status")
        _check(fields.get("assignee_kind"), ACTOR_KINDS, "assignee_kind")
        with self._tx() as conn:
            row = self._issue_row(conn, issue_id)
            changes: dict[str, Any] = {}
            for key in ("title", "body", "kind", "priority", "resolution"):
                if key in fields and fields[key] != row[key]:
                    if key == "title" and not str(fields[key]).strip():
                        raise TiccError("title cannot be empty")
                    changes[key] = fields[key]
            if "labels" in fields:
                labels = _clean_labels(fields["labels"])
                if labels != json.loads(row["labels"]):
                    changes["labels"] = json.dumps(labels)
            if changes:
                sets = ", ".join(f"{k} = ?" for k in changes)
                conn.execute(
                    f"UPDATE issues SET {sets} WHERE id = ?", (*changes.values(), row["id"])
                )
                shown = {
                    k: (json.loads(v) if k == "labels" else v)
                    for k, v in changes.items()
                    if k != "body"
                }
                if "body" in changes:
                    shown["body"] = "(edited)"
                self._add_event(conn, row["id"], actor, "edit", "", shown)
            if "assignee" in fields:
                self._assign(conn, row, actor, fields["assignee"], fields.get("assignee_kind"))
            if "status" in fields:
                self._transition(conn, row["id"], actor, fields["status"], note)
        return self.get_issue(issue_id, timeline=False)

    def _transition(
        self, conn: sqlite3.Connection, issue_id: int, actor: Actor, new: str, note: str = ""
    ) -> None:
        row = self._issue_row(conn, issue_id)
        old = row["status"]
        if new == old:
            return
        pending = conn.execute(
            "SELECT COUNT(*) FROM requests WHERE issue_id = ? AND status = 'pending'"
            " AND blocking = 1",
            (issue_id,),
        ).fetchone()[0]
        if pending and actor.kind != "human":
            # The issue is waiting on a human. Agents must not route around an
            # approval by flipping the status; humans may override.
            raise Conflict(
                f"issue #{issue_id} has {pending} pending blocking request(s); "
                "wait for a human response or cancel your request first"
            )
        self._set_status(conn, issue_id, old, new, actor, note)
        if new in TERMINAL_STATUSES:
            # Auto-cancel any outstanding requests on a finished issue.
            conn.execute(
                "UPDATE requests SET status = 'cancelled', resolved_at = ?, responded_by = ?,"
                " responded_by_kind = ?, response = 'issue ' || ?"
                " WHERE issue_id = ? AND status = 'pending'",
                (now_iso(), actor.name, actor.kind, new, issue_id),
            )

    def _assign(
        self,
        conn: sqlite3.Connection,
        row: sqlite3.Row,
        actor: Actor,
        assignee: str | None,
        assignee_kind: str | None,
    ) -> None:
        assignee = (assignee or "").strip() or None
        if assignee is None:
            assignee_kind = None
        elif not assignee_kind:
            assignee_kind = actor.kind if assignee == actor.name else (row["assignee_kind"] or "human")
        if assignee == row["assignee"] and assignee_kind == row["assignee_kind"]:
            return
        conn.execute(
            "UPDATE issues SET assignee = ?, assignee_kind = ? WHERE id = ?",
            (assignee, assignee_kind, row["id"]),
        )
        self._add_event(
            conn,
            row["id"],
            actor,
            "assign",
            "",
            {"from": row["assignee"], "to": assignee, "to_kind": assignee_kind},
        )

    def set_status(
        self, actor: Actor, issue_id: int, status: str, note: str = "", resolution: str | None = None
    ) -> dict[str, Any]:
        _check(status, STATUSES, "status")
        with self._tx() as conn:
            self._issue_row(conn, issue_id)
            if resolution is not None:
                conn.execute("UPDATE issues SET resolution = ? WHERE id = ?", (resolution, issue_id))
            self._transition(conn, issue_id, actor, status, note)
        return self.get_issue(issue_id, timeline=False)

    def comment(self, actor: Actor, issue_id: int, body: str) -> dict[str, Any]:
        body = (body or "").strip()
        if not body:
            raise TiccError("comment body is required")
        with self._tx() as conn:
            self._issue_row(conn, issue_id)
            event_id = self._add_event(conn, issue_id, actor, "comment", body)
        return self._event(event_id)

    def report_progress(
        self, actor: Actor, issue_id: int, message: str, percent: int | None = None
    ) -> dict[str, Any]:
        """Record a progress update. Starts work on an open issue automatically."""
        message = (message or "").strip()
        if not message:
            raise TiccError("progress message is required")
        if percent is not None:
            percent = int(percent)
            if not 0 <= percent <= 100:
                raise TiccError("percent must be between 0 and 100")
        with self._tx() as conn:
            row = self._issue_row(conn, issue_id)
            if row["status"] in TERMINAL_STATUSES:
                raise Conflict(f"issue #{issue_id} is {row['status']}; reopen it first")
            if percent is not None:
                conn.execute("UPDATE issues SET progress = ? WHERE id = ?", (percent, issue_id))
            event_id = self._add_event(
                conn, issue_id, actor, "progress", message, {"percent": percent}
            )
            if row["status"] == "open":
                self._set_status(conn, issue_id, "open", "in_progress", actor)
        return self._event(event_id)

    def claim(self, actor: Actor, issue_id: int, force: bool = False) -> dict[str, Any]:
        """Atomically assign an issue to ``actor`` and mark it in progress.

        Fails with :class:`Conflict` if someone else already holds it, unless
        ``force`` is given. This is how multiple agents avoid duplicate work.
        """
        with self._tx() as conn:
            row = self._issue_row(conn, issue_id)
            if row["status"] in TERMINAL_STATUSES:
                raise Conflict(f"issue #{issue_id} is {row['status']}")
            if row["assignee"] and row["assignee"] != actor.name and not force:
                raise Conflict(f"issue #{issue_id} is already claimed by {row['assignee']}")
            self._assign(conn, row, actor, actor.name, actor.kind)
            if row["status"] == "open":
                self._set_status(conn, issue_id, "open", "in_progress", actor)
        return self.get_issue(issue_id, timeline=False)

    def release(self, actor: Actor, issue_id: int, note: str = "") -> dict[str, Any]:
        with self._tx() as conn:
            row = self._issue_row(conn, issue_id)
            if row["assignee"] and row["assignee"] != actor.name and actor.kind != "human":
                raise Forbidden(f"issue #{issue_id} is held by {row['assignee']}, not {actor.name}")
            self._assign(conn, row, actor, None, None)
            if row["status"] == "in_progress":
                self._set_status(conn, issue_id, "in_progress", "open", actor, note)
            elif note:
                self._add_event(conn, issue_id, actor, "comment", note)
        return self.get_issue(issue_id, timeline=False)

    def next_issue(
        self, actor: Actor, kind: str | None = None, label: str | None = None
    ) -> dict[str, Any] | None:
        """Claim the highest-priority unassigned open issue, or return None."""
        _check(kind, ISSUE_KINDS, "kind")
        for _ in range(5):  # retry if another worker wins the race
            candidates = self.list_issues(
                status="open", kind=kind, assignee="none", label=label, limit=1
            )
            if not candidates:
                return None
            try:
                return self.claim(actor, candidates[0]["id"])
            except Conflict:
                continue
        return None

    # -- human intervention requests ---------------------------------------

    def request_human(
        self,
        actor: Actor,
        issue_id: int,
        question: str,
        kind: str = "help",
        options: Iterable[str] | None = None,
        blocking: bool = True,
    ) -> dict[str, Any]:
        """Ask a human for help, an approval, or a decision.

        Blocking requests move the issue to ``blocked`` until every blocking
        request on it is resolved, at which point the previous status returns.
        """
        question = (question or "").strip()
        if not question:
            raise TiccError("question is required")
        _check(kind, REQUEST_KINDS, "kind")
        opts = [str(o).strip() for o in (options or []) if str(o).strip()]
        if kind == "decision" and len(opts) < 2:
            raise TiccError("a decision request needs at least two options")
        if kind == "approval" and opts:
            raise TiccError("approval requests take no options (the answer is approve/reject)")
        ts = now_iso()
        with self._tx() as conn:
            row = self._issue_row(conn, issue_id)
            if row["status"] in TERMINAL_STATUSES:
                raise Conflict(f"issue #{issue_id} is {row['status']}")
            prior = row["status"]
            if prior == "blocked":
                # Already blocked by an earlier request: inherit what it will
                # return to so we don't restore to 'blocked'.
                earlier = conn.execute(
                    "SELECT prior_status FROM requests WHERE issue_id = ? AND status = 'pending'"
                    " AND blocking = 1 ORDER BY id LIMIT 1",
                    (issue_id,),
                ).fetchone()
                prior = earlier["prior_status"] if earlier else "in_progress"
            elif prior == "open":
                prior = "in_progress"
            cur = conn.execute(
                "INSERT INTO requests (issue_id, kind, question, options, blocking, status,"
                " requested_by, requested_by_kind, prior_status, created_at)"
                " VALUES (?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?)",
                (
                    issue_id,
                    kind,
                    question,
                    json.dumps(opts),
                    1 if blocking else 0,
                    actor.name,
                    actor.kind,
                    prior,
                    ts,
                ),
            )
            req_id = int(cur.lastrowid)
            self._add_event(
                conn,
                issue_id,
                actor,
                "request",
                question,
                {"request_id": req_id, "kind": kind, "options": opts, "blocking": bool(blocking)},
                ts,
            )
            if blocking and row["status"] != "blocked":
                self._set_status(
                    conn, issue_id, row["status"], "blocked", actor, f"waiting on R{req_id}"
                )
        return self.get_request(req_id)

    def get_request(self, request_id: int) -> dict[str, Any]:
        with self._lock:
            req = self._request_dict(self._request_row(self._conn, request_id))
            issue = self._conn.execute(
                "SELECT title, status FROM issues WHERE id = ?", (req["issue_id"],)
            ).fetchone()
        req["issue_title"] = issue["title"]
        req["issue_status"] = issue["status"]
        return req

    def list_requests(
        self,
        status: str | None = "pending",
        issue_id: int | None = None,
        requested_by: str | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        where: list[str] = []
        params: list[Any] = []
        if status and status != "all":
            _check(status, REQUEST_STATUSES, "status")
            where.append("r.status = ?")
            params.append(status)
        if issue_id is not None:
            where.append("r.issue_id = ?")
            params.append(int(issue_id))
        if requested_by:
            where.append("r.requested_by = ?")
            params.append(requested_by)
        sql = (
            "SELECT r.*, i.title AS issue_title, i.status AS issue_status, i.priority AS"
            " issue_priority FROM requests r JOIN issues i ON i.id = r.issue_id"
        )
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += (
            " ORDER BY r.status = 'pending' DESC, CASE i.priority WHEN 'critical' THEN 0"
            " WHEN 'high' THEN 1 WHEN 'medium' THEN 2 ELSE 3 END, r.id LIMIT ?"
        )
        params.append(int(limit))
        return [self._request_dict(r) for r in self._query(sql, params)]

    def respond(
        self,
        actor: Actor,
        request_id: int,
        response: str = "",
        decision: str | None = None,
    ) -> dict[str, Any]:
        """Resolve a pending request.

        * ``help``     -> ``response`` text is required.
        * ``approval`` -> ``decision`` is ``approve`` or ``reject``.
        * ``decision`` -> ``decision`` must be one of the request's options.

        Approvals and decisions can only be given by humans; that is the whole
        point of asking for them.
        """
        response = (response or "").strip()
        with self._tx() as conn:
            req = self._request_row(conn, request_id)
            if req["status"] != "pending":
                raise Conflict(f"request R{request_id} is already {req['status']}")
            kind = req["kind"]
            if kind in ("approval", "decision") and actor.kind != "human":
                raise Forbidden(f"only humans can answer {kind} requests")
            if kind == "help":
                if not response and not decision:
                    raise TiccError("a response is required")
                new_status = "answered"
                answer = response or decision or ""
            elif kind == "approval":
                d = (decision or "").lower()
                if d in ("approve", "approved", "yes", "y"):
                    new_status = "approved"
                elif d in ("reject", "rejected", "no", "n", "deny"):
                    new_status = "rejected"
                else:
                    raise TiccError("approval needs decision 'approve' or 'reject'")
                answer = response
            else:
                options = json.loads(req["options"])
                choice = decision
                if choice is not None and choice not in options:
                    # Allow choosing by 1-based index for convenience.
                    if str(choice).isdigit() and 1 <= int(choice) <= len(options):
                        choice = options[int(choice) - 1]
                    else:
                        raise TiccError(f"decision must be one of: {', '.join(options)}")
                if choice is None:
                    raise TiccError(f"decision must be one of: {', '.join(options)}")
                new_status = "answered"
                answer = choice if not response else f"{choice}\n\n{response}"
            ts = now_iso()
            conn.execute(
                "UPDATE requests SET status = ?, response = ?, responded_by = ?,"
                " responded_by_kind = ?, resolved_at = ? WHERE id = ?",
                (new_status, answer, actor.name, actor.kind, ts, request_id),
            )
            self._add_event(
                conn,
                req["issue_id"],
                actor,
                "response",
                answer,
                {"request_id": int(request_id), "kind": kind, "outcome": new_status},
                ts,
            )
            self._maybe_unblock(conn, req, actor)
        return self.get_request(request_id)

    def cancel_request(self, actor: Actor, request_id: int, reason: str = "") -> dict[str, Any]:
        with self._tx() as conn:
            req = self._request_row(conn, request_id)
            if req["status"] != "pending":
                raise Conflict(f"request R{request_id} is already {req['status']}")
            if actor.kind != "human" and actor.name != req["requested_by"]:
                raise Forbidden("agents may only cancel their own requests")
            ts = now_iso()
            conn.execute(
                "UPDATE requests SET status = 'cancelled', response = ?, responded_by = ?,"
                " responded_by_kind = ?, resolved_at = ? WHERE id = ?",
                (reason or None, actor.name, actor.kind, ts, request_id),
            )
            self._add_event(
                conn,
                req["issue_id"],
                actor,
                "response",
                reason or "request cancelled",
                {"request_id": int(request_id), "kind": req["kind"], "outcome": "cancelled"},
                ts,
            )
            self._maybe_unblock(conn, req, actor)
        return self.get_request(request_id)

    def _maybe_unblock(self, conn: sqlite3.Connection, req: sqlite3.Row, actor: Actor) -> None:
        if not req["blocking"]:
            return
        issue = self._issue_row(conn, req["issue_id"])
        if issue["status"] != "blocked":
            return
        remaining = conn.execute(
            "SELECT prior_status FROM requests WHERE issue_id = ? AND status = 'pending'"
            " AND blocking = 1 ORDER BY id",
            (req["issue_id"],),
        ).fetchall()
        if remaining:
            return
        target = req["prior_status"] or "in_progress"
        if target in ("blocked", *TERMINAL_STATUSES):
            target = "in_progress"
        self._set_status(conn, issue["id"], "blocked", target, actor, "unblocked")

    def wait_for_request(
        self, request_id: int, timeout: float = 60.0, poll: float = 1.0
    ) -> dict[str, Any]:
        """Block until a request is resolved or ``timeout`` seconds elapse."""
        deadline = time.monotonic() + max(0.0, float(timeout))
        while True:
            req = self.get_request(request_id)
            if req["status"] != "pending" or time.monotonic() >= deadline:
                return req
            time.sleep(min(poll, max(0.05, deadline - time.monotonic())))

    # -- misc --------------------------------------------------------------

    def _event(self, event_id: int) -> dict[str, Any]:
        rows = self._query("SELECT * FROM events WHERE id = ?", (event_id,))
        return self._event_dict(rows[0])

    def events_since(self, after_id: int = 0, limit: int = 200) -> list[dict[str, Any]]:
        """Global activity feed (useful for dashboards and notifiers)."""
        rows = self._query(
            "SELECT e.*, i.title AS issue_title FROM events e JOIN issues i ON i.id = e.issue_id"
            " WHERE e.id > ? ORDER BY e.id DESC LIMIT ?",
            (int(after_id), int(limit)),
        )
        return [self._event_dict(r) for r in rows]

    def stats(self) -> dict[str, Any]:
        by_status = {s: 0 for s in STATUSES}
        for row in self._query("SELECT status, COUNT(*) AS n FROM issues GROUP BY status"):
            by_status[row["status"]] = row["n"]
        by_kind = {k: 0 for k in ISSUE_KINDS}
        for row in self._query(
            "SELECT kind, COUNT(*) AS n FROM issues WHERE status NOT IN ('done','closed')"
            " GROUP BY kind"
        ):
            by_kind[row["kind"]] = row["n"]
        pending = self._query("SELECT COUNT(*) AS n FROM requests WHERE status = 'pending'")[0]["n"]
        return {"by_status": by_status, "open_by_kind": by_kind, "pending_requests": pending}
