"""The project brain (an Obsidian vault in the _CONVENTIONS format) as prwatch reads and edits it.

Nothing here runs git or claude: the leaf index, matching leaves to a PR's files, copying them into a
review's inputs, applying a refresh's edits with guards, backups, the refresh log and undo.
"""

import datetime
import json
import os
import re
import shutil
from dataclasses import asdict, dataclass, field
from pathlib import Path

FOLDERS = ("reference", "gotchas", "decisions", "rejected", "investigations")
TYPE_ORDER = ("gotcha", "decision", "rejected", "reference", "investigation")
REPO_PATH = re.compile(r"(?:inkhub_[a-z_]+|api|deploy|scripts|tests)/[A-Za-z0-9_./-]+\.(?:py|ts|tsx|sql|tf|yml|yaml|sh)")


def log_event(msg):
    """Where brain problems are reported; main.py points this at its own log."""
    print(f"brain: {msg}", flush=True)


@dataclass
class Leaf:
    folder: str
    slug: str
    path: Path
    type: str = ""
    title: str = ""
    status: str = ""
    as_of: str = ""
    tags: list = field(default_factory=list)
    repo_paths: list = field(default_factory=list)


def parse_frontmatter(text):
    """The block between the first two `---` lines as a dict; `[a, b]` values become lists."""
    lines = text.replace("\r\n", "\n").split("\n")
    if not lines or lines[0].strip() != "---":
        return {}
    out = {}
    for line in lines[1:]:
        if line.strip() == "---":
            return out
        m = re.match(r"^([A-Za-z_][\w-]*):\s*(.*)$", line)
        if m:
            v = m.group(2).strip()
            if v.startswith("[") and v.endswith("]"):
                out[m.group(1)] = [x.strip().strip("\"'") for x in v[1:-1].split(",") if x.strip()]
            else:
                out[m.group(1)] = v.strip("\"'")
    return {}  # no closing ---


def parse_leaf(path, folder):
    text = path.read_bytes().decode("utf-8")
    fm = parse_frontmatter(text)
    paths = list(dict.fromkeys(REPO_PATH.findall(text)))
    return Leaf(folder, path.stem, path, fm.get("type", ""), fm.get("title", ""), fm.get("status", ""),
                fm.get("as_of", ""), fm.get("tags") or [], paths)


def _leaf_files(vault):
    return sorted(p for f in FOLDERS for p in (vault / f).glob("*.md") if p.name != "MANIFEST.md")


def build_index(vault, cache=None):
    """Every usable leaf of FOLDERS. Superseded and unreadable leaves are left out (unreadable ones
    reported). With `cache`, the index is reused until the leaf count or the newest mtime changes."""
    vault = Path(vault)
    files = _leaf_files(vault)
    key = [len(files), max((p.stat().st_mtime for p in files), default=0)]
    if cache and Path(cache).exists():
        try:
            data = json.loads(Path(cache).read_text(encoding="utf-8"))
            if data["key"] == key:
                return [Leaf(**{**d, "path": Path(d["path"])}) for d in data["leaves"]]
        except (ValueError, KeyError, TypeError):
            pass
    leaves = []
    for p in files:
        try:
            leaf = parse_leaf(p, p.parent.name)
        except (OSError, UnicodeDecodeError) as exc:
            log_event(f"unreadable leaf {p.parent.name}/{p.name} skipped: {exc}")
            continue
        if leaf.status != "superseded":
            leaves.append(leaf)
    if cache:
        Path(cache).write_text(json.dumps({"key": key, "leaves": [{**asdict(l), "path": str(l.path)} for l in leaves]}),
                               encoding="utf-8")
    return leaves


def _score(leaf, changed):
    """(score, reason) of a leaf against the changed files: 3 exact file, 2 same directory, 1 same
    top-level package; investigations only count on an exact file."""
    best = (0, "")
    for c in changed:
        for p in leaf.repo_paths:
            if p == c:
                s = (3, f"names {c}")
            elif p.rsplit("/", 1)[0] == c.rsplit("/", 1)[0]:
                s = (2, f"names {p}, same directory as {c}")
            elif p.split("/", 1)[0] == c.split("/", 1)[0] and "/" in c:
                s = (1, f"names {p}, same package as {c}")
            else:
                continue
            best = max(best, s, key=lambda x: x[0])
    if leaf.type == "investigation" and best[0] < 3:
        return 0, ""
    return best


