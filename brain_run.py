"""The brain refresh: what needs git or claude. Sync points, the master scan, anchors, the refresh call
and the backfill. Every write to the vault goes through brain.apply."""

import json
import os
import re
import shutil
import subprocess

import brain
import review
from store import later, stamp

IGNORED = ("tests/", "docs/")
BRAIN_MODEL = os.environ.get("PRWATCH_BRAIN_MODEL", "sonnet")
BRAIN_MAX_RUN_USD = float(os.environ.get("PRWATCH_BRAIN_MAX_RUN_USD", "2") or 0)
DIFF_MAX_LINES = 3000
RETRY_MIN = (30, 90)  # after the 3rd failed call the leaves are stuck

SCHEMA = {
    "type": "object",
    "properties": {
        "leaves": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "slug": {"type": "string"},
                "verdict": {"type": "string", "enum": ["unchanged", "edited"]},
                "edits": {"type": "array", "items": {
                    "type": "object", "properties": {"old": {"type": "string"}, "new": {"type": "string"}},
                    "required": ["old", "new"]}},
                "why": {"type": "string"},
            },
            "required": ["slug", "verdict"],
        }},
        "suggestions": {"type": "array", "items": {
            "type": "object",
            "properties": {"kind": {"type": "string", "enum": ["new_leaf", "manifest_hook"]},
                           "slug": {"type": "string"}, "text": {"type": "string"}},
            "required": ["kind", "text"]}},
    },
    "required": ["leaves"],
}

PROMPT = """You keep reference leaves of a project knowledge base true to the code. You are an unattended run:
nobody reads this while it runs, so never ask a question.
Inputs in {inputs}: leaves/ (the leaves to check, markdown with frontmatter), {what}, and commits.txt (the
subject lines of the commits in that range). The repository is checked out at {head} in the current
directory; read code to check details. Aim for about 15 tool calls.
For each leaf decide whether the code change makes any statement in it wrong or incomplete. If not: verdict
"unchanged". If so: verdict "edited" with the smallest edits that make it true again. Each edit's "old" is
text copied exactly from the leaf that occurs once in it; "new" is its replacement. Touch nothing the change
does not bear on, and leave the frontmatter alone (code updates as_of and source). In "why", quote the line
of code each edit rests on.
You may suggest a new leaf, or a changed one-line MANIFEST hook, in "suggestions"; they are logged for a
person and never applied.
Plain punctuation, no em or en dashes."""

DIFF_WHAT = "diff.txt (git diff of the files the leaves name, from each leaf's last check to {head})"
VERIFY_WHAT = "current/ (the current versions of the files the leaves name, at {head}; the leaves are undated)"


def _ignored(path):
    return path.startswith(IGNORED) or "/tests/" in path or path.endswith(".md")


def anchor(as_of):
    """The last master commit on or before the `as_of` day, or "" for an undated leaf."""
    if not as_of or as_of == "undated" or len(as_of) < 10:
        return ""
    return review.git("rev-list", "-1", f"--before={as_of[:10]}T23:59:59Z",
                      f"origin/{review.BASE_BRANCH}", check=False).strip()


def _is_commit(sha):
    return bool(sha) and subprocess.run(["git", "cat-file", "-e", f"{sha}^{{commit}}"], cwd=review.REPO_DIR,
                                        capture_output=True).returncode == 0


def changed_named_files(sync_sha, head, paths):
    """The named `paths` that changed from `sync_sha` to `head` (edited, deleted, or renamed either way),
    tests and docs left out; None when `sync_sha` is not a known commit."""
    if not _is_commit(sync_sha):
        return None
    touched = set()
    for line in review.git("diff", "--name-status", sync_sha, head).splitlines():
        touched.update(line.split("\t")[1:])
    return [p for p in paths if p in touched and not _ignored(p)]


def scan(store, index, head, prev_head=""):
    """After master moved from `prev_head` to `head`: mark due the reference leaves whose named files
    changed since their sync point, move the sync point of the rest. Returns the slugs newly due. A leaf
    already due restarts its settle wait only when this move touched its files. An unknown or missing
    sync point is re-anchored from the leaf's as_of; an undated leaf without one is left to the backfill."""
    newly = []
    for leaf in index:
        if leaf.folder != "reference" or not leaf.repo_paths:
            continue
        row = store.brain_row(leaf.slug)
        if row is not None and row["state"] == "stuck":
            continue
        sync = row["sync_sha"] if row is not None else ""
        changed = changed_named_files(sync, head, leaf.repo_paths)
        if changed is None:
            sync = anchor(leaf.as_of)
            changed = changed_named_files(sync, head, leaf.repo_paths)
            if changed is None:
                continue  # undated, or older than the clone: the backfill's verify pass
            store.brain_upsert(leaf.slug, path=f"{leaf.folder}/{leaf.path.name}", sync_sha=sync)
        if changed:
            already = row is not None and row["state"] == "due"
            if not already:
                newly.append(leaf.slug)
            if not already or (prev_head and changed_named_files(prev_head, head, leaf.repo_paths)):
                store.brain_upsert(leaf.slug, path=f"{leaf.folder}/{leaf.path.name}", state="due",
                                   touched_at=stamp())
        else:
            store.brain_upsert(leaf.slug, path=f"{leaf.folder}/{leaf.path.name}", sync_sha=head, synced_at=stamp())
    return newly


