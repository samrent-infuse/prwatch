"""Run one review through the claude CLI, with the model cut off from GitHub.

Code prepares everything the reviewer reads (diff, PR text, previous review, the conversation since,
project context) in an inputs directory; claude runs restricted (Read, Grep, Glob, Agent only: no Bash,
no WebFetch) and lean (no settings, hooks, MCP servers, skills or CLAUDE.md from the checkout) with an
environment holding only the Claude token, and returns a JSON verdict validated by render.py.
"""

import datetime
import json
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

from prwatch import prompts
from prwatch import brain
from prwatch import claude_auth
from prwatch import fetch
from prwatch import render
from prwatch.fetch import REPO, gh

REPO_DIR = Path(os.environ.get("REPO_DIR", "/repo"))
SKILL_DIR = Path(os.environ.get("SKILL_DIR", "/skills/requesting-code-review"))
WORK = Path(os.environ.get("PRWATCH_WORK", "/tmp/prwatch-work"))
HERE = Path(os.environ.get("INKHUB_DIR", "/inkhub")) / "prwatch"
STATE = HERE / "state"
PROJECT_CONTEXT = HERE / "project-context.md"  # what a reviewer must know about the watched repository
RUNS = STATE / "runs"                          # what each run got and returned, kept RUNS_KEEP_DAYS
RUNS_KEEP_DAYS = 14
# The project brain (a vault in the _CONVENTIONS format), mounted read-only; empty = no grounding.
_BRAIN = os.environ.get("PRWATCH_BRAIN_DIR", "/brain")
BRAIN_DIR = Path(_BRAIN) if _BRAIN else None
BRAIN_MAX_LEAVES = int(os.environ.get("PRWATCH_BRAIN_MAX_LEAVES", "12") or 12)
BASE_BRANCH = os.environ.get("PRWATCH_BASE_BRANCH", "main")
MODEL, EFFORT = "opus", "high"  # the run itself; its reviewer subagent is reviewer_model()
# The reviewer subagent runs on REVIEWER_MODEL_SMALL instead of MODEL when the diff it reads has at most
# SMALL_PR_MAX_LINES added plus removed lines and no changed path matches RISKY_PATHS. Empty = always MODEL.
REVIEWER_MODEL_SMALL = os.environ.get("PRWATCH_REVIEWER_MODEL_SMALL", "").strip()
SMALL_PR_MAX_LINES = int(os.environ.get("PRWATCH_SMALL_PR_MAX_LINES", "400") or 0)
RISKY_PATHS = re.compile(os.environ.get(
    "PRWATCH_RISKY_PATHS", r"^deploy/|^\.github/workflows/|schema\.sql$|/migrations?/|/main\.py$"))
TIMEOUT_S = 1800
# claude stops a run at this spend (its own list-price estimate); 0 = no limit.
MAX_RUN_USD = float(os.environ.get("PRWATCH_MAX_RUN_USD", "0") or 0)
CLAUDE_ENV_KEEP = ("HOME", "PATH", "LANG")
# The run's cwd is the PR's own checkout, so nothing in it may configure the run: no project settings or
# hooks (a PR could add one), no MCP servers, no skills, no CLAUDE.md. Repository knowledge comes from
# project-context.md instead. This also keeps the fixed context of every tool call small.
LEAN_SETTINGS = {"claudeMdExcludes": ["**/CLAUDE.md", "**/CLAUDE.local.md", "**/AGENTS.md", "**/.claude/rules/**"]}
LEAN_ARGS = ["--setting-sources", "user", "--settings", json.dumps(LEAN_SETTINGS),
             "--strict-mcp-config", "--no-chrome", "--disable-slash-commands"]

PROMPT = prompts.load("review")

# The three below are appended to PROMPT's bullet list when they apply, so each starts on a new line.
REREVIEW = "\n" + prompts.load("rereview")
BRAIN = "\n" + prompts.load("review-brain")
BASE_MOVED = "\n" + prompts.load("review-base-moved")


# Parallel runs share one clone: commands that write its refs or worktree list take turns.
GIT_LOCK = threading.Lock()
LOCKED_COMMANDS = ("fetch", "worktree")


