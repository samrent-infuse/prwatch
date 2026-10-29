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
        "strengths": {"type": "array", "items": {"type": "string"}},
        "not_judged": {"type": "array", "items": {"type": "string"}},
        "earlier": {"type": "array", "items": {  # re-reviews: the earlier critical and important findings
            "type": "object",
            "properties": {
                "finding": {"type": "string"},
                "status": {"type": "string", "enum": ["fixed", "still present", "partly fixed", "no longer applies"]},
                "checked_at": {"type": "string"},
            },
            "required": ["finding", "status"],
        }},
    },
    "required": ["verdict", "summary", "findings"],
}
VERDICT_LINE = {"APPROVE": "approve", "REQUEST_CHANGES": "changes requested", "COMMENT": "comment"}


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


def decide_event(v, pr, me_logins, allow_approve, allow_request_changes=False):
    """(event, withheld_reason). The model's own verdict word is not consulted: only the counted findings
    decide. REQUEST_CHANGES on any critical finding (not on my PR or a draft, and only when enabled). With
    no critical finding, APPROVE (important findings are approved "with fixes", Samuel 2026-10-29) when the
    PR is ready, unheld, touches no infra, is not mine and its CI has cleared; WAIT while that CI is still
    running, COMMENT when it failed."""
    if pr["merged_at_head"]:
        return "COMMENT", "merged before the review finished"
    c = counts(v)
    if c["critical"]:
        if pr["author"] in me_logins:
            return "COMMENT", "own PR"
        if pr["draft"]:
            return "COMMENT", "draft"
        if not allow_request_changes:
            return "COMMENT", "request changes not enabled (PRWATCH_ALLOW_REQUEST_CHANGES)"
        return "REQUEST_CHANGES", ""
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
    ci, why = pr.get("ci", ("cleared", ""))
    if ci == "pending":
        return "WAIT", f"CI {why}"
    if ci == "failed":
        return "COMMENT", f"CI {why}"
    return "APPROVE", ""


def plain(s):
    return re.sub(r"\s*[—–]\s*", ", ", s or "").strip()


def marker(key):
    return f"<!-- prwatch {key} -->"


def verdict_line(event, v):
    """The words after "Verdict:"; an approval with important findings says they are to be settled first."""
    line = VERDICT_LINE.get(event, event.lower())
    n = counts(v)["important"]
    if event == "APPROVE" and n:
        line += f", with fixes: settle the {n} important finding{'s' if n > 1 else ''} before merging"
    return line


def body(v, head, key, event="", moved_to=""):
    """The review as posted: heading, what was reviewed, the verdict line (when `event` is known), Strengths,
    the earlier findings' status (re-reviews), Critical, Important, Minor, Not judged, footer, hidden marker.
    Empty sections are left out. `moved_to`: the PR's head when it moved past `head` during the run."""
    rereview = v.get("kind") == "rereview"
    reviewed = f"Reviewed `{head[:7]}` against the PR description. Tests were not run."
    if moved_to:
        reviewed += f" The PR has since moved to `{moved_to[:7]}`, which is reviewed on its own."
    summary = plain(v["summary"])
    lines = ["## Automated re-review" if rereview else "## Automated code review", "", reviewed, "",
             f"**Verdict: {verdict_line(event, v)}.** {summary}" if event else summary, ""]

    def section(title, items):
        if items:
            lines.extend([f"### {title}", *items, ""])

    section("Strengths", [f"- {plain(s)}" for s in v.get("strengths") or []])
    if rereview and v.get("earlier"):
        section("Status of the earlier findings", ["| # | Finding | Status | Checked at |", "|---|---|---|---|"] + [
            f"| {i} | {plain(e.get('finding'))} | {plain(e.get('status'))} | `{plain(e.get('checked_at')) or '-'}` |"
            for i, e in enumerate(v["earlier"], 1)])
    for severity in SEVERITIES:
        items = []
        for i, f in enumerate((f for f in v["findings"] if f["severity"] == severity), 1):
            where = (f.get("file") or "") + (f":{f['line']}" if f.get("file") and f.get("line") else "")
            items.append(f"{i}. {'`' + where + '`: ' if where else ''}{plain(f['title'])}")
            if f.get("detail"):
                items.append(f"   {plain(f['detail'])}")
        section(severity.capitalize(), items)
    if not v["findings"]:
        lines += ["No findings.", ""]
    section("Not judged", [f"- {plain(s)}" for s in v.get("not_judged") or []])
    lines += [FOOTER.format(sha7=head[:7]), marker(key)]
    return "\n".join(lines) + "\n"
