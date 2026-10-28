"""Read-only GitHub reads for the PR watcher: one GraphQL query per tick for every open PR."""

import json
import os
import re
import subprocess

from decide import ME, Snapshot

OWNER, NAME = os.environ.get("PRWATCH_REPO", "example-org/example-repo").split("/", 1)
REPO = f"{OWNER}/{NAME}"

QUERY = """
query($owner:String!, $name:String!) {
  repository(owner:$owner, name:$name) {
    pullRequests(states:OPEN, first:60, orderBy:{field:UPDATED_AT, direction:DESC}) {
      nodes {
        number title isDraft baseRefName headRefOid author { login }
        labels(first:20) { nodes { name } }
        reviewRequests(first:20) { nodes { requestedReviewer { ... on User { login } } } }
        reviews(last:40) { nodes { databaseId author { login } state submittedAt commit { oid } } }
        commits(last:1) { nodes { commit { statusCheckRollup { contexts(first:60) { nodes {
          __typename
          ... on CheckRun { name status conclusion }
          ... on StatusContext { context state }
        } } } } } }
      }
    }
  }
}
"""


class AuthBroken(Exception):
    """GitHub answered 401/403: every write pauses until `gh api user` works again."""


AUTH_ERR = re.compile(r"HTTP 40[13]|Bad credentials|Resource not accessible|authentication", re.I)


def gh(*args, check=True):
    p = subprocess.run(["gh", *args], capture_output=True, text=True, encoding="utf-8")
    if check and p.returncode != 0:
        if AUTH_ERR.search(p.stderr or ""):
            raise AuthBroken(f"gh {' '.join(args[:3])}: {p.stderr.strip()[:200]}")
        raise RuntimeError(f"gh {' '.join(args[:3])} failed: {p.stderr.strip()[:300]}")
    return p.stdout


def _checks(node):
    commits = node["commits"]["nodes"]
    rollup = commits[0]["commit"]["statusCheckRollup"] if commits else None
    out = []
    for c in (rollup or {}).get("contexts", {}).get("nodes", []):
        if c["__typename"] == "CheckRun":
            out.append({"name": c["name"], "status": c["status"],
                        "conclusion": (c["conclusion"] or "").upper()})
        else:  # StatusContext: a commit status, e.g. an external scanner's verdict
            state = c["state"].upper()
            out.append({"name": c["context"],
                        "status": "PENDING" if state in ("PENDING", "EXPECTED") else "COMPLETED",
                        "conclusion": state})
    return out


def open_prs():
    data = json.loads(gh("api", "graphql", "-f", f"query={QUERY}", "-F", f"owner={OWNER}", "-F", f"name={NAME}"))
    snaps = []
    for n in data["data"]["repository"]["pullRequests"]["nodes"]:
        snaps.append(Snapshot(
            number=n["number"], title=n["title"], author=(n["author"] or {}).get("login", ""),
            draft=n["isDraft"], head=n["headRefOid"], base=n["baseRefName"],
            labels={l["name"].lower() for l in n["labels"]["nodes"]},
            review_requests={(r["requestedReviewer"] or {}).get("login", "") for r in n["reviewRequests"]["nodes"]} - {""},
            reviews=[{"id": r["databaseId"], "author": (r["author"] or {}).get("login", ""), "state": r["state"],
                      "commit": (r["commit"] or {}).get("oid", ""), "submitted": r["submittedAt"] or ""}
                     for r in n["reviews"]["nodes"]],
            checks=_checks(n)))
    return snaps


def files_between(base, head):
    """Paths changed from base to head, or None when GitHub cannot compare (e.g. a force push)."""
    p = subprocess.run(["gh", "api", f"repos/{REPO}/compare/{base}...{head}", "--jq", "[.files[].filename]"],
                       capture_output=True, text=True, encoding="utf-8")
    if p.returncode != 0:
        return None
    return json.loads(p.stdout or "null")


def previous_review(n, head):
    """My last review with a body on `head`, plus its inline comments, as text; "" when there is none.

    Used when reviews/PR-n-sha7.md is missing because the earlier review was not written by prwatch."""
    reviews = json.loads(gh("api", f"repos/{REPO}/pulls/{n}/reviews?per_page=100"))
    mine = [r for r in reviews if (r.get("user") or {}).get("login") in ME
            and r.get("commit_id") == head and (r.get("body") or "").strip()]
    if not mine:
        return ""
    r = mine[-1]
    comments = json.loads(gh("api", f"repos/{REPO}/pulls/{n}/reviews/{r['id']}/comments"))
    inline = "\n".join(f"{c['path']}:{c.get('line') or c.get('original_line')}: {c['body']}" for c in comments)
    return r["body"].strip() + ("\n\nInline comments:\n" + inline if inline else "")


def _login(item):
    return (item.get("user") or {}).get("login", "")


def conversation_since(n, head):
    """What people wrote on the PR after my last review on `head` (reviews, comments, inline comments),
    oldest first, as text; "" when there is nothing or no review of mine on that head. Bots are left out.

    An author's reply can settle a finding without a code change, so a re-review reads it."""
    reviews = json.loads(gh("api", f"repos/{REPO}/pulls/{n}/reviews?per_page=100"))
    mine = [r["submitted_at"] for r in reviews
            if _login(r) in ME and r.get("commit_id") == head and r.get("submitted_at")]
    if not mine:
        return ""
    since = max(mine)
    items = [(r["submitted_at"], _login(r), f"review, {r['state'].lower()}", r.get("body") or "")
             for r in reviews if (r.get("submitted_at") or "") > since]
    for c in json.loads(gh("api", f"repos/{REPO}/issues/{n}/comments?since={since}&per_page=100")):
        items.append((c["created_at"], _login(c), "comment", c.get("body") or ""))
    for c in json.loads(gh("api", f"repos/{REPO}/pulls/{n}/comments?since={since}&per_page=100")):
        items.append((c["created_at"], _login(c), f"on {c['path']}:{c.get('line') or c.get('original_line')}",
                      c.get("body") or ""))
    items = sorted(i for i in items if i[0] > since and i[3].strip() and not i[1].endswith("[bot]"))
    return "\n\n".join(f"{t} {who} ({what}):\n{body.strip()}" for t, who, what, body in items)[:20000]
