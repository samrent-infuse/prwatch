"""PR watcher loop: one GraphQL read per tick, rules in decide.py, model calls behind CI and caps.

  python3 -m prwatch.main          # loop every PRWATCH_INTERVAL seconds (default 180)
  python3 -m prwatch.main --once   # one tick
  PRWATCH_DRY_RUN=1 ...     # decide and log, change nothing, call no model

A review is a job keyed review:<pr>:<head>. The model runs once and its verdict is stored; posting
is retried from the stored verdict (2, 10, 30, 60 min) and confirmed by reading the review back.
A 401/403 from GitHub pauses every write until `gh api user` works again.
Kill switch: STOP in the parent folder (mounted at /inkhub/STOP) skips every tick while it exists.
Pins: prwatch/pins.txt, one PR number per line, treats those PRs as review-requested.
Status: prwatch/status.json every tick, for any external status line.
"""

import argparse
import concurrent.futures
import datetime
import json
import os
import sys
import time
import traceback

from prwatch import act
from prwatch import brain
from prwatch import brain_run
from prwatch import decide as d
from prwatch import fetch
from prwatch import post
from prwatch import render
from prwatch import review
from prwatch.store import Store, due, later, stamp

DAILY_CAP = int(os.environ.get("PRWATCH_DAILY_CAP", "6"))
PER_PR_CAP = int(os.environ.get("PRWATCH_PER_PR_CAP", "2"))
INTERVAL = int(os.environ.get("PRWATCH_INTERVAL", "180"))
DRY = os.environ.get("PRWATCH_DRY_RUN") == "1"
ALLOW_APPROVE = os.environ.get("PRWATCH_ALLOW_APPROVE") == "1"
ALLOW_REQUEST_CHANGES = os.environ.get("PRWATCH_ALLOW_REQUEST_CHANGES") == "1"
# The brain refresh: off unless 1 (grounding works either way); a due leaf waits this long after the
# last master move that touched its files.
BRAIN_REFRESH = os.environ.get("PRWATCH_BRAIN_REFRESH") == "1"
BRAIN_SETTLE_MIN = int(os.environ.get("PRWATCH_BRAIN_SETTLE_MIN", "60") or 60)
# Model runs at once (reviews and fix drafts), each on a worker thread so polling goes on meanwhile.
MAX_PARALLEL = max(1, int(os.environ.get("PRWATCH_MAX_PARALLEL", "1") or 1))
POOL = None     # ThreadPoolExecutor, made in main()
RUNNING = {}    # job key -> (future, action); touched by the main thread only
WAIT_RECHECK_MIN = 1  # an approval waiting for CI looks again after this long
MODEL_RETRY_MIN = (10, 30)  # after the 3rd failed model run the job is stuck
DB = os.environ.get("PRWATCH_DB", "/data/state.db")
LOG = act.OUT / "prwatch.log"
NOTIFY = act.OUT / "notifications.log"
BEAT = act.OUT / "heartbeat.txt"
STATUS = act.OUT / "status.json"
PINS = act.ROOT / "pins.txt"


def log(msg, notify=False):
    line = f"{stamp()} {'DRY ' if DRY else ''}{msg}"
    print(line, flush=True)
    for f in [LOG] + ([NOTIFY] if notify else []):
        with f.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")


def usage_note(u):
    if not u or u.get("cost_usd") is None:
        return ""
    return (f"; {u['in_tokens'] + u['cache_read'] + u['cache_write']:,} in / {u['out_tokens']:,} out tokens, "
            f"${u['cost_usd']:.2f} list" + (f", reviewer {u['reviewer']}" if u.get("reviewer") else ""))


def pins():
    try:
        return frozenset(int(x) for x in PINS.read_text().split() if x.isdigit())
    except OSError:
        return frozenset()


def atomic_write(path, text):
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


# ------------------------------------------------------------------ reviews

CARRIED = "Same patch as the reviewed "


