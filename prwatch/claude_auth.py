"""The Claude token for model calls.

Primary: a long-lived token from `claude setup-token`, saved by set-token.ps1 into claude.env as
CLAUDE_CODE_OAUTH_TOKEN (about a year; no refresh needed). Fallback while claude.env is missing:
the host's desktop Claude Code login, ~/.claude/.credentials.json mounted READ-ONLY. Its access token
lasts about 8 hours and only a `claude` run on the laptop renews it (the refresh token rotates; two
refreshers would sign each other out), so the fallback holds calls overnight.
Nothing here logs or returns the token text except token().
"""

import datetime
import json
import os
from pathlib import Path

CREDS = Path(os.environ.get("HOST_CLAUDE_CREDENTIALS", "/host-claude/credentials.json"))
MIN_VALID_MIN = 35  # a review may run 30 min; never start one on a token about to expire


def _oauth():
    try:
        return json.loads(CREDS.read_text(encoding="utf-8")).get("claudeAiOauth") or {}
    except (OSError, ValueError):
        return {}


def state():
    """(ready, human reason). Never includes the token."""
    if os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"):
        return True, "setup-token from claude.env"
    o = _oauth()
    if not o.get("accessToken"):
        return False, f"no claude.env token and no desktop login at {CREDS}; run set-token.ps1"
    expires = datetime.datetime.fromtimestamp(o.get("expiresAt", 0) / 1000, datetime.timezone.utc)
    left = expires - datetime.datetime.now(datetime.timezone.utc)
    if left < datetime.timedelta(minutes=MIN_VALID_MIN):
        return False, (f"no claude.env token, desktop login expires {expires:%H:%MZ}; held until a `claude` run "
                       "renews it, or run set-token.ps1")
    return True, f"desktop login fallback, valid until {expires:%H:%MZ}; run set-token.ps1"


def token():
    """The access token for one call, or None when state() is not ready."""
    if os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"):
        return os.environ["CLAUDE_CODE_OAUTH_TOKEN"]
    return _oauth().get("accessToken") if state()[0] else None