def git(*args, cwd=None, check=True):
    def run():
        return subprocess.run(["git", *args], cwd=cwd or REPO_DIR, capture_output=True, text=True,
                              encoding="utf-8", check=check).stdout
    if args and args[0] in LOCKED_COMMANDS:
        with GIT_LOCK:
            return run()
    return run()


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


def is_ancestor(a, b):
    return subprocess.run(["git", "merge-base", "--is-ancestor", a, b], cwd=REPO_DIR).returncode == 0


def merge_base(tip):
    return git("merge-base", f"origin/{BASE_BRANCH}", tip).strip()


def patch_lines(tip):
    """The PR's whole patch at `tip` (merge-base..tip) as a sorted bag of (file, added or removed line).
    Context lines and line numbers are left out, so moving the branch onto a newer base changes nothing."""
    out, path = [], ""
    for line in git("diff", "-U0", merge_base(tip), tip).splitlines():
        if line.startswith("+++ "):
            path = line[6:] if line.startswith("+++ b/") else line[4:]
        elif line.startswith("--- "):
            continue
        elif line[:1] in "+-":
            out.append((path, line))
    return sorted(out)


def same_patch(n, reviewed, head):
    """True when the PR at `head` adds and removes exactly the lines it did at `reviewed` (a rebase, a merge
    from the base branch, a squash); False when anything differs; None when `reviewed` cannot be fetched.
    A block moved within one file, unchanged, does not show up."""
    fetch_pr(n)
    git("fetch", "--quiet", "origin", reviewed, check=False)
    if subprocess.run(["git", "cat-file", "-e", f"{reviewed}^{{commit}}"], cwd=REPO_DIR,
                      capture_output=True).returncode != 0:
        return None
    return patch_lines(reviewed) == patch_lines(head)


def prepare(n, head, base_prev, prev_body):
    """Worktree at head plus inputs/. Returns (worktree, inputs, base, is_rereview, base_moved)."""
    fetch_pr(n)
    base, rereview, moved = merge_base(head), False, False
    if base_prev and is_ancestor(base_prev, head) and merge_base(base_prev) == base:
        base, rereview = base_prev, True       # plain new commits: review only those
    elif base_prev and prev_body:
        rereview, moved = True, True           # rebased or merged the base branch: whole PR, old findings
    key = f"{n}-{head[:7]}"
    wt, inputs = WORK / f"wt-{key}", WORK / f"in-{key}"
    cleanup(wt, inputs)
    inputs.mkdir(parents=True)
    git("worktree", "add", "--detach", str(wt), head)
    (inputs / "input.diff").write_text(git("diff", base, head), encoding="utf-8")
    pr = json.loads(gh("pr", "view", str(n), "--repo", REPO, "--json", "title,body,author"))
    (inputs / "pr.md").write_text(f"# {pr['title']}\n\nAuthor: {pr['author']['login']}\n\n{pr['body'] or ''}\n",
                                  encoding="utf-8")
    if PROJECT_CONTEXT.exists():
        shutil.copyfile(PROJECT_CONTEXT, inputs / "project-context.md")
    if BRAIN_DIR:
        try:
            brain.ground(BRAIN_DIR, inputs, git("diff", "--name-only", base, head).split(), BRAIN_MAX_LEAVES,
                         WORK / "brain_index.json")
        except Exception as exc:  # the review still runs, only without the brain
            brain.log_event(f"grounding #{n} failed: {exc}")
    if rereview:
        if prev_body:
            (inputs / "previous.md").write_text(prev_body, encoding="utf-8")
        try:
            talk = fetch.conversation_since(n, base_prev)
        except fetch.AuthBroken:
            raise
        except Exception:  # the re-review still runs, only without the conversation
            talk = ""
        if talk:
            (inputs / "conversation.md").write_text(talk, encoding="utf-8")
    return wt, inputs, base, rereview, moved


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


def reviewer_model(diff):
    """(model, why) for the reviewer subagent, from the diff it will read."""
    lines = sum(1 for l in diff.splitlines() if l[:1] in "+-" and not l.startswith(("+++ ", "--- ")))
    paths = [l[6:] for l in diff.splitlines() if l.startswith("+++ b/")]
    paths += [l[6:] for l in diff.splitlines() if l.startswith("--- a/")]
    risky = next((p for p in paths if RISKY_PATHS.search(p)), "")
    if REVIEWER_MODEL_SMALL and lines <= SMALL_PR_MAX_LINES and not risky:
        return REVIEWER_MODEL_SMALL, f"{lines} lines"
    return MODEL, f"{lines} lines" + (f", touches {risky}" if risky else "")


