# AGENTS.md

Instructions for AI agents working in this repository. The first part covers
developing ticc itself. The second part is the workflow for tracking your work
in ticc, and it stands on its own so other projects can copy it.

## Developing ticc

### Commands

ticc is pure Python stdlib (3.10+). There are no runtime dependencies and no
build step. A `.venv` exists at the repo root.

```bash
pip install -e '.[test]'                                      # editable install plus pytest
python -m pytest                                              # full suite
python -m pytest tests/test_ticc.py::test_claim_is_exclusive  # one test
python -m ticc <cmd>                                          # run without installing
ticc serve                                                    # web UI and API on :8765
```

There is no linter or formatter configured.

### Architecture

All the rules live in `ticc/store.py`. Status transitions, who may answer
what, blocking and unblocking, and claim exclusivity are all enforced by
`Store`. The other modules are thin adapters and should never reimplement a
rule:

- `client.py` has `RemoteStore`, which mirrors `Store`'s public methods over
  HTTP. `open_backend()` returns a `RemoteStore` when `TICC_URL` is set and a
  local `Store` otherwise. The CLI and the MCP server only talk to whatever it
  returns.
- `server.py` maps regex routes to `Store` calls and serves `web/index.html`,
  which is the entire UI in one file. `ticc serve` always uses a local `Store`.
- `cli.py` is the argparse front end. `run()` dispatches on `args.cmd`.
- `mcp.py` is a hand-rolled JSON-RPC server over stdio. Tool schemas are in
  `TOOLS`, dispatch is in `McpServer.handlers`, and `INSTRUCTIONS` is sent to
  the model on `initialize`. The MCP actor is always an agent.

Adding or changing an operation touches every layer: the `Store` method, the
matching `RemoteStore` method, the server route, the CLI subcommand, the MCP
tool (both `TOOLS` and `handlers`), and the HTTP API table in the README.
`test_remote_roundtrip` checks that local and remote behave the same.

Errors are `TiccError` subclasses with an `http_status`: `NotFound` is 404,
`Conflict` is 409, `Forbidden` is 403, and the base class is 400. The server
sends that status and `RemoteStore` turns it back into the same exception, so
callers see identical behavior locally and remotely.

Each `Store` holds one SQLite connection guarded by an `RLock`. Writes go
through `with self._tx() as conn:`, which issues `BEGIN IMMEDIATE` so
concurrent processes take turns. That is what makes `claim` and `next_issue`
atomic. File databases run in WAL mode.

`SCHEMA` uses `CREATE TABLE IF NOT EXISTS` and there is no migration
mechanism. If you add a column, existing `.ticc/ticc.db` files won't get it
unless you add an explicit migration in `Store.__init__`.

A blocking request saves the issue's `prior_status`, and `_maybe_unblock`
restores it only once the last pending blocking request is resolved.
`_transition` refuses status changes from agents while any blocking request is
pending, and moving an issue to `done` or `closed` cancels its pending
requests. Route status changes through `_transition` and `_set_status` so
these rules hold and every change writes a timeline event.

### Tests

Everything is in `tests/test_ticc.py`. The `store` fixture gives you a `Store`
on a temp file. The `remote` fixture starts a real server on port 0 in a
thread and wraps it in a `RemoteStore`. CLI tests use the `run_cli` helper,
which calls `cli.main()` in-process with `--db`, `--as` and `--kind` and
captures the output. `test_mcp_stdio_process` runs the MCP server as a real
subprocess over stdio.

### Dogfooding

This repo tracks its own work in ticc, in `.ticc/ticc.db` (gitignored).
Commands you run from the repo use that database unless `TICC_DB` or
`TICC_URL` is set, so point `TICC_DB` at a scratch path when you experiment by
hand. Otherwise you'll pollute the real backlog.

## Tracking work in ticc

This project tracks work in ticc. Use it to pick up work, say what you're
doing, file what you find, and ask a human when you need one. Use the MCP
tools if you have them (`claim_issue`, `report_progress` and so on).
Otherwise use the `ticc` CLI with `--json`. Set your identity once:

```bash
export TICC_ACTOR=<your-agent-name> TICC_ACTOR_KIND=agent
```

### 1. Pick up work

- If you're working on a specific issue, run `ticc claim <id>` first. If it
  fails because someone else holds it, don't use `--force`. Pick something
  else or ask.
- If you're looking for work, `ticc --json next [--kind bug]` claims the
  highest-priority unassigned open issue. Exit code 2 means there's nothing to
  do.
- Read the whole issue before you start: `ticc show <id>`. Humans often leave
  guidance in the comments.

### 2. Report progress

Post a short, factual update at each real milestone, not after every step:

```bash
ticc progress <id> "Reproduced. Root cause: X. Next: fix and regression test." -P 40
```

A good update says what changed, what you learned and what comes next.
Include links to PRs, commits or logs when you have them.

### 3. File what you find

Bugs, follow-ups and ideas outside your current scope go in new issues. Don't
quietly fix unrelated things, and don't forget them either:

```bash
ticc new bug "Title that states the problem" -b "Steps, expected, actual, where" -l area
ticc new feature "..."   # or: task
```

### 4. Ask a human when you need one

Ask before you act, not after. Ask when:

- you're stuck after a reasonable attempt (`help`),
- the action is risky, destructive, irreversible, expensive or
  security-relevant, touches production or shared data, or changes a public
  API (`approval`),
- there are several reasonable approaches and picking one is a product or team
  call (`decision`),
- your instructions say something needs sign-off.

```bash
ticc ask <id> "What I need, why, what I tried, impact of each answer" --approval --wait 3600
ticc ask <id> "Which approach?" --decision -o "A: ..." -o "B: ..." --wait 3600
ticc ask <id> "I'm stuck on X; tried Y and Z" --wait 3600     # help
```

Write the question so a busy person can answer it without opening anything
else. Exit codes from `--wait` and `ticc wait` are `0` for approved or
answered, `3` for rejected, `4` for cancelled and `5` for timed out. A timeout
means the request is still pending, so either wait again with
`ticc wait <request-id>` or work on something else and come back.

Then respect the answer:

- If it's approved, go ahead, but only within the scope you described.
- If it's rejected, don't do it. Read the note, then adjust or ask again.
- If it's answered, follow the guidance or the chosen option.
- If you sort it out yourself before anyone replies, withdraw it with
  `ticc cancel <request-id> -m "why"`.

While a blocking request is pending the issue is `blocked`, and the tracker
will refuse your status changes. Never try to work around a pending approval.
Only humans can answer approvals and decisions.

### 5. Finish

```bash
ticc status <id> in_review -m "Summary of what changed, how it was tested, links"
ticc done <id> -r fixed -m "Summary"              # when no human review is needed
ticc release <id> -m "Where I left off and why"   # if you have to stop early
```

Leave the issue in a state the next person, human or agent, can pick up from
its timeline alone.
