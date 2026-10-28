# prwatch: notes for a Claude session

prwatch runs on its own in the `prwatch-prwatch-1` container; nothing here needs a long-lived session.
Read this when the owner asks about the watcher, or asks you to review a PR by hand. Settings are in
`prwatch.env` next to this file.

## Who is who

| Role | Setting | Meaning |
|---|---|---|
| Me | `PRWATCH_ME` | PRs from these logins are mine; a review from any of them counts as my review. |
| My reviewer | `PRWATCH_REVIEWER` | Requested on my ready PRs once CI cleared, once per head. |
| Watched authors | `PRWATCH_WATCHED_AUTHORS` | Their PRs get a first review without asking. |
| Required check | `PRWATCH_REQUIRED_CHECK` | A check that must have passed before a re-review or an approval. |

The rules are in `prwatch/decide.py` (what to do) and `prwatch/render.py` (what to post), with tests in
`tests/`. README "When a PR is reviewed" and "What gets posted" say the same in prose.

## Doing things by hand

```bash
docker exec prwatch-prwatch-1 python3 -m prwatch.main --review 1380 --test   # full review, printed, not posted
docker exec prwatch-prwatch-1 python3 -m prwatch.main --review 1380          # review it now and post it
docker exec prwatch-prwatch-1 python3 -m prwatch.main --once --dry-run       # what the next pass would do
```

Prefer a `--test` run to reviewing inside your own session: a lean run carries far less context, so the
same review costs several times less. Posting anything yourself is a GitHub write the owner must approve.

## Where to look when something went wrong

| File | Content |
|---|---|
| `state/notifications.log` | What needs a look: posts, stuck jobs, auth breaks, cap comments. |
| `state/prwatch.log` | Every decision, run start and end, with tokens and cost. |
| `state/status.json` | The last tick: state, model calls and spend today, posts waiting, stuck jobs, brain counts. |
| `state/runs/<pr>-<sha7>-<time>/` | One run: `prompt.md` and claude's `result.json`. Kept 14 days. |
| `../reviews/PR-<n>-<sha7>.md` | The review body of each verdict. |
| SQLite `/data/state.db` | `prs`, `jobs` (`review:<pr>:<head>`: model, post, posted, dropped, stuck), `calls` (the ledger). |

A job that is `stuck` is never retried on its own: read its `last_error`, then delete the job row to let it
run again. `STOP` in the parent folder pauses every tick.

## Known traps

- **Auth.** A 401 or 403 pauses every GitHub write (`AUTH BROKEN` in status) until `gh api user` works again.
  The Claude token is `claude.env`, from `scripts/set-token.ps1`; a missing one holds model calls.
- **Line endings.** The repository is edited on Windows; shell scripts are pinned to LF by `.gitattributes`.
- **Public repository.** This code is public: no repository names, logins, vault content or personal paths
  in it. They belong in `prwatch.env`, `project-context.md` and the compose `.env`, all git-ignored.
- **Prompt changes.** Try them with `--review <n> --test` on a recent PR before relying on them; the run's
  folder under `state/runs/` shows what it got and returned.