def validate(out):
    """(result, "") when the refresh output is usable, else (None, why)."""
    if not isinstance(out, dict) or not isinstance(out.get("leaves"), list):
        return None, "no leaves"
    for l in out["leaves"]:
        if not isinstance(l, dict) or not isinstance(l.get("slug"), str) or l.get("verdict") not in ("unchanged", "edited"):
            return None, f"malformed leaf {str(l)[:80]}"
        for e in l.get("edits") or []:
            if not isinstance(e, dict) or not isinstance(e.get("old"), str) or not isinstance(e.get("new"), str):
                return None, f"malformed edit in {l['slug']}"
    if not isinstance(out.get("suggestions", []), list):
        return None, "malformed suggestions"
    return out, ""


def _claude(prompt, cwd, add_dirs):
    """Run the refresh call; (stdout, returncode). The only place this module starts claude."""
    args = ["claude", "-p", prompt, "--model", BRAIN_MODEL, "--effort", "medium", *review.LEAN_ARGS,
            "--restricted", "--tools", "Read,Grep,Glob", "--allowedTools", "Read", "Grep", "Glob",
            *[a for d in add_dirs for a in ("--add-dir", str(d))],
            "--permission-mode", "dontAsk", "--permission-prompts", "none",
            "--output-format", "json", "--json-schema", json.dumps(SCHEMA), "--no-session-persistence"]
    if BRAIN_MAX_RUN_USD > 0:
        args += ["--max-budget-usd", f"{BRAIN_MAX_RUN_USD:g}"]
    try:
        p = subprocess.run(args, cwd=cwd, env=review.scrubbed_env(), capture_output=True, text=True,
                           encoding="utf-8", timeout=review.TIMEOUT_S)
    except subprocess.TimeoutExpired:
        return "", 124
    return p.stdout or p.stderr, p.returncode


def _sync_of(store, slug):
    row = store.brain_row(slug)
    return row["sync_sha"] if row is not None and _is_commit(row["sync_sha"]) else ""


def _prepare(group, head, inputs, store, verify):
    """Copy the leaves, write diff.txt (or current/ when verifying) and commits.txt.
    Returns ({slug: mtime when read}, [PR numbers from the commit subjects])."""
    mtimes, subjects, parts = {}, [], []
    for leaf in group:
        dest = inputs / "leaves" / leaf.folder / leaf.path.name
        dest.parent.mkdir(parents=True, exist_ok=True)
        mtimes[leaf.slug] = leaf.path.stat().st_mtime
        shutil.copyfile(leaf.path, dest)
        sync = _sync_of(store, leaf.slug)
        if sync:
            subjects += review.git("log", "--format=%s", f"{sync}..{head}", "--", *leaf.repo_paths).splitlines()
            if not verify:
                parts.append(review.git("diff", sync, head, "--", *leaf.repo_paths))
    if verify:
        for path in sorted({p for l in group for p in l.repo_paths}):
            text = review.git("show", f"{head}:{path}", check=False)
            if text:
                (inputs / "current" / path).parent.mkdir(parents=True, exist_ok=True)
                (inputs / "current" / path).write_text(text, encoding="utf-8")
    else:
        diff = "\n".join(parts).splitlines()
        if len(diff) > DIFF_MAX_LINES:
            stat = [review.git("diff", "--stat", _sync_of(store, l.slug), head, "--", *l.repo_paths)
                    for l in group if _sync_of(store, l.slug)]
            diff = ["(diff over 3,000 lines: the stat follows; read the files you need)", *"\n".join(stat).splitlines()]
        (inputs / "diff.txt").write_text("\n".join(diff) + "\n", encoding="utf-8")
    subjects = list(dict.fromkeys(subjects))
    (inputs / "commits.txt").write_text("\n".join(subjects) + "\n", encoding="utf-8")
    prs = sorted({int(m) for s in subjects for m in re.findall(r"\(#(\d+)\)", s)})
    return mtimes, prs


