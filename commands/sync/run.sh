#!/bin/sh
set -eu
if [ -z "${GC_CITY_PATH:-}" ] || [ -z "${GC_PACK_DIR:-}" ]; then
  echo "gc bartertown sync: missing Gas City pack context" >&2
  exit 1
fi
BARTERTOWN_CITY_ROOT="${BARTERTOWN_CITY_ROOT:-$GC_CITY_PATH}" \
  exec python3 "$GC_PACK_DIR/scripts/bartertown_admin.py" sync "$@"
