# Working with ticc (instructions for AI agents)

This project tracks work in **ticc**. Use it to pick up work, report what you
are doing, file what you find, and ask humans when you need them. Use the MCP
tools if they are available (`claim_issue`, `report_progress`, …); otherwise
use the `ticc` CLI with `--json`. Set your identity once:

```bash
export TICC_ACTOR=<your-agent-name> TICC_ACTOR_KIND=agent
```

## 1. Pick up work

- Working on a specific issue? `ticc claim <id>` first. If it fails because
  someone else holds it, do not `--force` it; pick something else or ask.
- Looking for work? `ticc --json next [--kind bug]` claims the
  highest-priority unassigned open issue (exit code 2 means nothing to do).
- Read the whole issue first: `ticc show <id>`. Humans often leave guidance
  in comments.

## 2. Report progress

Post a short, factual update at each meaningful milestone, not on every step:

```bash
ticc progress <id> "Reproduced. Root cause: X. Next: fix + regression test." -P 40
```

Good updates say what changed, what you learned, and what's next. Include
links (PRs, commits, logs) when you have them.

## 3. File what you find

Bugs, follow-ups or ideas outside your current scope go in new issues — do
not silently fix unrelated things or forget them:

```bash
ticc new bug "Title that states the problem" -b "Steps / expected / actual / where" -l area
ticc new feature "..."   # or: task
```

## 4. Ask a human when you need one

Ask **before** acting, not after, when:

- you are stuck after a reasonable attempt (`help`),
- the action is risky, destructive, irreversible, expensive, security-relevant,
  touches production or shared data, or changes a public API (`approval`),
- there are several reasonable approaches and the choice is a product or
  team call (`decision`),
- your instructions say something needs sign-off.

```bash
ticc ask <id> "What I need, why, what I tried, impact of each answer" --approval --wait 3600
ticc ask <id> "Which approach?" --decision -o "A: ..." -o "B: ..." --wait 3600
ticc ask <id> "I'm stuck on X; tried Y and Z" --wait 3600     # help
```

Write the question so a busy human can answer it without opening anything
else. Exit codes from `--wait` / `ticc wait`: `0` approved/answered,
`3` rejected, `4` cancelled, `5` timed out (still pending; wait again with
`ticc wait <request-id>`, or work on something else and come back).

Then **respect the answer**:

- `approved` → go ahead, within the scope you described.
- `rejected` → don't do it. Read the human's note and adjust or ask again.
- `answered` → follow the guidance / chosen option.
- If you resolve it yourself before anyone answers, `ticc cancel <request-id> -m "why"`.

While a blocking request is pending the issue is `blocked`, and the tracker
will refuse your status changes. Never try to work around a pending approval.
You cannot answer approvals or decisions; only humans can.

## 5. Finish

```bash
ticc status <id> in_review -m "Summary of what changed, how it was tested, links"
ticc done <id> -r fixed -m "Summary"          # when no human review is needed
ticc release <id> -m "Where I left off and why"   # if you must stop early
```

Leave the issue in a state the next person (human or agent) can pick up from
its timeline alone.
