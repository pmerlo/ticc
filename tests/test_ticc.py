import io
import json
import subprocess
import sys
import threading

import pytest

from ticc import cli
from ticc.client import RemoteStore
from ticc.mcp import McpServer
from ticc.server import make_server
from ticc.store import Actor, Conflict, Forbidden, NotFound, Store, TiccError

ALICE = Actor("alice", "human")
BOB = Actor("bob", "human")
BOT = Actor("bot-1", "agent")
BOT2 = Actor("bot-2", "agent")


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "t.db")
    yield s
    s.close()


@pytest.fixture
def remote(tmp_path):
    s = Store(tmp_path / "srv.db")
    httpd = make_server(s, port=0, token="")
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    yield RemoteStore(f"http://127.0.0.1:{httpd.server_address[1]}")
    httpd.shutdown()
    httpd.server_close()
    s.close()


# ----------------------------------------------------------------- issues


def test_create_and_list(store):
    a = store.create_issue(ALICE, "Crash on save", kind="bug", priority="high", labels="ui, io")
    store.create_issue(BOT, "Dark mode", kind="feature", priority="low")
    assert a["status"] == "open" and a["labels"] == ["ui", "io"]
    titles = [i["title"] for i in store.list_issues()]
    assert titles == ["Crash on save", "Dark mode"]  # priority order
    assert [i["title"] for i in store.list_issues(kind="feature")] == ["Dark mode"]
    assert [i["title"] for i in store.list_issues(label="io")] == ["Crash on save"]
    assert [i["title"] for i in store.list_issues(query="dark")] == ["Dark mode"]
    assert store.list_issues(reporter="bot-1")[0]["reporter_kind"] == "agent"


def test_validation(store):
    with pytest.raises(TiccError):
        store.create_issue(ALICE, "  ")
    with pytest.raises(TiccError):
        store.create_issue(ALICE, "x", kind="epic")
    with pytest.raises(NotFound):
        store.get_issue(99)
    with pytest.raises(TiccError):
        Actor("x", "robot")


def test_progress_starts_work_and_records_percent(store):
    i = store.create_issue(ALICE, "Fix it", kind="bug")
    store.report_progress(BOT, i["id"], "found root cause", 40)
    got = store.get_issue(i["id"])
    assert got["status"] == "in_progress" and got["progress"] == 40
    assert [e["type"] for e in got["events"]] == ["created", "progress", "status"]
    with pytest.raises(TiccError):
        store.report_progress(BOT, i["id"], "x", 101)


def test_claim_is_exclusive(store):
    i = store.create_issue(ALICE, "Task")
    store.claim(BOT, i["id"])
    with pytest.raises(Conflict):
        store.claim(BOT2, i["id"])
    store.claim(BOT, i["id"])  # idempotent for holder
    got = store.claim(BOT2, i["id"], force=True)
    assert got["assignee"] == "bot-2" and got["assignee_kind"] == "agent"


def test_next_issue_takes_highest_priority_unassigned(store):
    store.create_issue(ALICE, "low", priority="low")
    hi = store.create_issue(ALICE, "high", priority="high")
    taken = store.create_issue(ALICE, "critical but taken", priority="critical", assignee="bob")
    got = store.next_issue(BOT)
    assert got["id"] == hi["id"] and got["status"] == "in_progress"
    assert store.next_issue(BOT2)["title"] == "low"
    assert store.next_issue(BOT2) is None
    assert taken["assignee"] == "bob"


def test_release_returns_to_queue(store):
    i = store.create_issue(ALICE, "t")
    store.claim(BOT, i["id"])
    with pytest.raises(Forbidden):
        store.release(BOT2, i["id"])
    got = store.release(BOT, i["id"], "out of time")
    assert got["assignee"] is None and got["status"] == "open"


def test_update_issue_and_edit_events(store):
    i = store.create_issue(ALICE, "t")
    store.update_issue(ALICE, i["id"], title="new", priority="low", labels=["a"], body="b")
    got = store.get_issue(i["id"])
    assert (got["title"], got["priority"], got["labels"], got["body"]) == ("new", "low", ["a"], "b")
    edit = [e for e in got["events"] if e["type"] == "edit"][0]
    assert edit["data"]["body"] == "(edited)"
    with pytest.raises(TiccError):
        store.update_issue(ALICE, i["id"], reporter="mallory")


# ------------------------------------------------------- human intervention


