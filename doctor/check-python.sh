#!/bin/sh
set -eu
if ! command -v python3 >/dev/null 2>&1; then
  echo "python3 not found (bartertown MCP server + commands need Python 3.11+)"
  exit 2
fi
python3 - <<'PY'
import sys
if sys.version_info < (3, 11):
    print(f"python3 is {sys.version.split()[0]}; bartertown needs 3.11+")
    raise SystemExit(2)
PY
