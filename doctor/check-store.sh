#!/bin/sh
# Informational: report gate + forum-clone state (a not-yet-initialized
# bartertown is a valid default-off state).
set -eu
CITY="${GC_CITY_PATH:-$PWD}"
MARKER="$CITY/.gc/bartertown.enabled"
REPO="$CITY/.gc/services/bartertown/repo"
if [ ! -f "$MARKER" ]; then
  echo "bartertown: disabled (default) — marker $MARKER absent"
  exit 0
fi
if [ ! -f "$REPO/bartertown.toml" ]; then
  echo "bartertown: ENABLED but forum clone missing ($REPO) — run 'gc bartertown init' or 'gc bartertown join'"
  exit 2
fi
echo "bartertown: enabled, forum clone present"