def test_help_request_blocks_and_unblocks(store):
    i = store.create_issue(ALICE, "t")
    store.claim(BOT, i["id"])
    r = store.request_human(BOT, i["id"], "Which DB creds?")
    assert store.get_issue(i["id"])["status"] == "blocked"
    assert [x["id"] for x in store.list_requests()] == [r["id"]]
    # A help request can be answered by anyone, including another agent.
    done = store.respond(BOT2, r["id"], "use the staging ones")
    assert done["status"] == "answered" and done["response"] == "use the staging ones"
    assert store.get_issue(i["id"])["status"] == "in_progress"
    assert store.list_requests() == []


def test_approval_only_by_humans(store):
    i = store.create_issue(ALICE, "Migrate DB")
    store.claim(BOT, i["id"])
    r = store.request_human(BOT, i["id"], "OK to drop table users_old?", kind="approval")
    with pytest.raises(Forbidden):
        store.respond(BOT, r["id"], decision="approve")
    with pytest.raises(TiccError):
        store.respond(ALICE, r["id"], decision="maybe")
    got = store.respond(ALICE, r["id"], "go ahead", decision="approve")
    assert got["status"] == "approved" and got["responded_by"] == "alice"
    with pytest.raises(Conflict):
        store.respond(ALICE, r["id"], decision="reject")


def test_agent_cannot_bypass_pending_request(store):
    i = store.create_issue(ALICE, "t")
    store.claim(BOT, i["id"])
    store.request_human(BOT, i["id"], "May I deploy?", kind="approval")
    for status in ("in_progress", "done", "closed"):
        with pytest.raises(Conflict):
            store.set_status(BOT, i["id"], status)
    # Humans can override; finishing auto-cancels outstanding requests.
    store.set_status(ALICE, i["id"], "closed", resolution="wontfix")
    reqs = store.get_issue(i["id"])["requests"]
    assert reqs[0]["status"] == "cancelled"


def test_decision_request(store):
    i = store.create_issue(ALICE, "t")
    with pytest.raises(TiccError):
        store.request_human(BOT, i["id"], "pick", kind="decision", options=["only one"])
    r = store.request_human(BOT, i["id"], "Which lib?", kind="decision", options=["requests", "httpx"])
    with pytest.raises(TiccError):
        store.respond(ALICE, r["id"], decision="urllib3")
    got = store.respond(ALICE, r["id"], "lighter", decision="2")  # by index
    assert got["status"] == "answered" and got["response"].startswith("httpx")


def test_multiple_requests_restore_original_status(store):
    i = store.create_issue(ALICE, "t")
    store.set_status(ALICE, i["id"], "in_review")
    r1 = store.request_human(BOT, i["id"], "q1")
    r2 = store.request_human(BOT, i["id"], "q2", kind="approval")
    store.respond(ALICE, r1["id"], "a1")
    assert store.get_issue(i["id"])["status"] == "blocked"
    store.respond(ALICE, r2["id"], decision="reject")
    assert store.get_issue(i["id"])["status"] == "in_review"


def test_non_blocking_request_and_cancel(store):
    i = store.create_issue(ALICE, "t")
    store.claim(BOT, i["id"])
    r = store.request_human(BOT, i["id"], "FYI, thoughts?", blocking=False)
    assert store.get_issue(i["id"])["status"] == "in_progress"
    with pytest.raises(Forbidden):
        store.cancel_request(BOT2, r["id"])
    assert store.cancel_request(BOT, r["id"], "figured it out")["status"] == "cancelled"


def test_human_override_while_blocked(store):
    i = store.create_issue(ALICE, "t")
    r = store.request_human(BOT, i["id"], "q")
    store.set_status(ALICE, i["id"], "in_progress")  # human un-blocks manually
    store.respond(ALICE, r["id"], "a")
    assert store.get_issue(i["id"])["status"] == "in_progress"


def test_wait_for_request(store):
    i = store.create_issue(ALICE, "t")
    r = store.request_human(BOT, i["id"], "q", kind="approval")
    assert store.wait_for_request(r["id"], timeout=0.1, poll=0.05)["status"] == "pending"
    threading.Timer(0.2, lambda: store.respond(ALICE, r["id"], decision="approve")).start()
    assert store.wait_for_request(r["id"], timeout=5, poll=0.05)["status"] == "approved"


def test_stats_and_events(store):
    i = store.create_issue(ALICE, "t", kind="bug")
    store.request_human(BOT, i["id"], "q")
    s = store.stats()
    assert s["pending_requests"] == 1 and s["by_status"]["blocked"] == 1
    assert s["open_by_kind"]["bug"] == 1
    assert store.events_since(0)[0]["issue_title"] == "t"


