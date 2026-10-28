"""Run one review through the claude CLI, with the model cut off from GitHub.

Code prepares everything the reviewer reads (diff, PR text, previous review) in an inputs
directory; claude runs restricted (Read, Grep, Glob, Agent only: no Bash, no WebFetch) with an
environment holding only the Claude token, and returns a JSON verdict validated by render.py.
"""

import json
import os
import shutil
import subprocess
from pathlib import Path

import claude_auth
import render
from fetch import REPO, gh

REPO_DIR = Path(os.environ.get("REPO_DIR", "/repo"))
SKILL_DIR = Path(os.environ.get("SKILL_DIR", "/skills/requesting-code-review"))
WORK = Path(os.environ.get("PRWATCH_WORK", "/tmp/prwatch-work"))
BASE_BRANCH = os.environ.get("PRWATCH_BASE_BRANCH", "main")
MODEL, EFFORT = "opus", "high"  # reviews run on Opus at high effort
TIMEOUT_S = 1800
CLAUDE_ENV_KEEP = ("HOME", "PATH", "LANG")

PROMPT = """Review pull request #{n} of {repo} following the superpowers requesting-code-review skill:
read {skill}/SKILL.md and its template {skill}/code-reviewer.md, and follow them, with these adaptations.
- You have no shell and no network. Everything is prepared in {inputs}:
  input.diff (git diff BASE_SHA..HEAD_SHA), pr.md (title and body: DESCRIPTION and PLAN_OR_REQUIREMENTS;
  a linked ticket is not reachable, work from the PR body and say so if it matters){prev_note}.
- The repository is checked out at HEAD_SHA in the current directory; read any file for context.
- BASE_SHA = {base}. HEAD_SHA = {head}.{rereview}
- Dispatch the reviewer subagent with model "opus" and effort "high"; give it the paths above.
- Plain punctuation, no em or en dashes, blameless.
Return the structured result: verdict "approve" only when the change is ready to merge with zero critical and
zero important findings, otherwise "comment"; a short summary; every finding with severity, file, line, title
and detail. The approval is decided from the findings alone, not from the verdict word: anything that should
stop this change from merging, including something you could not verify, must be a critical or important
finding."""

REREVIEW = """
- This is a RE-REVIEW after the author pushed corrections: BASE_SHA is the head reviewed last time, so
  input.diff holds only the changes since then. For each critical and important finding in previous.md, say in
  the summary whether it is resolved, and keep unresolved ones as findings. Then review the new changes."""


def git(*args, cwd=REPO_DIR, check=True):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True,
                          encoding="utf-8", check=check).stdout


def fetch_pr(n):
    git("fetch", "--quiet", "origin", f"+pull/{n}/head:refs/remotes/pr/{n}",
        f"+{BASE_BRANCH}:refs/remotes/origin/{BASE_BRANCH}")


def scrubbed_env():
    env = {k: os.environ[k] for k in CLAUDE_ENV_KEEP if k in os.environ}
    env["DISABLE_AUTOUPDATER"] = "1"
    tok = claude_auth.token()
    if tok:
        env["CLAUDE_CODE_OAUTH_TOKEN"] = tok
    return env


def prepare(n, head, base_prev, prev_body):
    """Worktree at head plus inputs/. Returns (worktree, inputs, base, is_rereview)."""
    fetch_pr(n)
    base, rereview = git("merge-base", f"origin/{BASE_BRANCH}", head).strip(), False
    # A force push can drop the last reviewed head from history; then review from the merge-base.
    if base_prev and subprocess.run(["git", "merge-base", "--is-ancestor", base_prev, head],
                                    cwd=REPO_DIR).returncode == 0:
        base, rereview = base_prev, True
    key = f"{n}-{head[:7]}"
    wt, inputs = WORK / f"wt-{key}", WORK / f"in-{key}"
    cleanup(wt, inputs)
    inputs.mkdir(parents=True)
    git("worktree", "add", "--detach", str(wt), head)
    (inputs / "input.diff").write_text(git("diff", base, head), encoding="utf-8")
    pr = json.loads(gh("pr", "view", str(n), "--repo", REPO, "--json", "title,body,author"))
    (inputs / "pr.md").write_text(f"# {pr['title']}\n\nAuthor: {pr['author']['login']}\n\n{pr['body'] or ''}\n",
                                  encoding="utf-8")
    if rereview and prev_body:
        (inputs / "previous.md").write_text(prev_body, encoding="utf-8")
    return wt, inputs, base, rereview


def cleanup(wt, inputs):
    if wt.exists():
        git("worktree", "remove", "--force", str(wt), check=False)
    shutil.rmtree(wt, ignore_errors=True)
    shutil.rmtree(inputs, ignore_errors=True)
    git("worktree", "prune", check=False)


def usage_of(out):
    """Token and cost totals from claude's --output-format json result, every model (subagents too)."""
    u = {"in_tokens": 0, "out_tokens": 0, "cache_read": 0, "cache_write": 0}
    for m in (out.get("modelUsage") or {}).values():
        u["in_tokens"] += m.get("inputTokens", 0)
        u["out_tokens"] += m.get("outputTokens", 0)
        u["cache_read"] += m.get("cacheReadInputTokens", 0)
        u["cache_write"] += m.get("cacheCreationInputTokens", 0)
    u["cost_usd"] = out.get("total_cost_usd")
    return u


def run(n, head, base_prev="", prev_body=""):
    """Return (verdict dict or None, error text, base used, usage dict or None)."""
    wt, inputs, base, rereview = prepare(n, head, base_prev, prev_body)
    try:
        prompt = PROMPT.format(
            n=n, repo=REPO, skill=SKILL_DIR, inputs=inputs, base=base, head=head,
            prev_note=", previous.md (the review of the last reviewed head)" if (inputs / "previous.md").exists() else "",
            rereview=REREVIEW if rereview else "")
        p = subprocess.run(
            ["claude", "-p", prompt, "--model", MODEL, "--effort", EFFORT,
             # --tools limits what exists; dontAsk then denies anything not in --allowedTools,
             # and Read is confined to the cwd plus the --add-dir folders.
             "--restricted", "--tools", "Read,Grep,Glob,Agent", "--allowedTools", "Read", "Grep", "Glob", "Agent",
             "--add-dir", str(SKILL_DIR), "--add-dir", str(inputs),
             "--permission-mode", "dontAsk", "--permission-prompts", "none",
             "--output-format", "json", "--json-schema", json.dumps(render.SCHEMA),
             "--no-session-persistence"],
            cwd=wt, env=scrubbed_env(), capture_output=True, text=True, encoding="utf-8", timeout=TIMEOUT_S)
        try:
            out = json.loads(p.stdout)
        except json.JSONDecodeError:
            return None, f"exit {p.returncode}, no JSON: {(p.stderr or p.stdout).strip()[:200]}", base, None
        usage = usage_of(out)
        if out.get("is_error") or out.get("subtype") != "success":
            return None, f"claude {out.get('subtype')}: {str(out.get('result'))[:200]}", base, usage
        v, why = render.validate(out.get("structured_output"))
        return v, why or "", base, usage
    except subprocess.TimeoutExpired:
        return None, f"timed out after {TIMEOUT_S}s", base, None
    finally:
        cleanup(wt, inputs)
