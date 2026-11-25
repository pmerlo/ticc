# ticc — issue tracking for humans and agents

`ticc` is a small, self-hosted issue tracker built for teams where humans and
AI agents work on the same backlog. Both can:

- **file** bug reports, feature requests and tasks
- **claim** work atomically, so two agents never pick up the same issue
- **report progress** as they go (messages plus an optional percent complete)
- **ask a human** for help, an **approval**, or a **decision** when they are stuck
  or about to do something that needs sign-off, then **wait** for the answer

Humans get a web inbox ("Needs you") listing everything waiting on them, and
can approve, reject, answer or decide with one click.

It is pure Python stdlib (3.10+) on SQLite: no dependencies, no build step.

```
┌────────────┐   CLI / HTTP   ┌──────────────┐   MCP (stdio)   ┌───────────┐
│  humans    │ ─────────────▶ │  ticc store  │ ◀────────────── │  agents   │
│ (web UI,   │                │  (SQLite)    │                 │ (Claude,  │
│  CLI)      │ ◀── inbox ──── │              │ ── CLI/HTTP ──▶ │  scripts) │
└────────────┘                └──────────────┘                 └───────────┘
```

## Quick start

```bash
pip install -e .            # or run without installing: python -m ticc ...
ticc init                   # creates ./.ticc/ticc.db
ticc serve                  # web UI + API on http://127.0.0.1:8765/
```

File and work on issues from the CLI:

```bash
ticc new bug "Login fails for uppercase emails" -p high -l auth -b "Steps: ..."
ticc new feature "Export issues as CSV"
ticc list                    # active issues, highest priority first
ticc show 1                  # details + full timeline
```

## Concepts

**Issues** have a `kind` (`bug`, `feature`, `task`), a `priority` (`critical`,
`high`, `medium`, `low`), labels, an assignee, and a `status`:

| status        | meaning                                                       |
|---------------|---------------------------------------------------------------|
| `open`        | filed, nobody working on it                                    |
| `in_progress` | someone claimed it or reported progress                       |
| `blocked`     | waiting on a human (set automatically by blocking requests)   |
| `in_review`   | work finished, a human should check it                        |
| `done`        | completed                                                     |
| `closed`      | closed without completing (`--resolution wontfix`, etc.)      |

Every change lands on the issue's **timeline** with who made it and whether
they are a human or an agent (🤖).

**Requests** are how anyone — usually an agent — asks a human for intervention:

| kind       | the human…                          | outcome                 |
|------------|-------------------------------------|-------------------------|
| `help`     | writes a free-form answer           | `answered`              |
| `approval` | approves or rejects (with a note)   | `approved` / `rejected` |
| `decision` | picks one of the supplied options   | `answered`              |

Rules the tracker enforces:

- A **blocking** request (the default) moves the issue to `blocked`. When the
  last blocking request is resolved, the issue returns to its previous status.
- **Only humans can answer approvals and decisions.** Agents can answer `help`
  requests (one agent can unblock another) and can withdraw their own requests.
- While an issue has a pending blocking request, **agents cannot change its
  status** — so an agent can't mark work `done` and route around an approval it
  asked for. Humans can always override; finishing an issue cancels any
  outstanding requests on it.
- `claim` fails if someone else holds the issue (unless `--force`).

## Identity

Every action is attributed to an actor with a name and a kind (`human` or
`agent`):

```bash
export TICC_ACTOR=claude-1 TICC_ACTOR_KIND=agent   # or per command:
ticc --as claude-1 --agent progress 3 "tests passing" -P 80
```

The default is `$USER` as a `human`. The MCP server always acts as an agent.

> ticc trusts the identity a client declares. It is designed for a team's own
> humans and agents on a trusted network; set `TICC_TOKEN` (below) to keep
> others out, and don't expose it to the internet as-is.

## Agent workflow (CLI)

```bash
export TICC_ACTOR=claude-1 TICC_ACTOR_KIND=agent

ticc --json next --kind bug                       # claim top unassigned bug (exit 2 if none)
ticc progress 7 "Reproduced; root cause in tz parsing" -P 30
ticc new bug "Flaky test test_tz_roundtrip" -l tests   # file what you find along the way

# Need sign-off before something risky? Ask, then wait for the answer:
ticc ask 7 "Fix needs a migration rewriting 2M rows. OK to run it?" --approval --wait 3600
# exit code: 0 approved/answered · 3 rejected · 4 cancelled · 5 still pending (timed out)

ticc ask 7 "Which window?" --decision -o "Sat 02:00" -o "Sun 02:00" --wait 600
ticc ask 7 "Where are staging creds?"             # help request, returns R<id> immediately
ticc wait 12 --timeout 600                        # ...and wait for it later

ticc status 7 in_review -m "PR #431 ready; added regression test"
ticc done 7 -r fixed
ticc release 7 -m "Out of time; left notes in comment"   # hand it back instead
```