# ---------------------------------------------------------------- HTTP API


def test_remote_roundtrip(remote):
    i = remote.create_issue(BOT, "Remote bug", kind="bug", labels="a,b")
    assert i["reporter_kind"] == "agent" and i["labels"] == ["a", "b"]
    remote.claim(BOT, i["id"])
    remote.report_progress(BOT, i["id"], "halfway", 50)
    r = remote.request_human(BOT, i["id"], "approve?", kind="approval")
    with pytest.raises(Forbidden):
        remote.respond(BOT, r["id"], decision="approve")
    with pytest.raises(Conflict):
        remote.set_status(BOT, i["id"], "done")
    threading.Timer(0.3, lambda: remote.respond(ALICE, r["id"], decision="approve")).start()
    assert remote.wait_for_request(r["id"], timeout=5)["status"] == "approved"
    remote.set_status(BOT, i["id"], "done", note="shipped", resolution="fixed")
    got = remote.get_issue(i["id"])
    assert got["status"] == "done" and got["resolution"] == "fixed"
    assert remote.list_issues() == []
    assert len(remote.list_issues(include_closed=True)) == 1
    with pytest.raises(NotFound):
        remote.get_issue(42)
    assert remote.next_issue(BOT) is None


def test_server_auth_and_ui(tmp_path):
    import urllib.error
    import urllib.request

    s = Store(tmp_path / "a.db")
    httpd = make_server(s, port=0, token="sekrit")
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        html = urllib.request.urlopen(base + "/").read()
        assert b"<title>ticc</title>" in html
        with pytest.raises(urllib.error.HTTPError) as ei:
            urllib.request.urlopen(base + "/api/issues")
        assert ei.value.code == 401
        ok = RemoteStore(base, token="sekrit")
        assert ok.stats()["pending_requests"] == 0
    finally:
        httpd.shutdown()
        httpd.server_close()
        s.close()


# --------------------------------------------------------------------- CLI


def run_cli(tmp_path, *argv, actor=("alice", "human"), stdin=None):
    env_args = ["--db", str(tmp_path / "cli.db"), "--as", actor[0], "--kind", actor[1]]
    out, err = io.StringIO(), io.StringIO()
    old = sys.stdout, sys.stderr, sys.stdin
    sys.stdout, sys.stderr = out, err
    if stdin is not None:
        sys.stdin = io.StringIO(stdin)
    try:
        code = cli.main(env_args + list(argv))
    finally:
        sys.stdout, sys.stderr, sys.stdin = old
    return code, out.getvalue(), err.getvalue()


def test_cli_flow(tmp_path):
    code, out, _ = run_cli(tmp_path, "--json", "new", "bug", "Boom", "-p", "high", "-b", "-",
                           stdin="steps:\n1. click")
    assert code == 0 and json.loads(out)["body"] == "steps:\n1. click"
    bot = ("bot", "agent")
    code, out, _ = run_cli(tmp_path, "--json", "next", actor=bot)
    assert code == 0 and json.loads(out)["assignee"] == "bot"
    assert run_cli(tmp_path, "progress", "1", "repro'd", "-P", "30", actor=bot)[0] == 0
    code, out, _ = run_cli(tmp_path, "--json", "ask", "1", "ship it?", "--approval", actor=bot)
    rid = json.loads(out)["id"]
    # agent waiting times out -> exit 5
    assert run_cli(tmp_path, "wait", str(rid), "-t", "0.1", actor=bot)[0] == 5
    # agent cannot approve
    code, _, err = run_cli(tmp_path, "approve", str(rid), actor=bot)
    assert code == 1 and "only humans" in err
    code, out, _ = run_cli(tmp_path, "inbox")
    assert "ship it?" in out
    assert run_cli(tmp_path, "reject", str(rid), "-m", "add tests first")[0] == 0
    assert run_cli(tmp_path, "wait", str(rid), actor=bot)[0] == 3
    assert run_cli(tmp_path, "done", "1", "-r", "fixed", actor=bot)[0] == 0
    code, out, _ = run_cli(tmp_path, "show", "1")
    assert "add tests first" in out and "done" in out
    assert run_cli(tmp_path, "next", actor=bot)[0] == 2
    code, out, _ = run_cli(tmp_path, "list", "--all")
    assert "#1" in out


def test_cli_decision(tmp_path):
    run_cli(tmp_path, "new", "task", "Pick")
    code, out, _ = run_cli(tmp_path, "--json", "ask", "1", "which?", "--decision",
                           "-o", "A", "-o", "B", actor=("bot", "agent"))
    rid = json.loads(out)["id"]
    code, out, _ = run_cli(tmp_path, "--json", "answer", str(rid), "-c", "B")
    assert code == 0 and json.loads(out)["response"] == "B"


