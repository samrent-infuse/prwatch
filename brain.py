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
