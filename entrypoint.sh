#!/bin/sh
# Container start: git over gh credentials, own clone in the repo volume, then the loop.
set -e
: "${PRWATCH_REPO:?set PRWATCH_REPO=owner/name in prwatch.env}"
gh auth setup-git >/dev/null
if [ ! -d /repo/.git ]; then
  git clone --quiet --filter=blob:none https://github.com/$PRWATCH_REPO.git /repo
fi
# Claude Code's first-run onboarding would block `claude -p`; mark it done in the container's own home.
[ -f "$HOME/.claude.json" ] || echo '{"hasCompletedOnboarding": true}' > "$HOME/.claude.json"
exec python3 -u /app/main.py "$@"