def brain_note(inputs):
    """(prompt section, extra claude args) for a grounded review; ("", []) without brain.md."""
    if not BRAIN_DIR or not (Path(inputs) / "brain.md").exists():
        return "", []
    return BRAIN.format(vault=BRAIN_DIR), ["--add-dir", str(BRAIN_DIR)]


def budget_args():
    return ["--max-budget-usd", f"{MAX_RUN_USD:g}"] if MAX_RUN_USD > 0 else []


def keep_run(name, files):
    """Keep what a run got and returned, {file name: text}, in RUNS/<name>-<time>/, and drop records older
    than RUNS_KEEP_DAYS. Returns the folder, or None when it could not be written. Never fails a run."""
    try:
        stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        folder = RUNS / f"{name}-{stamp}"
        folder.mkdir(parents=True, exist_ok=True)
        for fname, text in files.items():
            (folder / fname).write_text(text or "", encoding="utf-8")
        cutoff = time.time() - RUNS_KEEP_DAYS * 86400
        for old in RUNS.iterdir():
            if old.stat().st_mtime < cutoff:
                shutil.rmtree(old, ignore_errors=True) if old.is_dir() else old.unlink()
        return folder
    except OSError:
        return None


def run(n, head, base_prev="", prev_body="", reviewer=None):
    """Return (verdict dict or None, error text, base used, usage dict or None). `reviewer` overrides the
    reviewer subagent's model (otherwise reviewer_model() picks it from the diff)."""
    wt, inputs, base, rereview, moved = prepare(n, head, base_prev, prev_body)
    reviewer = reviewer or reviewer_model((inputs / "input.diff").read_text(encoding="utf-8"))[0]
    brain_text, brain_dirs = brain_note(inputs)
    try:
        notes = "".join(note for name, note in (
            ("project-context.md", ", project-context.md (what a reviewer must know about this repository)"),
            ("previous.md", ", previous.md (the review of the last reviewed head)"),
            ("conversation.md", ", conversation.md (what people wrote on the PR since that review)"),
            ("brain.md", ", brain.md (project brain leaves about the changed files)"),
        ) if (inputs / name).exists())
        prompt = PROMPT.format(
            n=n, repo=REPO, skill=SKILL_DIR, inputs=inputs, base=base, head=head, notes=notes,
            range_note=BASE_MOVED.format(branch=BASE_BRANCH) if moved else "",
            rereview=REREVIEW if rereview else "", reviewer=reviewer, brain=brain_text)
        p = subprocess.run(
            ["claude", "-p", prompt, "--model", MODEL, "--effort", EFFORT, *LEAN_ARGS,
             # --tools limits what exists; dontAsk then denies anything not in --allowedTools,
             # and Read is confined to the cwd plus the --add-dir folders.
             "--restricted", "--tools", "Read,Grep,Glob,Agent", "--allowedTools", "Read", "Grep", "Glob", "Agent",
             "--add-dir", str(SKILL_DIR), "--add-dir", str(inputs), *brain_dirs,
             "--permission-mode", "dontAsk", "--permission-prompts", "none",
             "--output-format", "json", "--json-schema", json.dumps(render.SCHEMA),
             *budget_args(), "--no-session-persistence"],
            cwd=wt, env=scrubbed_env(), capture_output=True, text=True, encoding="utf-8", timeout=TIMEOUT_S)
        keep_run(f"PR-{n}-{head[:7]}", {"prompt.md": prompt, "result.json": p.stdout or p.stderr})
        try:
            out = json.loads(p.stdout)
        except json.JSONDecodeError:
            return None, f"exit {p.returncode}, no JSON: {(p.stderr or p.stdout).strip()[:200]}", base, None
        usage = {**usage_of(out), "reviewer": reviewer}
        if out.get("is_error") or out.get("subtype") != "success":
            return None, f"claude {out.get('subtype')}: {str(out.get('result'))[:200]}", base, usage
        v, why = render.validate(out.get("structured_output"))
        return v, why or "", base, usage
    except subprocess.TimeoutExpired:
        return None, f"timed out after {TIMEOUT_S}s", base, None
    finally:
        cleanup(wt, inputs)
