"""Post a stored verdict to GitHub and read it back. Code only; never re-runs the model.

Outcomes: posted (confirmed by reading it back), dropped (head moved or PR closed unmerged),
retry (network or 5xx, backoff 2, 10, 30, 60 min, then stuck), stuck (422 or out of retries),
auth (401/403: the caller pauses every GitHub write).
"""

import json
import re
import subprocess
import tempfile

from prwatch import render
from prwatch import decide
from prwatch.decide import ME
from prwatch.fetch import REPO, AuthBroken, check_list, gh

POST_BACKOFF_MIN = (2, 10, 30, 60)


def _api(*args):
    """gh api call -> (http status or 0 for no response, parsed body or None, stderr)."""
    p = subprocess.run(["gh", "api", "-i", *args], capture_output=True, text=True, encoding="utf-8")
    m = re.match(r"HTTP/\S+ (\d{3})", p.stdout or "")
    status = int(m.group(1)) if m else 0
    if not status:
        m = re.search(r"HTTP (\d{3})", p.stderr or "")
        status = int(m.group(1)) if m else 0
    payload = (p.stdout or "").split("\r\n\r\n", 1)[-1].split("\n\n", 1)[-1] if p.stdout else ""
    try:
        data = json.loads(payload) if payload.strip() else None
    except json.JSONDecodeError:
        data = None
    if status in (401, 403):
        raise AuthBroken(f"HTTP {status}: {(p.stderr or payload).strip()[:200]}")
    return status, data, (p.stderr or "").strip()


def pr_facts(n, head):
    pr = json.loads(gh("pr", "view", str(n), "--repo", REPO, "--json",
                       "state,isDraft,headRefOid,labels,files,author,statusCheckRollup"))
    return {
        "state": pr["state"], "head": pr["headRefOid"], "draft": pr["isDraft"],
        "author": pr["author"]["login"], "labels": [l["name"].lower() for l in pr["labels"]],
        "files": [f["path"] for f in pr["files"]],
        "merged_at_head": pr["state"] == "MERGED" and pr["headRefOid"] == head,
        "ci": decide.ci_status(check_list(pr.get("statusCheckRollup") or [])),
    }


def existing_review(n, head, key):
    """Id of a review of mine on this head (posted by anyone, or carrying our marker), else None."""
    data = json.loads(gh("api", "--paginate", f"repos/{REPO}/pulls/{n}/reviews",
                         "--jq", "[.[] | {id, login: .user.login, commit_id, body}]") or "[]")
    for r in data:
        if r["commit_id"] == head and (r["login"] in ME or render.marker(key) in (r["body"] or "")):
            return r["id"]
    return None


def post(n, head, key, verdict, allow_approve, allow_request_changes=False):
    """Return (outcome, detail, review_id); outcome "wait" means an approval waits for CI.
    Raises AuthBroken on 401/403."""
    pr = pr_facts(n, head)
    if pr["state"] == "CLOSED":
        return "dropped", "closed unmerged", None
    if pr["head"] != head and not pr["merged_at_head"]:
        return "dropped", f"head moved to {pr['head'][:7]}", None
    rid = existing_review(n, head, key)
    if rid:
        return "posted", "already on GitHub", rid
    event, why = render.decide_event(verdict, pr, ME, allow_approve, allow_request_changes)
    if event == "WAIT":
        return "wait", f"approval waits: {why}", None
    payload = {"commit_id": head, "event": event, "body": render.body(verdict, head, key)}
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as fh:
        json.dump(payload, fh)
    status, data, err = _api("-X", "POST", f"repos/{REPO}/pulls/{n}/reviews", "--input", fh.name)
    if status == 422:
        return "stuck", f"422: {err[:200] or data}", None
    if not (200 <= status < 300) or not data or "id" not in data:
        return "retry", f"post HTTP {status or 'no response'}: {err[:150]}", None
    status, back, err = _api(f"repos/{REPO}/pulls/{n}/reviews/{data['id']}")
    if status != 200 or render.marker(key) not in ((back or {}).get("body") or ""):
        return "retry", f"read-back HTTP {status or 'no response'}", None  # existing_review finds it next time
    return "posted", f"{event}" + (f" ({why})" if why else ""), data["id"]