def carried_verdict(store, a):
    """The stored verdict of the last reviewed head when the PR still adds and removes exactly the same
    lines (a rebase, a merge from the base branch, a squash), else None. Costs git only, no model call."""
    prev = store.job(f"review:{a.pr}:{a.base}") if a.base else None
    if not prev or not prev["verdict_json"]:
        return None
    try:
        if not review.same_patch(a.pr, a.base, a.head):
            return None
    except Exception as exc:  # any doubt: run the model
        log(f"#{a.pr} patch compare {a.base[:7]}..{a.head[:7]} failed: {exc}")
        return None
    v = json.loads(prev["verdict_json"])
    summary = v["summary"]
    if summary.startswith(CARRIED):  # carried before: keep one note, not a chain
        summary = summary.split("\n\n", 1)[-1]
    v["summary"] = (f"{CARRIED}{a.base[:7]}: the PR adds and removes exactly the same lines; only the base or "
                    f"the history changed. No new review was run, and the earlier findings stand.\n\n{summary}")
    return v


def keep_verdict(store, a, key, v, tries):
    v = {**v, "kind": a.kind}  # the body is headed "re-review" for a rereview
    act.REVIEWS.mkdir(parents=True, exist_ok=True)
    (act.REVIEWS / f"PR-{a.pr}-{a.head[:7]}.md").write_text(render.body(v, a.head, key), encoding="utf-8")
    store.upsert_job(key, pr=a.pr, head=a.head, kind=a.kind, base_prev=a.base, state="post",
                     model_tries=tries, verdict_json=json.dumps(v), next_try_at="", last_error="")


def run_review(store, a, reviewer=None):
    """Model run for one review job; stores the verdict as state 'post'."""
    key = f"review:{a.pr}:{a.head}"
    job = store.job(key)
    tries = job["model_tries"] if job else 0
    carried = carried_verdict(store, a) if a.kind == "rereview" else None
    if carried:
        keep_verdict(store, a, key, carried, tries)
        log(f"#{a.pr} {a.kind} at {a.head[:7]}: same patch as {a.base[:7]}, verdict carried over, "
            "no model call; posting")
        return
    prev = act.REVIEWS / f"PR-{a.pr}-{a.base[:7]}.md" if a.base else None
    prev_body = prev.read_text(encoding="utf-8") if prev and prev.exists() else ""
    if a.base and not prev_body:
        try:
            prev_body = fetch.previous_review(a.pr, a.base)
        except post.AuthBroken:
            raise
        except Exception as exc:  # the review still runs, only without previous.md
            log(f"#{a.pr} previous review at {a.base[:7]} not readable: {exc}")
    cid = store.start_call(a.pr, a.kind, review.MODEL, a.head, a.reason)
    try:
        v, err, base, usage = review.run(a.pr, a.head, a.base, prev_body, reviewer=reviewer)
    except Exception as exc:  # git or gh failure while preparing
        v, err, base, usage = None, f"prepare failed: {exc}", "", None
    store.end_call(cid, "verdict " + v["verdict"] if v else err, usage)
    tries += 1
    if v:
        keep_verdict(store, a, key, v, tries)
        c = render.counts(v)
        log(f"#{a.pr} {a.kind} at {a.head[:7]} (base {base[:7]}): {v['verdict']}, "
            f"{c['critical']}/{c['important']}/{c['minor']} critical/important/minor{usage_note(usage)}; posting")
        return
    stuck = tries > len(MODEL_RETRY_MIN)
    store.upsert_job(key, pr=a.pr, head=a.head, kind=a.kind, base_prev=a.base,
                     state="stuck" if stuck else "model", model_tries=tries, last_error=err[:300],
                     next_try_at="" if stuck else later(MODEL_RETRY_MIN[tries - 1]))
    log(f"#{a.pr} {a.kind} at {a.head[:7]} failed ({tries}): {err}" + (" -> STUCK" if stuck else ""), notify=True)


