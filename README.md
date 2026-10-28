# prwatch

A pull request watcher that reviews with Claude Code, runs as one Docker container, and keeps GitHub
writes in plain code. Built for one GitHub repository and one person: it reviews the PRs you are asked
to review, re-reviews after the author pushes corrections, and drafts fixes (never pushed) for review
feedback on your own PRs.

## What it does
Every `PRWATCH_INTERVAL` seconds (default 180), ONE GraphQL read of all open PRs. Rules live in `decide.py` (pure, unit-tested):

| PR role | No model call | Model call (Opus, high) |
|---|---|---|
| mine (author in `PRWATCH_ME`) | request `PRWATCH_REVIEWER` once per head, when ready and CI cleared | fix draft for a new CHANGES_REQUESTED review: patch in `drafts/`, never pushed |
| asked (review requested from me, pinned, or reviewed by me before) | docs-only push since my last review: skip; posting and retrying a stored verdict; one comment at the change-round cap | review when requested; re-review when files changed since my last review, BASE_SHA = last reviewed head |
| other | nothing | nothing (pin it to review) |

A first review starts as soon as my review is requested, without waiting for CI, so it lands early.
Every other model call (re-reviews, fix drafts) waits until CI has CLEARED on the head: every check
completed and SUCCESS, a SKIPPED check passes only when its reusable job `<name> / ...` passed, and the
check named by `PRWATCH_REQUIRED_CHECK` (when set) has passed. An approval always waits for that at post
time. Caps: `PRWATCH_DAILY_CAP` and `PRWATCH_PER_PR_CAP`,
0 = no cap. A re-review also waits until the head has been unchanged for `PRWATCH_SETTLE_MIN` minutes,
so several fix-up pushes cost one review; a re-request of my review skips the wait. A PR whose title
contains one of `PRWATCH_SKIP_TITLE` is ignored completely. After `PRWATCH_MAX_CHANGE_ROUNDS` change
requests of mine on one PR, new commits stop starting re-reviews: the author gets one comment, and a
re-request of my review still gets one.

## How a review runs
0. Re-review of a rebase: when the PR still adds and removes exactly the same lines, per file, as at the
   head reviewed last (a rebase, a merge from the base branch, a squash), the stored verdict is posted
   again on the new head with a note saying so, and no model runs. Approval rules are applied again.
1. Code prepares a worktree at the head and an inputs folder: `input.diff`, `pr.md`, `project-context.md`
   (copied from this folder: what a reviewer must know about the repository), and on a re-review
   `previous.md` (the local review file, else my last review on that head from GitHub) and
   `conversation.md` (what people wrote on the PR since then, bots left out). With a project brain mounted
   (`PRWATCH_BRAIN_DIR`), `brain.md` lists up to `PRWATCH_BRAIN_MAX_LEAVES` vault leaves naming a changed file,
   its directory or its package (gotchas, decisions, rejected, reference; investigations on an exact file),
   copied under `brain/`; the run may read the rest of the vault. `input.diff` is
   last-reviewed-head..HEAD for plain new commits, and the whole PR from its merge-base when the base moved.
2. claude runs `--restricted --tools Read,Grep,Glob,Agent` with the same `--allowedTools`
   (`dontAsk` denies anything not pre-approved), and an environment of only the Claude token, HOME and
   PATH: the model has no shell, no network and no GitHub token. It runs lean: no settings, hooks, MCP
   servers, skills or CLAUDE.md from the PR's checkout (`--setting-sources user`, `claudeMdExcludes`,
   `--strict-mcp-config`, `--disable-slash-commands`); `project-context.md` replaces the repository's
   CLAUDE.md. `PRWATCH_MAX_RUN_USD` stops a run at that spend. It follows the superpowers
   `requesting-code-review` skill: writes three to eight questions the review must settle, dispatches one
   reviewer (Opus; `PRWATCH_REVIEWER_MODEL_SMALL` for a diff of at most `PRWATCH_SMALL_PR_MAX_LINES`
   changed lines touching no `PRWATCH_RISKY_PATHS`) with working rules (about 25 tool calls, stop at 40, verify each finding by quoting the
   line, list what was "Not judged"), then confirms the critical and important findings itself. It returns
   a JSON verdict (`--json-schema`, read from `structured_output`), validated in `render.py`. claude's raw
   result is kept in `runs/` for 14 days.
