"""Pure review rules: validate Claude's JSON verdict, decide APPROVE or COMMENT, render the body.

The model only returns findings; code counts them and decides.
"""

import re

HOLD_LABELS = {"hold", "do-not-merge", "wip", "blocked"}
# Paths that need a human DevOps step after merge: never approved automatically.
INFRA_PATHS = re.compile(r"^(deploy/terraform/|deploy/ansible/|\.github/workflows/)")
FOOTER = "Automated review (Claude Opus), reviewed at {sha7}."
SEVERITIES = ("critical", "important", "minor")

SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["approve", "comment"]},
        "summary": {"type": "string"},
        "findings": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "severity": {"type": "string", "enum": list(SEVERITIES)},
                "file": {"type": "string"},
                "line": {"type": "integer"},
                "title": {"type": "string"},
                "detail": {"type": "string"},
            },
            "required": ["severity", "title", "detail"],
        }},
    },
    "required": ["verdict", "summary", "findings"],
}


def validate(v):
    """Return (verdict, None) when usable, else (None, why)."""
    if not isinstance(v, dict):
        return None, "no structured output"
    if v.get("verdict") not in ("approve", "comment"):
        return None, f"bad verdict {v.get('verdict')!r}"
    if not isinstance(v.get("summary"), str) or not isinstance(v.get("findings"), list):
        return None, "summary or findings missing"
    for f in v["findings"]:
        if not isinstance(f, dict) or f.get("severity") not in SEVERITIES or not f.get("title"):
            return None, f"malformed finding {str(f)[:80]}"
    return v, None


def counts(v):
    return {s: sum(1 for f in v["findings"] if f["severity"] == s) for s in SEVERITIES}


def decide_event(v, pr, me_logins, allow_approve):
    """(event, withheld_reason). APPROVE when code counts zero critical and important findings AND
    the PR is open, ready, unheld, touches no infra and is not mine. The model's own verdict word is
    not consulted: only the counted findings decide."""
    if pr["merged_at_head"]:
        return "COMMENT", "merged before the review finished"
    c = counts(v)
    if c["critical"] or c["important"]:
        return "COMMENT", ""
    if pr["author"] in me_logins:
        return "COMMENT", "own PR"
    if pr["draft"]:
        return "COMMENT", "draft"
    if set(pr["labels"]) & HOLD_LABELS:
        return "COMMENT", "hold label"
    infra = [p for p in pr["files"] if INFRA_PATHS.match(p)]
    if infra:
        return "COMMENT", f"infra path {infra[0]}"
    if not allow_approve:
        return "COMMENT", "container approvals not enabled (PRWATCH_ALLOW_APPROVE)"
    return "APPROVE", ""


def plain(s):
    return re.sub(r"\s*[—–]\s*", ", ", s or "").strip()


def marker(key):
    return f"<!-- prwatch {key} -->"


def body(v, head, key):
    c = counts(v)
    lines = [plain(v["summary"]), ""]
    if v["findings"]:
        lines.append(f"Findings: {c['critical']} critical, {c['important']} important, {c['minor']} minor.")
        lines.append("")
        order = {s: i for i, s in enumerate(SEVERITIES)}
        for i, f in enumerate(sorted(v["findings"], key=lambda f: order[f["severity"]]), 1):
            where = f.get("file") or ""
            if where and f.get("line"):
                where += f":{f['line']}"
            lines.append(f"{i}. **{f['severity'].capitalize()}**{' `' + where + '`' if where else ''}: {plain(f['title'])}")
            if f.get("detail"):
                lines.append(f"   {plain(f['detail'])}")
        lines.append("")
    else:
        lines += ["No findings.", ""]
    lines += [FOOTER.format(sha7=head[:7]), marker(key)]
    return "\n".join(lines) + "\n"