def drain_posts(store):
    for job in store.jobs_in("post"):
        if not due(job["next_try_at"]):
            continue
        outcome, detail, rid = post.post(job["pr"], job["head"], job["key"], json.loads(job["verdict_json"]),
                                         ALLOW_APPROVE, ALLOW_REQUEST_CHANGES)
        if outcome == "wait":  # not a failure: no retry counted, logged when the reason changes
            store.upsert_job(job["key"], last_error=detail, next_try_at=later(WAIT_RECHECK_MIN))
            if detail != job["last_error"]:
                log(f"#{job['pr']} post at {job['head'][:7]}: {detail}")
            continue
        tries = job["post_tries"] + (outcome == "retry")
        if outcome == "retry" and tries > len(post.POST_BACKOFF_MIN):
            outcome, detail = "stuck", f"out of post retries: {detail}"
        fields = {"state": "post" if outcome == "retry" else outcome, "post_tries": tries,
                  "last_error": detail if outcome in ("retry", "stuck") else "", "review_id": rid,
                  "next_try_at": later(post.POST_BACKOFF_MIN[tries - 1]) if outcome == "retry" else ""}
        store.upsert_job(job["key"], **fields)
        if outcome == "posted":
            row, note = store.row(job["pr"])
            row.last_reviewed_head = job["head"]
            store.save(job["pr"], row, note)
        log(f"#{job['pr']} post at {job['head'][:7]}: {outcome}, {detail}",
            notify=outcome in ("posted", "stuck"))


# -------------------------------------------------------------------- brain

def brain_step(store, model_call_made):
    """Follow master for the brain: scan when it moved, then (writer on, no review call this tick, Claude
    token ready) refresh one group of settled due leaves."""
    if not review.BRAIN_DIR or not review.BRAIN_DIR.is_dir():
        return
    head, prev = fetch.master_head(), store.get_meta("brain_master")
    if not head:
        return
    index = brain.build_index(review.BRAIN_DIR, review.WORK / "brain_index.json")
    if head != prev:
        review.git("fetch", "--quiet", "origin", f"+{review.BASE_BRANCH}:refs/remotes/origin/{review.BASE_BRANCH}")
        due = brain_run.scan(store, index, head, prev)
        store.set_meta("brain_master", head)
        if due:
            log(f"brain: master at {head[:7]}; due: {', '.join(due)}")
    if not BRAIN_REFRESH or model_call_made or not act.claude_ready():
        return
    group = brain.pick_group(store.brain_rows(), index, datetime.datetime.now(datetime.timezone.utc),
                             BRAIN_SETTLE_MIN)
    if group:
        outcome = brain_run.refresh(store, review.BRAIN_DIR, group, head)
        log(f"brain refresh of {', '.join(getattr(l, 'slug', str(l)) for l in group)} at {head[:7]}: {outcome}",
            notify=True)


def brain_status(store):
    if not review.BRAIN_DIR or not review.BRAIN_DIR.is_dir():
        return None
    rows = store.brain_rows()
    return {**brain.log_counts(review.BRAIN_DIR, stamp()[:10]), "refresh_on": BRAIN_REFRESH,
            "due": sum(r["state"] == "due" for r in rows), "stuck": [r["slug"] for r in rows if r["state"] == "stuck"]}


# ------------------------------------------------------------------ workers

def run_fix_draft(store, a):
    """Model run drafting fixes for one CHANGES_REQUESTED review on my PR. True when a draft was made."""
    cid = store.start_call(a.pr, a.kind, review.MODEL, a.head, a.reason)
    try:
        outcome, usage = act.fix_draft(a.pr, a.head, a.review_id)
    except post.AuthBroken:
        raise
    except Exception as exc:
        outcome, usage = f"fix draft failed: {exc}", None
    store.end_call(cid, outcome, usage)
    log(f"#{a.pr} fix draft at {a.head[:7]}: {outcome}{usage_note(usage)}", notify=True)
    return not outcome.startswith("fix draft failed")