def match(index, changed, limit):
    """[(leaf, reason)] best first: score, then TYPE_ORDER, then newer as_of."""
    scored = [(l, *_score(l, changed)) for l in index]
    scored = [x for x in scored if x[1] > 0]
    order = {t: i for i, t in enumerate(TYPE_ORDER)}
    scored.sort(key=lambda x: x[0].as_of, reverse=True)  # newer as_of first; the next sort is stable
    scored.sort(key=lambda x: (-x[1], order.get(x[0].type, len(order))))
    return [(l, why) for l, _, why in scored[:limit]]


def ground(vault, inputs, changed, limit, cache=None):
    """Copy the leaves that match the changed files into inputs/brain/<folder>/ and list them in
    inputs/brain.md. Returns how many; 0 (and nothing written) without a vault or a match."""
    vault = Path(vault)
    if not vault.is_dir():
        return 0
    picked = match(build_index(vault, cache), changed, limit)
    if not picked:
        return 0
    lines = []
    for leaf, why in picked:
        dest = Path(inputs) / "brain" / leaf.folder / leaf.path.name
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(leaf.path, dest)
        lines.append(f"- `{leaf.folder}/{leaf.slug}` {leaf.type}, as_of {leaf.as_of or 'undated'}, "
                     f"{leaf.status or 'no status'}: {why}")
    (Path(inputs) / "brain.md").write_text(
        "Vault leaves about the files this PR changes (copies under brain/; the vault itself is readable too).\n\n"
        + "\n".join(lines) + "\n", encoding="utf-8")
    return len(picked)


SHRINK_LIMIT = 0.6  # an edited leaf keeps at least 60% of its length
LOG_LEAF = Path("handoff") / "brain-refresh-log.md"


def _set_frontmatter(text, as_of, suffix):
    """`as_of` replaced and `suffix` appended to `source` (added when missing), in the frontmatter only."""
    head, sep, rest = text.partition("\n---\n")
    lines = head.split("\n")
    out, has_source = [], False
    for line in lines:
        if line.startswith("as_of:"):
            line = f"as_of: {as_of}"
        elif line.startswith("source:"):
            has_source = True
            v = line[len("source:"):].strip()
            line = f'source: "{v[1:-1]}{suffix}"' if v[:1] == v[-1:] == '"' and len(v) > 1 else f"source: {v}{suffix}"
        out.append(line)
    if not has_source:
        out.append(f'source: "{suffix.lstrip("; ")}"')
    return "\n".join(out) + sep + rest


def apply(leaf, edits, read_mtime, prs, sha, backups, today):
    """Apply a refresh's edits to one leaf. ("edited" | "skipped" | "stuck", why). Skipped: the file changed
    since it was read (left due). Stuck: an edit does not match exactly once, or a guard fails (frontmatter
    lost its type or title, or the leaf shrank past SHRINK_LIMIT). The previous version is backed up first;
    the write is atomic and keeps the file's line endings."""
    path = Path(leaf.path)
    if path.stat().st_mtime != read_mtime:
        return "skipped", "changed on disk since it was read"
    raw = path.read_bytes()
    crlf = b"\r\n" in raw
    old_text = text = raw.decode("utf-8").replace("\r\n", "\n")
    for e in edits:
        old, new = e["old"].replace("\r\n", "\n"), e["new"].replace("\r\n", "\n")
        n = text.count(old) if old else 0
        if n != 1:
            return "stuck", f"edit matches {n} times: {old[:60]!r}"
        text = text.replace(old, new)
    prs_text = ", ".join(f"#{p}" for p in prs) or "master"
    text = _set_frontmatter(text, today, f"; refreshed from {prs_text} at {sha[:7]} by prwatch")
    fm_old, fm_new = parse_frontmatter(old_text), parse_frontmatter(text)
    if not fm_new.get("title") or fm_new.get("type") != fm_old.get("type"):
        return "stuck", "frontmatter lost its type or title"
    if len(text) < SHRINK_LIMIT * len(old_text):
        return "stuck", f"leaf would shrink from {len(old_text)} to {len(text)} characters"
    backup = Path(backups) / today / leaf.folder / path.name
    backup.parent.mkdir(parents=True, exist_ok=True)
    backup.write_bytes(raw)
    tmp = path.with_suffix(".md.prwatch-tmp")
    tmp.write_bytes((text.replace("\n", "\r\n") if crlf else text).encode("utf-8"))
    os.replace(tmp, path)
    return "edited", f"{len(edits)} edits, backup {backup}"


