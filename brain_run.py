"""The brain refresh: what needs git or claude. Sync points, the master scan, anchors, the refresh call
and the backfill. Every write to the vault goes through brain.apply."""

import subprocess

import brain
import review
from store import stamp

IGNORED = ("tests/", "docs/")


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