def job_key(a):
    return f"fix:{a.pr}:{a.review_id}" if a.kind == "fix_draft" else f"review:{a.pr}:{a.head}"


def _work(a):
    """One model job on a worker thread, with its own database connection. Its result (fix drafts only)
    is collected by harvest() on the main thread, which alone writes PR rows."""
    store = Store(DB)
    try:
        if a.kind == "fix_draft":
            return run_fix_draft(store, a)
        run_review(store, a)
    except post.AuthBroken as exc:
        store.set_meta("auth_broken", f"worker: {exc}")
        log(f"GitHub auth broken in #{a.pr} {a.kind}: {exc}; every write paused", notify=True)
    except Exception as exc:
        log(f"#{a.pr} {a.kind} crashed: {exc}", notify=True)
    finally:
        store.db.close()
    return None


def dispatch(a):
    """Start a model job on a worker unless it runs already or every worker is busy. Returns a note."""
    key = job_key(a)
    if key in RUNNING:
        return f"{a.kind} running"
    if len(RUNNING) >= MAX_PARALLEL:
        return f"{a.kind} waits: {MAX_PARALLEL} model runs busy"
    RUNNING[key] = (POOL.submit(_work, a), a)
    log(f"#{a.pr} {a.kind} started at {a.head[:7]} ({a.reason or a.base[:7]}); {len(RUNNING)} running")
    return ""


def harvest(store):
    """Collect finished jobs: free their slot; a drafted fix records its review id on the PR row."""
    for key, (fut, a) in list(RUNNING.items()):
        if not fut.done():
            continue
        del RUNNING[key]
        if a.kind == "fix_draft" and fut.result() is True:
            row, note = store.row(a.pr)
            row.last_fix_review_id = a.review_id
            store.save(a.pr, row, note)


# --------------------------------------------------------------------- tick

def handle_pr(store, snap, pinned, me):
    row, last_note = store.row(snap.number)
    if row.head_seen != snap.head:
        row.head_seen, row.head_seen_at = snap.head, stamp()
    seen = datetime.datetime.strptime(row.head_seen_at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=datetime.timezone.utc)
    head_age_min = (datetime.datetime.now(datetime.timezone.utc) - seen).total_seconds() / 60
    role = d.role_of(snap, pinned)
    last = row.last_reviewed_head or d._my_last_reviewed_head(snap, row)
    files = None
    if role == "asked" and last and last != snap.head and snap.head != row.skipped_head \
            and not snap.draft and d.ci_cleared(snap.checks)[0]:
        files = fetch.files_between(last, snap.head)
    role, actions, note = d.decide(snap, row, pinned, files, head_age_min)
    row.role = role
    if role == "asked" and not row.last_reviewed_head and last:
        row.last_reviewed_head = last
    for a in actions:
        if a.kind in ("review", "rereview"):
            job = store.job(f"review:{a.pr}:{a.head}")
            if job and (job["state"] != "model" or not due(job["next_try_at"])):
                continue  # verdict stored (drain_posts posts it), finished, or backing off
        ok, why = d.within_caps(a, store.calls_today(), store.calls_today(a.pr), DAILY_CAP, PER_PR_CAP)
        if not ok:
            note = f"{a.kind} held: {why}"
            continue
        if a.kind in d.MODEL_KINDS and not act.claude_ready():
            note = f"{a.kind} held: {act.claude_auth.state()[1]}"
            continue
        if DRY:
            log(f"#{snap.number} would {a.kind} at {a.head[:7]} ({a.reason or a.base[:7]})")
            continue
        if a.kind == "skip":
            row.skipped_head = a.head
        elif a.kind == "cap_comment":
            act.cap_comment(a.pr, int(a.reason.split()[0]))
            row.cap_commented = 1
            log(f"#{a.pr} change-round cap: commented, automatic re-reviews stop ({a.reason})", notify=True)
        elif a.kind == "request_reviewer":
            act.request_reviewer(a.pr)
            row.last_rerequest_head = a.head
            log(f"#{a.pr} requested {d.REVIEWER} at {a.head[:7]}", notify=True)
        elif a.kind in d.MODEL_KINDS:
            note = dispatch(a) or note
    if note and note != last_note:
        log(f"#{snap.number} ({role}) {note}", notify=not note.startswith(("waiting", "draft")))
    if not DRY:
        store.save(snap.number, row, note)


