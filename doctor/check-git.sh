#!/bin/sh
# Bartertown's storage is a plain git repo (spec §13); git is the only
# non-stdlib dependency.
set -eu
if ! command -v git >/dev/null 2>&1; then
  echo "git not found — bartertown storage/sync needs git 2.28+"
  exit 2
fi
if ! git version | grep -qE "git version ([3-9]|2\.(2[8-9]|[3-9][0-9]))"; then
  echo "git too old ($(git version)) — need 2.28+ (init -b main)"
  exit 2
fi
