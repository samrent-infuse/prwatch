"""Side effects besides reviews: request a reviewer, draft a fix on one of my PRs.

Fix drafts run claude restricted (Read, Edit, Write, Glob, Grep; no shell, no network) with only the
Claude token in its environment, in a scratch worktree; code commits and exports a patch. Nothing is
ever pushed.
"""

import json
import os
import subprocess
from pathlib import Path

from prwatch import prompts
from prwatch import claude_auth
from prwatch import post
from prwatch import review
from prwatch.decide import REVIEWER
from prwatch.fetch import REPO, gh

INKHUB = Path(os.environ.get("INKHUB_DIR", "/inkhub"))
ROOT = INKHUB / "prwatch"   # this folder: the files a user edits (pins.txt, project-context.md)
OUT = ROOT / "state"         # everything prwatch writes: logs, status.json, runs/, drafts/, brain-backups/
DRAFTS = OUT / "drafts"
REVIEWS = INKHUB / "reviews"
STOP = INKHUB / "STOP"

FIX_PROMPT = prompts.load("fix-draft")


def claude_ready():
    return claude_auth.state()[0]


def request_reviewer(n):
    status, _, err = post._api("-X", "POST", f"repos/{REPO}/pulls/{n}/requested_reviewers",
                               "-f", f"reviewers[]={REVIEWER}")
    if not 200 <= status < 300:
        raise RuntimeError(f"request {REVIEWER} on #{n}: HTTP {status or 'no response'} {err[:150]}")

CAP_COMMENT = ("Automated review: this PR has had {rounds} change requests from me, so new commits no longer start "
               "an automatic re-review. When it is ready for another look, re-request my review, or let's talk it "
               "through.")


def cap_comment(n, rounds):
    """One comment telling the author that automatic re-reviews stopped at the change-round cap."""
    status, _, err = post._api("-X", "POST", f"repos/{REPO}/issues/{n}/comments",
                               "-f", f"body={CAP_COMMENT.format(rounds=rounds)}")
    if not 200 <= status < 300:
        raise RuntimeError(f"cap comment on #{n}: HTTP {status or 'no response'} {err[:150]}")


def fix_draft(n, head, review_id):
    """Draft fixes for one CHANGES_REQUESTED review into drafts/PR-n-review-id.patch. Never pushes.
    Returns (outcome text, usage dict or None)."""
    r =json.loads(gh("api", f"repos/{REPO}/pulls/{n}/reviews/{review_id}"))
    comments = json.loads(gh("api", f"repos/{REPO}/pulls/{n}/reviews/{review_id}/comments"))
    inline = "\n".join(f"{c['path']}:{c.get('line') or c.get('original_line')}: {c['body']}" for c in comments) or "(none)"
    review.fetch_pr(n)
    wt, scratch = review.WORK / f"fix-{n}-{review_id}", review.WORK / f"fixin-{n}-{review_id}"
    review.cleanup(wt, scratch)
    review.git("worktree", "add", "--detach", str(wt), head)
    try:
        prompt = FIX_PROMPT.format(n=n, repo=REPO, head=head, author=r["user"]["login"],
                                   body=r.get("body") or "(empty)", comments=inline)
        p = subprocess.run(
            ["claude", "-p", prompt, "--model", review.MODEL, "--effort", review.EFFORT,
             # no settings, hooks, MCP servers or skills from the checkout; its CLAUDE.md stays readable
             "--setting-sources", "user", "--strict-mcp-config", "--no-chrome", "--disable-slash-commands",
             *review.budget_args(), "--restricted", "--tools", "Read,Edit,Write,Glob,Grep",
             "--allowedTools", "Read", "Edit", "Write", "Glob", "Grep",
             "--permission-mode", "acceptEdits", "--permission-prompts", "none", "--no-session-persistence",
             "--output-format", "json"],
            cwd=wt, env=review.scrubbed_env(), capture_output=True, text=True, encoding="utf-8",
            timeout=review.TIMEOUT_S)
        review.keep_run(f"fix-PR-{n}-review-{review_id}", p.stdout or p.stderr)
        try:
            out = json.loads(p.stdout)
        except json.JSONDecodeError:
            out = {}
        usage = review.usage_of(out) if out else None
        if p.returncode != 0 or out.get("is_error"):
            return f"fix draft failed: {(p.stderr or str(out.get('result'))).strip()[:200]}", usage
        text = str(out.get("result") or "")
        review.git("add", "-A", cwd=wt)
        if not review.git("status", "--porcelain", cwd=wt).strip():
            return "fix draft: no changes made; " + text.strip()[-300:], usage
        review.git("-c", "user.name=prwatch", "-c", "user.email=prwatch@localhost",
                   "commit", "-q", "-m", f"WIP address review {review_id} on #{n}", cwd=wt)
        DRAFTS.mkdir(parents=True, exist_ok=True)
        patch = DRAFTS / f"PR-{n}-review-{review_id}.patch"
        patch.write_text(review.git("format-patch", "--stdout", "-1", "HEAD", cwd=wt), encoding="utf-8")
        (DRAFTS / f"PR-{n}-review-{review_id}.md").write_text(text, encoding="utf-8")
        return f"fix drafted: {patch.name} (not pushed)", usage
    finally:
        review.cleanup(wt, scratch)