# --------------------------------------------------------------------- MCP


def test_mcp_tools(store):
    srv = McpServer(store, BOT)
    init = srv.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                       "params": {"protocolVersion": "2025-06-18"}})
    assert init["result"]["serverInfo"]["name"] == "ticc"
    assert srv.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
    tools = srv.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})["result"]["tools"]
    assert {"request_human", "wait_for_response", "report_progress"} <= {t["name"] for t in tools}

    def call(name, **args):
        res = srv.handle({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                          "params": {"name": name, "arguments": args}})["result"]
        return res["isError"], json.loads(res["content"][0]["text"])

    err, issue = call("create_issue", title="found a bug", kind="bug")
    assert not err and issue["reporter"] == "bot-1"
    err, _ = call("claim_issue", issue_id=issue["id"])
    err, req = call("request_human", issue_id=issue["id"], kind="approval", question="ok?")
    assert not err
    err, res = call("wait_for_response", request_id=req["id"], timeout_seconds=0.1)
    assert res["status"] == "pending"
    err, res = call("update_issue", issue_id=issue["id"], status="done")
    assert err and "pending" in res["error"]
    err, res = call("list_issues", assignee="me")
    assert res[0]["pending_requests"] == 1
    err, res = call("nope")
    assert err


def test_mcp_stdio_process(tmp_path):
    msgs = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2024-11-05"}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
         "params": {"name": "create_issue", "arguments": {"title": "x", "kind": "task"}}},
    ]
    proc = subprocess.run(
        [sys.executable, "-m", "ticc", "--db", str(tmp_path / "m.db"), "mcp"],
        input="\n".join(json.dumps(m) for m in msgs) + "\n",
        capture_output=True, text=True, timeout=30,
    )
    lines = [json.loads(line) for line in proc.stdout.splitlines()]
    assert [m["id"] for m in lines] == [1, 2]
    assert lines[0]["result"]["protocolVersion"] == "2024-11-05"
    assert json.loads(lines[1]["result"]["content"][0]["text"])["reporter_kind"] == "agent"


# ------------------------------------------------------------- deployment


def test_healthz_skips_token(tmp_path):
    import urllib.error
    import urllib.request

    s = Store(tmp_path / "h.db")
    httpd = make_server(s, port=0, token="sekrit")
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        assert json.loads(urllib.request.urlopen(base + "/healthz").read()) == {"ok": True}
        with pytest.raises(urllib.error.HTTPError):
            urllib.request.urlopen(base + "/api/stats")
    finally:
        httpd.shutdown()
        httpd.server_close()
        s.close()


def test_serve_reads_port_and_host_from_env(monkeypatch):
    monkeypatch.setenv("PORT", "4321")
    monkeypatch.setenv("TICC_HOST", "0.0.0.0")
    args = cli.build_parser().parse_args(["serve"])
    assert (args.host, args.port) == ("0.0.0.0", 4321)
    monkeypatch.delenv("PORT")
    monkeypatch.delenv("TICC_HOST")
    args = cli.build_parser().parse_args(["serve"])
    assert (args.host, args.port) == ("127.0.0.1", 8765)


def test_db_defaults_to_railway_volume(monkeypatch, tmp_path):
    from ticc.store import default_db_path

    monkeypatch.delenv("TICC_DB", raising=False)
    monkeypatch.setenv("RAILWAY_VOLUME_MOUNT_PATH", str(tmp_path))
    assert default_db_path() == tmp_path / "ticc.db"
    monkeypatch.setenv("TICC_DB", "/elsewhere/x.db")  # explicit setting wins
    assert str(default_db_path()) == "/elsewhere/x.db"


def test_startup_warnings():
    from ticc.server import startup_warnings

    assert startup_warnings("127.0.0.1", None, "db", {}) == []
    assert "TICC_TOKEN" in startup_warnings("0.0.0.0", None, "db", {})[0]
    assert startup_warnings("0.0.0.0", "t", "db", {}) == []
    on_railway = {"RAILWAY_ENVIRONMENT": "production"}
    assert "volume" in startup_warnings("0.0.0.0", "t", "db", on_railway)[0]
    with_volume = {**on_railway, "RAILWAY_VOLUME_MOUNT_PATH": "/data"}
    assert startup_warnings("0.0.0.0", "t", "db", with_volume) == []