See [AGENTS.md](AGENTS.md) for the full agent protocol — it is written to be
dropped into an agent's instructions (e.g. `CLAUDE.md`).

## Agent workflow (MCP)

`ticc mcp` runs a [Model Context Protocol](https://modelcontextprotocol.io)
server on stdio, exposing `list_issues`, `get_issue`, `create_issue`,
`update_issue`, `claim_issue`, `release_issue`, `next_issue`, `comment`,
`report_progress`, `request_human`, `wait_for_response`, `list_requests` and
`cancel_request`. The server also sends usage instructions to the model on
connect.

For Claude Code, add to `.mcp.json` (see [`examples/mcp.json`](examples/mcp.json)):

```json
{
  "mcpServers": {
    "ticc": {
      "command": "python3",
      "args": ["-m", "ticc", "mcp"],
      "env": { "TICC_ACTOR": "claude-1", "TICC_URL": "http://tracker.internal:8765" }
    }
  }
}
```

## Human workflow

- **Web**: `ticc serve`, open the page, enter your name. The **Needs you** tab
  lists every pending request with Approve / Reject / Answer / Decide controls;
  the tab title shows the count. The **Issues** tab has filters, the full
  timeline, status/priority/assignee controls, and a comment box for steering
  agents. **Activity** is a live feed across all issues.
  The switch at the top left sets the theme to System, Light or Dark (saved
  per browser).
- **CLI**:

  ```bash
  ticc inbox                         # what's waiting on me
  ticc approve 4 -m "Go ahead"       # or: ticc reject 4 -m "Add tests first"
  ticc answer 5 "Use the staging creds in 1Password"
  ticc answer 6 -c 2 "Sunday is quieter"   # pick option 2 of a decision
  ticc activity                      # recent changes everywhere
  ```

## Sharing one tracker

Run one server and point everyone at it:

```bash
TICC_TOKEN=$(openssl rand -hex 16) ticc serve --host 0.0.0.0 --port 8765
# clients (CLI and MCP):
export TICC_URL=http://tracker.internal:8765 TICC_TOKEN=<same token>
```

Without `TICC_URL`, commands use a local database: `TICC_DB`, or the nearest
`.ticc/ticc.db` found by walking up from the current directory (like git).
SQLite runs in WAL mode, so several local processes can share a file safely.

## HTTP API

All endpoints take/return JSON. Identify with `X-Ticc-Actor` and
`X-Ticc-Actor-Kind` headers; send `Authorization: Bearer $TICC_TOKEN` if a
token is configured.

| method | path                               | body / query                                          |
|--------|------------------------------------|-------------------------------------------------------|
| GET    | `/api/issues`                      | `?status=a,b&kind=&assignee=&label=&q=&all=1`         |
| POST   | `/api/issues`                      | `title, kind, body, priority, labels[], assignee`     |
| POST   | `/api/issues/next`                 | `kind?, label?` — claim top unassigned open issue     |
| GET    | `/api/issues/{id}`                 | issue + `events` + `requests`                         |
| PATCH  | `/api/issues/{id}`                 | any of `title, body, kind, priority, labels, status, note, resolution, assignee` |
| POST   | `/api/issues/{id}/comments`        | `body`                                                |
| POST   | `/api/issues/{id}/progress`        | `message, percent?`                                   |
| POST   | `/api/issues/{id}/claim`           | `force?`                                              |
| POST   | `/api/issues/{id}/release`         | `note?`                                               |
| POST   | `/api/issues/{id}/requests`        | `kind, question, options[], blocking?`                |
| GET    | `/api/requests`                    | `?status=pending|all|…&issue=&requested_by=`          |
| GET    | `/api/requests/{id}`               | `?wait=SECONDS` long-polls until resolved (max 300)   |
| POST   | `/api/requests/{id}/respond`       | `decision` (approve/reject/option), `response`        |
| POST   | `/api/requests/{id}/cancel`        | `reason?`                                             |
| GET    | `/api/events`                      | `?after=ID&limit=` global activity feed               |
| GET    | `/api/stats`                       | counts by status/kind, pending requests               |

Errors are `{"error": "..."}` with 400 (invalid), 401 (token), 403 (not
allowed, e.g. an agent approving), 404, or 409 (conflict, e.g. already claimed).

## Development

```bash
pip install pytest
python -m pytest
```

Layout: `ticc/store.py` holds all the rules (every interface goes through it),
`server.py` is the HTTP API + UI, `client.py` is the same interface over HTTP,
`cli.py` and `mcp.py` are the front ends, and `web/index.html` is the UI.
