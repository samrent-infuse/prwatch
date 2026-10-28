"""The brain refresh: what needs git or claude. Sync points, the master scan, anchors, the refresh call
and the backfill. Every write to the vault goes through brain.apply."""

import json
import os
import re
import shutil
import subprocess

from prwatch import prompts
from prwatch import brain
from prwatch import review
from prwatch.store import later, stamp

IGNORED = ("tests/", "docs/")
BRAIN_MODEL = os.environ.get("PRWATCH_BRAIN_MODEL", "sonnet")
BRAIN_MAX_RUN_USD = float(os.environ.get("PRWATCH_BRAIN_MAX_RUN_USD", "2") or 0)
DIFF_MAX_LINES = 3000
RETRY_MIN = (30, 90)  # after the 3rd failed call the leaves are stuck
# "propose" (default): a drifted leaf gets status stale and its proposed edits go to the refresh log for a
# session to apply; "apply": code applies the edits in place (with backup and undo).
BRAIN_APPLY = os.environ.get("PRWATCH_BRAIN_APPLY", "propose").strip()

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

PROMPT = prompts.load("brain-refresh")

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
    sync point is re-anchored from the leaf's as_of. A leaf with no row yet is left to the backfill, which
    sets its first sync point under a budget (scanning it here would make the loop an unbudgeted backfill)."""
    newly = []
    for leaf in index:
        if leaf.folder != "reference" or not leaf.repo_paths:
            continue
        row = store.brain_row(leaf.slug)
        if row is None or row["state"] == "stuck":
            continue
        sync = row["sync_sha"]
        changed = changed_named_files(sync, head, leaf.repo_paths)
        if changed is None:
            sync = anchor(leaf.as_of)
            changed = changed_named_files(sync, head, leaf.repo_paths)
            if changed is None:
                continue  # undated, or older than the clone: the backfill's verify pass
            store.brain_upsert(leaf.slug, path=f"{leaf.folder}/{leaf.path.name}", sync_sha=sync)
        if changed:
            already = row["state"] == "due"
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
    wt, inputs = review.WORK / f"{kind}-wt-{head[:7]}", review.WORK / f"{kind}-in-{head[:7]}"  # backfill may run beside the loop
    review.cleanup(wt, inputs)
    inputs.mkdir(parents=True)
    review.git("worktree", "add", "--detach", str(wt), head)
    cid = store.start_call(0, kind, BRAIN_MODEL, head, slugs)
    try:
        try:
            mtimes, prs = _prepare(group, head, inputs, store, verify)
        except Exception as exc:  # git or file trouble: close the ledger row and count a try
            store.end_call(cid, f"prepare failed: {exc}")
            return _failed(store, group, f"prepare failed: {exc}")
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
    done = {"edited": 0, "flagged": 0, "unchanged": 0, "skipped": 0, "stuck": 0}
    for r in result["leaves"]:
        r["slug"] = r["slug"].strip().removesuffix(".md").split("/")[-1]  # the model may answer "reference/<slug>"
        leaf = by_slug.get(r["slug"])
        if leaf is None:
            continue
        if r["verdict"] == "unchanged" or not r.get("edits"):
            store.brain_upsert(leaf.slug, sync_sha=head, synced_at=now, state="ok", tries=0, last_error="")
            done["unchanged"] += 1
            continue
        outcome, detail = brain.apply(leaf, r["edits"], mtimes[leaf.slug], prs, head,
                                      review.STATE / "brain-backups", now[:10],
                                      mode="apply" if BRAIN_APPLY == "apply" else "stale")
        done[outcome] += 1
        if outcome == "flagged":
            store.brain_upsert(leaf.slug, sync_sha=head, synced_at=now, state="ok", tries=0, last_error="")
            brain.log(vault, f"{now} [[{leaf.slug}]] proposed ({len(r['edits'])} edits, leaf flagged stale) from "
                             f"{', '.join(f'#{p}' for p in prs) or 'master'} at {head[:7]} ({kind}): "
                             f"{brain.plain(r.get('why', ''))}\n{brain.diff_block(r['edits'])}")
        elif outcome == "edited":
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


EST_DEFAULT_USD = 0.45  # per call, until the ledger holds ten brain calls


def _est_per_call(store):
    costs = [r[0] for r in store.db.execute(
        "SELECT cost_usd FROM calls WHERE kind LIKE 'brain_%' AND cost_usd IS NOT NULL").fetchall()]
    return sum(costs) / len(costs) if len(costs) >= 10 else EST_DEFAULT_USD


def _batches(rows, leaves, action):
    """Rows of one action grouped by shared top-level package, up to 6 per batch, most lines first."""
    pending = sorted((r for r in rows if r["action"] == action), key=lambda r: -r["lines"])
    out = []
    while pending:
        first = pending.pop(0)
        pk = {p.split("/", 1)[0] for p in leaves[first["slug"]].repo_paths}
        batch = [first] + [r for r in pending if {p.split("/", 1)[0] for p in leaves[r["slug"]].repo_paths} & pk][:5]
        pending = [r for r in pending if r not in batch]
        out.append(batch)
    return out


def backfill(store, vault, head, dry_run, budget_usd, only):
    """Bring every reference leaf that names repo files up to `head`. Returns one row per leaf: slug, as_of,
    anchor, files, lines, batch, action (sync only | refresh | verify | not anchored to code), est_usd.
    A dry run writes nothing. A real run records the free sync points first, then runs the refresh batches
    (most changed lines first) and the verify batches, and stops once this run's spend reaches budget_usd."""
    index = [l for l in brain.build_index(vault) if l.folder == "reference" and (not only or l.slug in only)]
    leaves = {l.slug: l for l in index}
    rows = []
    for leaf in index:
        row = {"slug": leaf.slug, "as_of": leaf.as_of, "anchor": "", "files": 0, "lines": 0, "batch": 0,
               "est_usd": 0.0}
        if not leaf.repo_paths:
            rows.append({**row, "action": "not anchored to code"})
            continue
        known = store.brain_row(leaf.slug)
        sync = known["sync_sha"] if known is not None and _is_commit(known["sync_sha"]) else anchor(leaf.as_of)
        changed = changed_named_files(sync, head, leaf.repo_paths) if sync else None
        if changed is None:
            rows.append({**row, "action": "verify"})
        elif not changed:
            rows.append({**row, "anchor": sync, "action": "sync only"})
        else:
            stat = review.git("diff", "--numstat", sync, head, "--", *leaf.repo_paths).splitlines()
            lines = sum(int(a) + int(d) for a, d, *_ in (s.split("\t") for s in stat) if a.isdigit() and d.isdigit())
            rows.append({**row, "anchor": sync, "files": len(changed), "lines": lines, "action": "refresh"})
    per_call = _est_per_call(store)
    batches = _batches(rows, leaves, "refresh") + _batches(rows, leaves, "verify")
    for i, batch in enumerate(batches, 1):
        for r in batch:
            r["batch"], r["est_usd"] = i, per_call / len(batch)
    _print(rows, batches)
    if dry_run:
        return rows
    now = stamp()
    for r in rows:
        if r["action"] == "sync only":
            store.brain_upsert(r["slug"], path=f"reference/{r['slug']}.md", sync_sha=head, synced_at=now, state="ok")
        elif r["action"] == "refresh":
            store.brain_upsert(r["slug"], path=f"reference/{r['slug']}.md", sync_sha=r["anchor"], state="due",
                               touched_at=now)
    start = store.db.execute("SELECT COALESCE(MAX(id), 0) FROM calls").fetchone()[0]
    for batch in batches:
        spent = store.db.execute("SELECT COALESCE(SUM(cost_usd), 0) FROM calls WHERE kind='brain_backfill' AND id>?",
                                 (start,)).fetchone()[0]
        if spent >= budget_usd:
            print(f"budget reached: ${spent:.2f} of ${budget_usd:.2f}; run again to continue")
            break
        group = [leaves[r["slug"]] for r in batch]
        outcome = refresh(store, vault, group, head, kind="brain_backfill", verify=batch[0]["action"] == "verify")
        print(f"batch {batch[0]['batch']} ({', '.join(l.slug for l in group)}): {outcome}")
    return rows


def _print(rows, batches):
    print(f"{'leaf':40} {'as_of':10} {'anchor':7} {'files':>5} {'lines':>6} {'batch':>5} {'est $':>6}  action")
    for r in sorted(rows, key=lambda r: (r["batch"] or 999, -r["lines"])):
        print(f"{r['slug'][:40]:40} {r['as_of'][:10]:10} {r['anchor'][:7]:7} {r['files']:>5} {r['lines']:>6} "
              f"{r['batch'] or '':>5} {r['est_usd']:>6.2f}  {r['action']}")
    print(f"{len(batches)} calls, about ${sum(r['est_usd'] for r in rows):.2f} at list price; "
          f"{sum(r['action'] == 'sync only' for r in rows)} leaves sync only (free)")