def tick(store, me):
    if act.STOP.exists():
        return "PAUSED (STOP file)"
    broken = store.get_meta("auth_broken")
    if broken:
        if fetch.gh("api", "user", "--jq", ".login", check=False).strip() != me:
            return f"AUTH BROKEN {broken}"
        store.set_meta("auth_broken", "")
        log(f"GitHub auth works again as {me}; resuming", notify=True)
    harvest(store)
    try:
        pinned = pins()
        calls_before = store.calls_today()
        for snap in fetch.open_prs():
            try:
                handle_pr(store, snap, pinned, me)
            except post.AuthBroken:
                raise
            except Exception as exc:
                log(f"#{snap.number} error: {exc}", notify=True)
        if not DRY:
            drain_posts(store)
            try:
                brain_step(store, model_call_made=bool(RUNNING) or store.calls_today() > calls_before)
            except Exception as exc:  # the brain never stops the review loop
                log(f"brain step failed: {exc}", notify=True)
    except post.AuthBroken as exc:
        store.set_meta("auth_broken", f"{me}: {exc}")
        log(f"GitHub auth broken as {me}: {exc}; every write paused", notify=True)
        return f"AUTH BROKEN {me}"
    return "OK"


def write_status(store, me, state):
    status = {
        "tick": stamp(), "state": state, "login": me, "interval_s": INTERVAL, "dry_run": DRY,
        "approve_enabled": ALLOW_APPROVE, "request_changes_enabled": ALLOW_REQUEST_CHANGES, "claude_token": act.claude_ready(), "claude_auth": act.claude_auth.state()[1],
        "model_calls_today": store.calls_today(), "daily_cap": DAILY_CAP, "usage_today": store.usage_today(), "brain": brain_status(store),
        "posts_waiting": len(store.jobs_in("post")),
        "stuck": [j["key"] for j in store.jobs_in("stuck")],
        "recent": [{"key": j["key"], "state": j["state"], "at": j["updated_at"], "error": j["last_error"]}
                   for j in store.recent_posted()],
    }
    atomic_write(STATUS, json.dumps(status, indent=1) + "\n")


def hand_review(store, n, test=False, reviewer_model=None):
    """Review PR n now, whatever the rules say. test=True runs everything but posts nothing and prints the
    review; otherwise the verdict is stored and posted like any other."""
    head = json.loads(fetch.gh("pr", "view", str(n), "--repo", fetch.REPO, "--json", "headRefOid"))["headRefOid"]
    row, _ = store.row(n)
    base = row.last_reviewed_head if row.last_reviewed_head and row.last_reviewed_head != head else ""
    if test:
        prev = fetch.previous_review(n, base) if base else ""
        v, err, _, usage = review.run(n, head, base, prev, reviewer=reviewer_model)
        print(render.body(v, head, f"review:{n}:{head}") if v else f"no verdict: {err}")
        print(f"(test run, nothing posted{usage_note(usage)})")
        return 0 if v else 1
    run_review(store, d.Action(kind="rereview" if base else "review", pr=n, head=head, base=base, reason="by hand"),
               reviewer=reviewer_model)
    drain_posts(store)
    return 0


