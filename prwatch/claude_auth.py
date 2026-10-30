"""The Claude token for model calls, with a second plan's token as fallback.

Slots, in order:
  primary  CLAUDE_CODE_OAUTH_TOKEN     a `claude setup-token` token (about a year), in claude.env
  second   PRWATCH_SECOND_OAUTH_TOKEN  another plan's setup-token, in claude2.env (optional)
  desktop  the host's desktop Claude Code login, ~/.claude/.credentials.json mounted READ-ONLY: about 8 hours,
           renewed only by a `claude` run on the laptop, so it holds calls overnight.
A slot whose plan hit its usage limit is skipped until the limit resets (state/token_limits.json), so a run
moves to the next slot and comes back on its own. Nothing here logs or returns token text except pick().
"""

import datetime
import json
import os
import re
import threading
from pathlib import Path

CREDS = Path(os.environ.get("HOST_CLAUDE_CREDENTIALS", "/host-claude/credentials.json"))
LIMITS = Path(os.environ.get("INKHUB_DIR", "/inkhub")) / "prwatch" / "state" / "token_limits.json"
MIN_VALID_MIN = 35  # a review may run 30 min; never start one on a desktop token about to expire
SLOTS = (("primary", "CLAUDE_CODE_OAUTH_TOKEN"), ("second", "PRWATCH_SECOND_OAUTH_TOKEN"))
# A plan's usage limit as claude reports it; a limit without a reset time is retried after this long.
LIMIT_TEXT = re.compile(r"usage limit|limit reached|rate[ _-]?limit|too many requests|\b429\b|"
                        r"out of (extra )?usage|session limit|weekly limit", re.I)
LIMIT_BACKOFF_MIN = int(os.environ.get("PRWATCH_LIMIT_BACKOFF_MIN", "60") or 60)
_LOCK = threading.Lock()  # worker threads mark limits concurrently


def _now():
    return datetime.datetime.now(datetime.timezone.utc)


def _oauth():
    try:
        return json.loads(CREDS.read_text(encoding="utf-8")).get("claudeAiOauth") or {}
    except (OSError, ValueError):
        return {}


def _limits():
    try:
        return json.loads(LIMITS.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def limited_until(name):
    """The time slot `name` is skipped until, or None when it is usable."""
    until = _limits().get(name)
    if not until:
        return None
    at = datetime.datetime.fromisoformat(until)
    return at if at > _now() else None


def mark_limited(name, until):
    """Skip slot `name` until `until` (its plan hit the usage limit)."""
    with _LOCK:
        limits = _limits()
        limits[name] = until.isoformat()
        LIMITS.parent.mkdir(parents=True, exist_ok=True)
        tmp = LIMITS.with_suffix(".tmp")
        tmp.write_text(json.dumps(limits), encoding="utf-8")
        os.replace(tmp, LIMITS)


def limit_reset(text):
    """When a limit reported in `text` resets: the epoch claude appends (`...|1730312345`), else a backoff."""
    m = re.search(r"\|(\d{10})\b", text or "")
    if m:
        return datetime.datetime.fromtimestamp(int(m.group(1)), datetime.timezone.utc)
    return _now() + datetime.timedelta(minutes=LIMIT_BACKOFF_MIN)


def _desktop():
    o = _oauth()
    if not o.get("accessToken"):
        return None, None
    expires = datetime.datetime.fromtimestamp(o.get("expiresAt", 0) / 1000, datetime.timezone.utc)
    if expires - _now() < datetime.timedelta(minutes=MIN_VALID_MIN):
        return None, expires
    return o["accessToken"], expires


def pick():
    """(slot name, token) for the next call: the first configured slot not at its usage limit, then the desktop
    login; (None, None) when nothing is usable."""
    for name, var in SLOTS:
        if os.environ.get(var) and not limited_until(name):
            return name, os.environ[var]
    tok, _ = _desktop()
    return ("desktop", tok) if tok else (None, None)


def state():
    """(ready, human reason). Never includes a token."""
    name, _ = pick()
    held = [f"{n} limited until {limited_until(n):%H:%MZ}" for n, var in SLOTS
            if os.environ.get(var) and limited_until(n)]
    note = f" ({', '.join(held)})" if held else ""
    if name == "primary":
        return True, "setup-token from claude.env"
    if name == "second":
        return True, f"second token from claude2.env{note}"
    if name == "desktop":
        _, expires = _desktop()
        return True, f"desktop login fallback, valid until {expires:%H:%MZ}{note}; run scripts/set-token.ps1"
    if held:
        return False, f"every token at its usage limit{note}"
    _, expires = _desktop()
    if expires:
        return False, (f"no claude.env token, desktop login expires {expires:%H:%MZ}; held until a `claude` run "
                       "renews it, or run scripts/set-token.ps1")
    return False, f"no claude.env token and no desktop login at {CREDS}; run scripts/set-token.ps1"


def token():
    """The token for one call, or None when nothing is usable."""
    return pick()[1]