def _failed(store, group, why):
    """Count a failed try: retry after RETRY_MIN, then stuck."""
    for leaf in group:
        row = store.brain_row(leaf.slug)
        tries = (row["tries"] if row is not None else 0) + 1
        if tries > len(RETRY_MIN):
            store.brain_upsert(leaf.slug, state="stuck", tries=tries, last_error=why[:300], next_try_at="")
        else:
            store.brain_upsert(leaf.slug, tries=tries, last_error=why[:300], next_try_at=later(RETRY_MIN[tries - 1]))
    return f"failed: {why}"


def refresh(store, vault, group, head, kind="brain_refresh", verify=False):
    """One refresh call for `group` against master at `head`, then code applies the result. Returns a
    one-line outcome. `verify` (the backfill's undated leaves) sends current files instead of a diff."""
    slugs = ", ".join(l.slug for l in group)
    wt, inputs = review.WORK / f"brain-wt-{head[:7]}", review.WORK / f"brain-in-{head[:7]}"
    review.cleanup(wt, inputs)
    inputs.mkdir(parents=True)
    review.git("worktree", "add", "--detach", str(wt), head)
    cid = store.start_call(0, kind, BRAIN_MODEL, head, slugs)
    try:
        mtimes, prs = _prepare(group, head, inputs, store, verify)
        what = (VERIFY_WHAT if verify else DIFF_WHAT).format(head=head)
        stdout, rc = _claude(PROMPT.format(inputs=inputs, what=what, head=head), wt, [inputs])
        review.keep_run(f"brain-{head[:7]}", stdout)
        try:
            out = json.loads(stdout)
        except (json.JSONDecodeError, TypeError):
            store.end_call(cid, f"exit {rc}, no JSON")
            return _failed(store, group, f"exit {rc}, no JSON: {(stdout or '')[:150]}")
        usage = review.usage_of(out)
        if out.get("is_error") or out.get("subtype") != "success":
            store.end_call(cid, f"claude {out.get('subtype')}", usage)
            return _failed(store, group, f"claude {out.get('subtype')}: {str(out.get('result'))[:150]}")
        result, why = validate(out.get("structured_output"))
        if result is None:
            store.end_call(cid, f"invalid output: {why}", usage)
            return _failed(store, group, f"invalid output: {why}")
        store.end_call(cid, f"{len(result['leaves'])} leaves checked", usage)
    finally:
        review.cleanup(wt, inputs)
    return _apply_result(store, vault, group, head, result, mtimes, prs, kind)


def _apply_result(store, vault, group, head, result, mtimes, prs, kind):
    now = stamp()
    by_slug = {l.slug: l for l in group}
    done = {"edited": 0, "unchanged": 0, "skipped": 0, "stuck": 0}
    for r in result["leaves"]:
        leaf = by_slug.get(r["slug"])
        if leaf is None:
            continue
        if r["verdict"] == "unchanged" or not r.get("edits"):
            store.brain_upsert(leaf.slug, sync_sha=head, synced_at=now, state="ok", tries=0, last_error="")
            done["unchanged"] += 1
            continue
        outcome, detail = brain.apply(leaf, r["edits"], mtimes[leaf.slug], prs, head,
                                      review.HERE / "brain-backups", now[:10])
        done[outcome] += 1
        if outcome == "edited":
            store.brain_upsert(leaf.slug, sync_sha=head, synced_at=now, state="ok", tries=0, last_error="")
            brain.log(vault, f"{now} [[{leaf.slug}]] edited ({len(r['edits'])} edits) from "
                             f"{', '.join(f'#{p}' for p in prs) or 'master'} at {head[:7]} ({kind}): "
                             f"{brain.plain(r.get('why', ''))}; {detail}")
        elif outcome == "stuck":
            store.brain_upsert(leaf.slug, state="stuck", last_error=detail[:300])
            brain.log(vault, f"{now} [[{leaf.slug}]] NOT edited, needs a person: {detail}")
    answered = {r["slug"] for r in result["leaves"]}
    for leaf in group:  # a leaf the model left out stays due and counts a try
        if leaf.slug not in answered:
            _failed(store, [leaf], "left out of the result")
    for s in result.get("suggestions") or []:
        brain.log(vault, f"{now} suggestion ({s.get('kind')}) [[{s.get('slug', '')}]]: {brain.plain(s.get('text', ''))}")
    return ", ".join(f"{v} {k}" for k, v in done.items() if v) or "nothing to apply"
