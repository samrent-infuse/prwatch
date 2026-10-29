# prwatch

Reviews pull requests with Claude Code from one Docker container. It reviews the PRs you are asked to
review and your watched teammates' PRs, re-reviews them after corrections, requests a reviewer on your own
ready PRs, and drafts fixes (never pushed) for change requests on them. Code makes every decision and every
GitHub write; the model only reads and returns a JSON verdict.

## How it runs

The container polls every `PRWATCH_INTERVAL` seconds with one GraphQL read of the open PRs. A poll that
finds nothing costs no tokens. Each review, re-review or fix draft runs on a worker thread, up to
`PRWATCH_MAX_PARALLEL` at once, so polling goes on while they run. A run is a fresh, lean `claude -p`: no
settings, hooks, MCP servers, skills or CLAUDE.md from the PR's checkout, only Read, Grep, Glob and Agent, and
no GitHub token. It follows the superpowers `requesting-code-review` skill, briefs one reviewer subagent with
three to eight questions, checks the critical and important findings itself, and returns the verdict. Code
then decides the event, renders the review, posts it pinned to the reviewed commit and reads it back.

## What is in here

| Path | What it is |
|---|---|
| `prwatch.env.example` | Repository, logins, watched authors, models, limits. Copy to `prwatch.env`; the only settings file. |
| `project-context.example.md` | What a reviewer must know about the repository. Copy to `project-context.md`. |
| `AGENT.md` | Notes for a Claude session asked about the watcher, or to review a PR by hand. |
| `prompts/` | Every prompt the model gets: `review.md` (the run), `reviewer.md` (the reviewer subagent's brief), `rereview.md`, `review-brain.md`, `review-base-moved.md`, `fix-draft.md`, `brain-refresh.md`. |
| `prwatch/main.py` | The loop, the workers and the command line. |
| `prwatch/decide.py` | The rules: which PRs need a review, a re-review, a reviewer request, a fix draft. Pure, tested. |
| `prwatch/review.py` | Prepares a run (worktree, diff, PR text, earlier review, conversation, brain leaves) and runs claude. |
| `prwatch/render.py`, `prwatch/post.py` | The verdict's checks and layout; posting, read-back and retries. |
| `prwatch/fetch.py`, `prwatch/act.py` | GitHub reads; reviewer requests, cap comments and fix drafts. |
| `prwatch/store.py` | SQLite state: PRs, jobs, the call ledger with tokens and cost. |
| `prwatch/brain.py`, `prwatch/brain_run.py` | Optional project brain: grounding reviews in a vault, proposing refreshes of its leaves. |
| `tests/` | Unit tests for all of the above. |
| `scripts/set-token.ps1` | Saves a long-lived Claude token into `claude.env` and restarts the container. |
| `compose.yaml`, `Dockerfile`, `entrypoint.sh` | The container. |
| `state/` | Everything prwatch writes: `prwatch.log`, `notifications.log`, `status.json`, `heartbeat.txt`, `runs/`, `drafts/`, `brain-backups/`. Git-ignored. |

## Setup

1. `cp prwatch.env.example prwatch.env` and `cp project-context.example.md project-context.md`; fill both in.
2. Claude token: `claude setup-token`, then from this folder
   `powershell -ExecutionPolicy Bypass -File scripts\set-token.ps1` (reads it hidden, tests it, restarts).
3. Start: `GH_TOKEN=$(gh auth token) docker compose up -d --build` (`gh auth token -u <login>` with several accounts).

## Try it by hand

```bash
docker exec prwatch-prwatch-1 python3 -m prwatch.main --review 1371 --test                       # full review, not posted
docker exec prwatch-prwatch-1 python3 -m prwatch.main --review 1371 --test --reviewer-model sonnet
docker exec prwatch-prwatch-1 python3 -m prwatch.main --review 1371                              # review and post it
docker exec prwatch-prwatch-1 python3 -m prwatch.main --once --dry-run                           # what a pass would do
docker run --rm -e INKHUB_DIR=/tmp/ink --entrypoint sh prwatch:local -c "mkdir -p /tmp/ink/prwatch/state && python3 -m unittest discover -s tests -t ."
```

## When a PR is reviewed

- **First review:** your review is requested, the PR is pinned (`pins.txt`), or its author is in
  `PRWATCH_WATCHED_AUTHORS`. It does not wait for CI.
- **Re-review:** the head moved past the commit of your last review and files other than docs changed. It
  waits until CI has cleared and the head has been quiet for `PRWATCH_SETTLE_MIN` minutes; a re-request of
  your review skips the quiet wait. A rebase whose patch is line for line the same posts the earlier verdict
  again with no model run; a near-identical one gets `compare.txt` so the run can keep the verdict cheaply. A
  small change is checked by the run itself, without a reviewer subagent.
- **Drafts:** reviewed when your review is requested on them, or every watched draft with
  `PRWATCH_REVIEW_DRAFTS=1`. Never approved, never changes-requested. Your own drafts get nothing.
- **Ignored:** a title containing one of `PRWATCH_SKIP_TITLE`; authors you do not watch, unless they ask.
- **Capped:** after `PRWATCH_MAX_CHANGE_ROUNDS` change requests on one PR, new commits stop starting
  re-reviews; the author gets one comment, and a re-request still works.

## What gets posted

Code counts the findings, whatever the model's verdict word says:

| Findings | Posted as |
|---|---|
| any critical | REQUEST_CHANGES (`PRWATCH_ALLOW_REQUEST_CHANGES=1`), else COMMENT; COMMENT on your PRs and drafts |
| important or none, no critical | APPROVE (with important findings: "approve, with fixes", listing them to settle before merging) when the PR is ready, has no hold label, touches no infra path, is not yours, `PRWATCH_ALLOW_APPROVE=1` and its CI has cleared (still running: the post waits; failed: COMMENT) |
| PR merged at that head | COMMENT |
| head moved during the run | COMMENT on the reviewed commit, saying the PR moved on |

A review reads "Automated code review" or "Automated re-review", the commit it covers, one verdict line,
then Strengths, the earlier findings' status (re-reviews), Critical, Important, Minor and Not judged, and
ends with "Automated review (Claude Opus), reviewed at <sha7>." Hold labels and infra paths are
`HOLD_LABELS` and `INFRA_PATHS` in `prwatch/render.py`.

## Cost controls

| Setting | Effect |
|---|---|
| `PRWATCH_REVIEWER_MODEL_SMALL`, `PRWATCH_SMALL_PR_MAX_LINES`, `PRWATCH_RISKY_PATHS` | The reviewer subagent runs on the smaller model for a small change touching no risky path. |
| `PRWATCH_MAX_RUN_USD` | A run stops at this spend (claude's own estimate). |
| `PRWATCH_DAILY_CAP`, `PRWATCH_PER_PR_CAP` | Model calls per day and per PR per day; 0 = no cap. |
| `PRWATCH_MAX_PARALLEL` | Runs at once. |
| `PRWATCH_ORCHESTRATOR_EFFORT`, `PRWATCH_FIX_MODEL` | Effort of the review run itself (the reviewer stays at high); model of fix drafts. |
| `PRWATCH_SETTLE_MIN` | Quiet time before a re-review, so a burst of fix-up pushes costs one review. |
| `PRWATCH_MAX_CHANGE_ROUNDS` | Change requests before automatic re-reviews stop. |

The reviewer's brief (`prompts/reviewer.md`, adapted from the superpowers code-reviewer template, MIT) is filled
in by code, so a run never reads skill files. Every call's tokens, list-price cost and reviewer model are in the
`calls` table, today's totals in `state/status.json`. `--measure-context` prints the fixed context a lean run
starts from (one Haiku call, about a cent); check it after a Claude Code update.

## Operate

- **Stop:** create `STOP` in the parent folder (ticks skip) or `docker compose stop`.
- **Where to look:** `state/notifications.log` (things to look at), `state/prwatch.log` (everything),
  `state/runs/<pr>-<sha7>-<time>/` (each run's prompt and result, 14 days), reviews in `../reviews/`.
- **Brain (optional):** with a vault in the `_CONVENTIONS` format mounted at `/brain`, reviews get the leaves
  that name the changed files. With `PRWATCH_BRAIN_REFRESH=1` and the mount read-write, a reference leaf
  whose named files changed is flagged `status: stale` and its proposed edits are logged
  (`PRWATCH_BRAIN_APPLY=apply` writes them instead, with backup and `--brain-undo`). Backfill:
  `--brain-backfill --dry-run`, then with `--budget-usd N`.

## Limits

- At most 60 open PRs are read per poll.
- The container needs Docker Desktop running; nothing runs while the laptop sleeps.
- Ideas taken from [errmakov-fuse/agent-codereview](https://github.com/errmakov-fuse/agent-codereview): the
  lean run, project context, settle time, patch comparison, the reviewer's questions and working rules, the
  conversation on re-reviews, spend limits, skip keywords, run records, change requests, the small-PR model,
  the change-round cap, watched authors, drafts, parallel runs, the review layout and by-hand reviews.