def brain_undo(store, folder_slug, date):
    """Put a leaf's backup back and stop automatic refreshes of it (state stuck) until a person looks."""
    used = brain.undo(review.BRAIN_DIR, review.STATE / "brain-backups", folder_slug, date)
    slug = folder_slug.split("/", 1)[1]
    store.brain_upsert(slug, path=f"{folder_slug}.md", state="stuck", last_error=f"undone by hand from {used}")
    brain.log(review.BRAIN_DIR, f"{stamp()} [[{slug}]] undone by hand: restored {used}; automatic refresh off for it")
    return used


def brain_command(store, args):
    """--brain-backfill and --brain-undo, run with docker exec beside the loop."""
    if not review.BRAIN_DIR or not review.BRAIN_DIR.is_dir():
        print(f"no brain at {review.BRAIN_DIR}")
        return 2
    if args.brain_undo:
        print(f"restored {brain_undo(store, args.brain_undo, args.date)}")
        return 0
    review.git("fetch", "--quiet", "origin", f"+{review.BASE_BRANCH}:refs/remotes/origin/{review.BASE_BRANCH}")
    head = review.git("rev-parse", f"origin/{review.BASE_BRANCH}").strip()
    if not args.dry_run and not act.claude_ready():
        print(f"no Claude token: {act.claude_auth.state()[1]}")
        return 2
    brain_run.backfill(store, review.BRAIN_DIR, head, args.dry_run, args.budget_usd,
                       [s for s in (args.only or "").split(",") if s])
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--brain-backfill", action="store_true", help="bring reference leaves up to master")
    ap.add_argument("--dry-run", action="store_true",
                    help="decide and log what a pass would do, change nothing; with --brain-backfill: list only")
    ap.add_argument("--review", type=int, metavar="PR", help="review this PR now, whatever the rules say")
    ap.add_argument("--test", action="store_true", help="with --review: run it, print the review, post nothing")
    ap.add_argument("--reviewer-model", help="with --review: the reviewer subagent's model (e.g. sonnet)")
    ap.add_argument("--budget-usd", type=float, default=5.0, help="with --brain-backfill: stop at this spend")
    ap.add_argument("--only", help="with --brain-backfill: comma-separated leaf slugs")
    ap.add_argument("--brain-undo", metavar="FOLDER/SLUG", help="restore a refreshed leaf's backup")
    ap.add_argument("--date", help="with --brain-undo: the backup day, YYYY-MM-DD (default newest)")
    args = ap.parse_args()
    act.OUT.mkdir(parents=True, exist_ok=True)  # state/
    store = Store(DB)
    brain.log_event = lambda msg: log(f"brain: {msg}")
    if args.brain_backfill or args.brain_undo:
        return brain_command(store, args)
    if args.review:
        return hand_review(store, args.review, args.test, args.reviewer_model)
    global DRY, POOL
    DRY = DRY or args.dry_run
    POOL = concurrent.futures.ThreadPoolExecutor(MAX_PARALLEL, thread_name_prefix="model")
    me = fetch.gh("api", "user", "--jq", ".login").strip()
    log(f"start as {me}: interval {INTERVAL}s, caps {DAILY_CAP}/day {PER_PR_CAP}/PR/day, "
        f"approvals {'enabled' if ALLOW_APPROVE else 'off (comments only)'}, "
        f"request changes {'enabled' if ALLOW_REQUEST_CHANGES else 'off'}, {MAX_PARALLEL} parallel, "
        f"claude: {act.claude_auth.state()[1]}")
    last_state = ""
    while True:
        try:
            state = tick(store, me)
        except Exception:
            state = "FAILING " + traceback.format_exc().strip().splitlines()[-1][:200]
        if state != last_state and state != "OK":
            log(f"tick: {state}", notify=True)
        last_state = state
        try:
            write_status(store, me, state)
            BEAT.write_text(stamp() + "\n")
        except Exception as exc:
            print(f"{stamp()} status write failed: {exc}", flush=True)
        if args.once:
            POOL.shutdown(wait=True)
            harvest(store)
            return 0
        time.sleep(INTERVAL)


if __name__ == "__main__":
    sys.exit(main())
