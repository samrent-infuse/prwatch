"""Pure rules for the PR watcher: one PR snapshot plus its stored row -> actions. No I/O.

Claude is called only for two reasons, and only once CI has cleared on the head:
  - a review is open: my review is requested (or the PR is pinned) and I have not reviewed this head;
  - files changed since my last review of the PR (an author's correction push).
On my own PRs Claude drafts fixes for a new CHANGES_REQUESTED review (never pushed); requesting
the configured reviewer costs no model call.
"""

import os
import re
from dataclasses import dataclass, field

# Identity and policy come from the environment (prwatch.env, see README).
ME = {x.strip() for x in os.environ.get("PRWATCH_ME", "").split(",") if x.strip()}  # my GitHub logins
REVIEWER = os.environ.get("PRWATCH_REVIEWER", "").strip()  # requested on my ready PRs; empty = none
# A check name prefix that must have passed before any model call (e.g. a scanner verdict); empty = none.
REQUIRED_CHECK = os.environ.get("PRWATCH_REQUIRED_CHECK", "").strip()
# Minutes a re-review waits for the head to stay unchanged, so a run of fix-up pushes costs one review;
# an explicit re-request acts at once. 0 = no wait.
SETTLE_MIN = int(os.environ.get("PRWATCH_SETTLE_MIN", "0") or 0)
# A PR whose title contains one of these (comma separated, any case) is ignored completely.
SKIP_TITLE = [x.strip().lower() for x in os.environ.get("PRWATCH_SKIP_TITLE", "").split(",") if x.strip()]
# After this many change requests from me on one PR, new commits no longer start a re-review: the author
# gets one comment, and a re-request of my review still works. 0 = no limit.
MAX_CHANGE_ROUNDS = int(os.environ.get("PRWATCH_MAX_CHANGE_ROUNDS", "0") or 0)
# A push touching only these gets no model call; the earlier verdict is not carried over.
TRIVIAL_PATH = re.compile(r"(^docs/|\.md$|\.txt$|\.rst$)")
MODEL_KINDS = {"review", "rereview", "fix_draft"}


@dataclass
class Snapshot:
    number: int
    title: str
    author: str
    draft: bool
    head: str
    base: str
    labels: set = field(default_factory=set)
    review_requests: set = field(default_factory=set)
    reviews: list = field(default_factory=list)  # dicts: id, author, state, commit, submitted
    checks: list = field(default_factory=list)   # dicts: name, status, conclusion (upper case)


@dataclass
class Row:
    role: str = ""
    last_reviewed_head: str = ""
    last_rerequest_head: str = ""
    last_fix_review_id: int = 0
    skipped_head: str = ""
    head_seen: str = ""     # the head prwatch saw last, and when it first saw it (settle time)
    head_seen_at: str = ""
    cap_commented: int = 0  # 1 once the change-round cap comment is on the PR


@dataclass
class Action:
    kind: str  # request_reviewer | review | rereview | fix_draft | skip | cap_comment
    pr: int
    head: str
    base: str = ""          # rereview: the head reviewed last time
    review_id: int = 0      # fix_draft: the CHANGES_REQUESTED review being answered
    reason: str = ""


def ci_status(checks):
    """("cleared" | "pending" | "failed", why). Cleared: every check completed and passed; a skip passes
    only when its reusable job (`<name> / ...`) passed on the same head; REQUIRED_CHECK, when set, must
    have passed. A failed check wins over a pending one."""
    if not checks:
        return "pending", "no checks yet"
    passed = {c["name"] for c in checks if c["conclusion"] == "SUCCESS"}
    pending = ""
    for c in checks:
        if c["status"] != "COMPLETED":
            pending = pending or f"{c['name']} pending"
        elif c["conclusion"] == "SUCCESS":
            continue
        elif c["conclusion"] == "SKIPPED" and any(p.startswith(c["name"] + " / ") for p in passed):
            continue
        elif c["conclusion"] == "SKIPPED" and any(o["name"].startswith(c["name"] + " / ") and
                                                  o["status"] != "COMPLETED" for o in checks):
            pending = pending or f"{c['name']} / ... pending"  # its reusable job is still running
        else:
            return "failed", f"{c['name']} {c['conclusion'].lower()}"
    if pending:
        return "pending", pending
    if REQUIRED_CHECK and not any(n.startswith(REQUIRED_CHECK) for n in passed):
        return "pending", f"{REQUIRED_CHECK} verdict not posted"
    return "cleared", "cleared"


def ci_cleared(checks):
    state, why = ci_status(checks)
    return state == "cleared", why