def log(vault, line):
    """Append `- line` to the refresh log leaf, creating it (and its handoff MANIFEST line) the first time."""
    vault = Path(vault)
    p = vault / LOG_LEAF
    if not p.exists():
        today = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
        p.write_text(f'---\ntype: handoff\ntitle: "prwatch brain refresh log"\nstatus: active\nas_of: {today}\n'
                     "source: \"written by prwatch, one line per reference leaf it refreshed or suggestion it made\"\n"
                     "tags: [brain, automation]\nlinks: [2026-10-28-prwatch-brain-grounding-and-refresh-design]\n---\n\n"
                     "# prwatch brain refresh log\n\nBackups for undo: `python3 main.py --brain-undo <folder/slug>`.\n\n",
                     encoding="utf-8")
        manifest = vault / "handoff" / "MANIFEST.md"
        if manifest.exists():
            m = manifest.read_bytes().decode("utf-8")
            nl = "\r\n" if "\r\n" in m else "\n"
            entry = ("- [[brain-refresh-log]]: prwatch's log of the reference leaves it refreshed after merges, "
                     "with their backups for undo, and its suggestions.")
            manifest.write_bytes((m.rstrip("\r\n") + nl + entry + nl).encode("utf-8"))
    with p.open("a", encoding="utf-8", newline="") as fh:
        fh.write(f"- {line}\n")


def log_counts(vault, day):
    """{"edited": n, "suggestions": n} from the refresh log's lines of `day` (YYYY-MM-DD)."""
    p = Path(vault) / LOG_LEAF
    lines = p.read_text(encoding="utf-8").splitlines() if p.exists() else []
    today = [l for l in lines if l.startswith(f"- {day}")]
    return {"edited": sum("]] edited (" in l for l in today), "suggestions": sum(" suggestion (" in l for l in today)}


def undo(vault, backups, folder_slug, date=None):
    """Put back a leaf's backup (`date`'s, else the newest). Returns the backup used."""
    folder, slug = folder_slug.split("/", 1)
    found = sorted(p for p in Path(backups).glob(f"*/{folder}/{slug}.md") if date is None or p.parts[-3] == date)
    if not found:
        raise FileNotFoundError(f"no backup of {folder_slug}" + (f" from {date}" if date else ""))
    shutil.copyfile(found[-1], Path(vault) / folder / f"{slug}.md")
    return found[-1]


def plain(s):
    """Text written into the vault: no em or en dashes, one line."""
    return re.sub(r"\s*[—–]\s*", ", ", " ".join(str(s or "").split()))


def _packages(leaf):
    return {p.split("/", 1)[0] for p in leaf.repo_paths}


def pick_group(rows, index, now, settle_min, max_leaves=6):
    """The next leaves to refresh together: due rows untouched for `settle_min` minutes and past their
    retry time, the oldest first, then others sharing one of its top-level packages, at most `max_leaves`."""
    cutoff = (now - datetime.timedelta(minutes=settle_min)).strftime("%Y-%m-%dT%H:%M:%SZ")
    stamp_now = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    leaves = {l.slug: l for l in index}
    ready = sorted((r for r in rows if r["state"] == "due" and r["touched_at"] <= cutoff
                    and (not r["next_try_at"] or r["next_try_at"] <= stamp_now) and r["slug"] in leaves),
                   key=lambda r: r["touched_at"])
    if not ready:
        return []
    first = leaves[ready[0]["slug"]]
    group = [first] + [leaves[r["slug"]] for r in ready[1:] if _packages(leaves[r["slug"]]) & _packages(first)]
    return group[:max_leaves]
