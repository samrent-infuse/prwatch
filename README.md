# prwatch

A pull request watcher that reviews with Claude Code, runs as one Docker container, and keeps GitHub
writes in plain code. Built for one GitHub repository and one person: it reviews the PRs you are asked
to review, re-reviews after the author pushes corrections, and drafts fixes (never pushed) for review
feedback on your own PRs.

## What it does
Every 180 s, ONE GraphQL read of all open PRs. Rules live in `decide.py` (pure, unit-tested):

| PR role | No model call | Model call (Opus, high) |
|---|---|---|
| mine (author in `PRWATCH_ME`) | request `PRWATCH_REVIEWER` once per head, when ready and CI cleared | fix draft for a new CHANGES_REQUESTED review: patch in `drafts/`, never pushed |
| asked (review requested from me, pinned, or reviewed by me before) | docs-only push since my last review: skip; posting and retrying a stored verdict | review when requested; re-review when files changed since my last review, BASE_SHA = last reviewed head |
| other | nothing | nothing (pin it to review) |

Claude is called only when CI has CLEARED on the head: every check completed and SUCCESS, a SKIPPED
check passes only when its reusable job `<name> / ...` passed, and the check named by
`PRWATCH_REQUIRED_CHECK` (when set) has passed. Caps: `PRWATCH_DAILY_CAP` and `PRWATCH_PER_PR_CAP`,
0 = no cap.

## How a review runs
1. Code prepares a worktree at the head and an inputs folder: `input.diff` (BASE_SHA..HEAD), `pr.md`,
   and `previous.md` on a re-review (the local review file, else my last review on that head from GitHub).
2. claude runs `--restricted --tools Read,Grep,Glob,Agent` with the same `--allowedTools`
   (`dontAsk` denies anything not pre-approved), and an environment of only the Claude token, HOME and
   PATH: the model has no shell, no network and no GitHub token. It follows the superpowers
   `requesting-code-review` skill and returns a JSON verdict (`--json-schema`, read from
   `structured_output`), validated in `render.py`.
3. The verdict is stored as job `review:<pr>:<head>` (state `post`). A failed model run retries after
   10 then 30 min, then the job is `stuck`.
4. Code posts it (`post.py`): re-read the PR (head moved or closed: drop; merged at head: COMMENT), skip if
   a review of mine is already on that head, decide APPROVE or COMMENT in code (zero critical and important
   findings counted by code, whatever the model's verdict word says, AND not draft, no hold label, no infra
   path, not my PR, AND `PRWATCH_ALLOW_APPROVE=1`), render the body (no em/en dashes, footer, hidden
   `<!-- prwatch review:<pr>:<head> -->` marker), POST with `commit_id`, then GET it back.
   Network/5xx: retry at 2, 10, 30, 60 min, then `stuck`. 422: `stuck`. 401/403: every write pauses
   (`AUTH BROKEN`) until `gh api user` works again.

Hold labels and infra paths (never approved automatically) are `HOLD_LABELS` and `INFRA_PATHS` in
`render.py`; change them to suit your repository.

## Setup
1. `cp prwatch.env.example prwatch.env` and fill it in (repository, base branch, your logins, reviewer,
   caps, approvals).
2. The compose file mounts the superpowers skills from the Claude Code plugin cache
   (`~/.claude/plugins/cache/superpowers-marketplace/superpowers/<version>/skills`); adjust the version.
3. Claude token: run `claude setup-token` (about a year), then
   `powershell -ExecutionPolicy Bypass -File set-token.ps1` here. It reads the token hidden, tries it with one
   Haiku call, writes `claude.env` (git-ignored) and restarts the container. Without `claude.env`, prwatch
   falls back to the desktop login (`~/.claude/.credentials.json`, read-only, never refreshed here): about
   8 hours, renewed only by a `claude` run on the host, so reviews hold overnight.
4. Start / rebuild (Git Bash, here): `GH_TOKEN=$(gh auth token) docker compose up -d --build`
   (or `gh auth token -u <login>` when several accounts are signed in).

## Operate
- Stop everything: create `STOP` in the parent folder (ticks skip) or `docker compose stop`.
- Pin a PR: add its number to `pins.txt`.
- Logs: `prwatch.log` (all), `notifications.log` (things to look at), `heartbeat.txt`; reviews in `../reviews/`.
- Status: every tick writes `status.json`: `OK`, `PAUSED (STOP file)`, `AUTH BROKEN`, model calls today,
  tokens and list-price cost today, posts waiting, stuck jobs, Claude token state.
- Spend: every call's tokens (input, output, cache read, cache write) and claude's list-price cost are in the
  `calls` table; today's totals are in `status.json` (`usage_today`) and each review's log line.
- Ledger: `docker exec prwatch-prwatch-1 python3 -c "import sqlite3;print(sqlite3.connect('/data/state.db').execute('select * from calls').fetchall())"`
- Turn approvals off: `PRWATCH_ALLOW_APPROVE=0` in prwatch.env, re-run the start command.
- Tests: `docker run --rm -e INKHUB_DIR=/tmp/ink --entrypoint sh prwatch:local -c "mkdir -p /tmp/ink/prwatch && python3 -m unittest test_decide test_post"`