3. The verdict is stored as job `review:<pr>:<head>` (state `post`). A failed model run retries after
   10 then 30 min, then the job is `stuck`.
4. Code posts it (`post.py`): re-read the PR (head moved or closed: drop; merged at head: COMMENT), skip if
   a review of mine is already on that head, decide the event in code from the counted findings, whatever
   the model's verdict word says: any critical finding is REQUEST_CHANGES (`PRWATCH_ALLOW_REQUEST_CHANGES=1`,
   not on my PR or a draft, else COMMENT); important findings are COMMENT; none is APPROVE when the PR is
   not a draft, has no hold label, touches no infra path, is not mine, `PRWATCH_ALLOW_APPROVE=1`, and its
   CI has cleared (still running: the post waits and looks again every minute; failed: COMMENT). Render the body (no em/en dashes, footer, hidden
   `<!-- prwatch review:<pr>:<head> -->` marker), POST with `commit_id`, then GET it back.
   Network/5xx: retry at 2, 10, 30, 60 min, then `stuck`. 422: `stuck`. 401/403: every write pauses
   (`AUTH BROKEN`) until `gh api user` works again.

Hold labels and infra paths (never approved automatically) are `HOLD_LABELS` and `INFRA_PATHS` in
`render.py`; change them to suit your repository.

## Setup
1. `cp prwatch.env.example prwatch.env` and fill it in (repository, base branch, your logins, reviewer,
   poll interval, settle time, caps, approvals, change requests, reviewer models), and `cp project-context.example.md project-context.md` and write it.
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
- Brain refresh (`PRWATCH_BRAIN_REFRESH=1`, mount rw): when master moves, each reference leaf whose named repo
  files changed is due; after `PRWATCH_BRAIN_SETTLE_MIN` quiet minutes one `PRWATCH_BRAIN_MODEL` call per package
  group (up to 6 leaves) returns edits; code applies them (exact match once, frontmatter kept, no shrink past 40%,
  untouched since read), backs the old leaf up to `brain-backups/`, and logs to `handoff/brain-refresh-log.md`.
  Only in ticks without a review call. Backfill: `docker exec prwatch-prwatch-1 python3 main.py --brain-backfill
  --dry-run`, then without `--dry-run` and with `--budget-usd N` (default 5) and optionally `--only a,b`. Undo:
  `--brain-undo reference/<slug> [--date YYYY-MM-DD]` (the leaf then stays out of automatic refreshes).
- Ledger: `docker exec prwatch-prwatch-1 python3 -c "import sqlite3;print(sqlite3.connect('/data/state.db').execute('select * from calls').fetchall())"`
- Turn approvals or change requests off: `PRWATCH_ALLOW_APPROVE=0` or `PRWATCH_ALLOW_REQUEST_CHANGES=0` in
  prwatch.env, re-run the start command.
- Ideas taken from [errmakov-fuse/agent-codereview](https://github.com/errmakov-fuse/agent-codereview): the
  lean run, project context, settle time, rebase re-approval by patch comparison, the reviewer's working
  rules and questions, the conversation on re-reviews, the per-run spend limit, skip keywords, run records,
  change requests on critical findings, a smaller reviewer model for small changes, the change-round cap.
- Tests: `docker run --rm -e INKHUB_DIR=/tmp/ink --entrypoint sh prwatch:local -c "mkdir -p /tmp/ink/prwatch && python3 -m unittest test_decide test_post"`