def role_of(snap, pins):
    if snap.author in ME:
        return "mine"
    if snap.review_requests & ME or snap.number in pins:
        return "asked"
    if any(r["author"] in ME for r in snap.reviews):
        return "asked"  # reviewed it before: follow the author's corrections
    return "other"


def _my_last_reviewed_head(snap, row):
    if row.last_reviewed_head:
        return row.last_reviewed_head
    mine = [r for r in snap.reviews if r["author"] in ME and r["commit"]]
    return max(mine, key=lambda r: r["submitted"])["commit"] if mine else ""


def decide(snap, row, pins=frozenset(), files_since_last=None, head_age_min=None):
    """Return (role, actions, note). `files_since_last`: paths changed between my last
    reviewed head and this head, or None when unknown (treated as non-trivial).
    `head_age_min`: minutes since this head was first seen, or None when unknown (no settle wait)."""
    role = role_of(snap, pins)
    if any(k in snap.title.lower() for k in SKIP_TITLE):
        return role, [], "ignored: skip keyword in the title"
    if snap.draft:
        return role, [], "draft"
    if role == "other":
        return role, [], ""
    ok, why = ci_cleared(snap.checks)
    if role == "mine":
        return role, *(_decide_mine(snap, row) if ok else ([], f"waiting for CI: {why}"))
    return role, *_decide_asked(snap, row, pins, files_since_last, head_age_min, ci=(ok, why))


def _decide_mine(snap, row):
    acts = []
    reviewed_here = any(r["author"] == REVIEWER and r["commit"] == snap.head for r in snap.reviews)
    if (REVIEWER and REVIEWER not in snap.review_requests and not reviewed_here
            and row.last_rerequest_head != snap.head):
        acts.append(Action("request_reviewer", snap.number, snap.head, reason="ready, CI cleared"))
    latest = {}
    for r in sorted(snap.reviews, key=lambda r: r["submitted"]):
        if r["author"] not in ME and r["state"] in ("APPROVED", "CHANGES_REQUESTED", "DISMISSED"):
            latest[r["author"]] = r
    asks = [r for r in latest.values() if r["state"] == "CHANGES_REQUESTED" and r["id"] > row.last_fix_review_id]
    if asks:
        r = max(asks, key=lambda r: r["id"])
        acts.append(Action("fix_draft", snap.number, snap.head, review_id=r["id"],
                           reason=f"{r['author']} requested changes"))
    return acts, ""


def _decide_asked(snap, row, pins, files_since_last, head_age_min=None, ci=(True, "")):
    # Done is what GitHub shows (a review of mine on this head), not what was written locally.
    if any(r["author"] in ME and r["commit"] == snap.head for r in snap.reviews):
        return [], ""
    last = _my_last_reviewed_head(snap, row)
    if last == snap.head:
        return [], ""
    if not last:
        # A first review does not wait for CI, so it lands early; an approval still waits for CI at post time.
        if snap.review_requests & ME or snap.number in pins:
            return [Action("review", snap.number, snap.head, reason="review requested")], ""
        return [], ""
    if not ci[0]:
        return [], f"waiting for CI: {ci[1]}"
    if snap.head == row.skipped_head:
        return [], ""
    if files_since_last is not None and all(TRIVIAL_PATH.search(f) for f in files_since_last):
        return [Action("skip", snap.number, snap.head, base=last)], (
            f"docs-only push since {last[:7]}; no model call, re-pin to force a review")
    rerequested = bool(snap.review_requests & ME)
    rounds = sum(1 for r in snap.reviews if r["author"] in ME and r["state"] == "CHANGES_REQUESTED")
    if MAX_CHANGE_ROUNDS and rounds >= MAX_CHANGE_ROUNDS and not rerequested:
        note = f"{rounds} change requests: no automatic re-review; re-request my review to get one"
        if not row.cap_commented:
            return [Action("cap_comment", snap.number, snap.head, reason=f"{rounds} change requests")], note
        return [], note
    if SETTLE_MIN and not rerequested and head_age_min is not None and head_age_min < SETTLE_MIN:
        return [], f"settling: re-review once {snap.head[:7]} is {SETTLE_MIN} min old"
    return [Action("rereview", snap.number, snap.head, base=last,
                   reason=f"files changed since {last[:7]}")], ""


def within_caps(action, calls_today, calls_today_pr, daily_cap, per_pr_cap):
    if action.kind not in MODEL_KINDS:
        return True, ""
    # a cap of 0 means no cap
    if daily_cap and calls_today >= daily_cap:
        return False, f"daily cap {daily_cap} reached"
    if per_pr_cap and calls_today_pr >= per_pr_cap:
        return False, f"per-PR cap {per_pr_cap} reached"
    return True, ""
