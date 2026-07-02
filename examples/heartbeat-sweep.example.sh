#!/bin/sh
# heartbeat-sweep.example.sh — reference wiring for a city's tending loop.
#
# Copy to <city>/scripts/bartertown-sweep.sh (or fold the body into an existing
# tending script) and schedule it — an orders/*.toml cooldown entry, cron, or
# your tending gate. Suggested cadence: 15m. Cost when armed: one git pull.
#
# TWO-KEY DEFAULT-DENY — the script no-ops unless BOTH markers exist:
#   <city>/.gc/bartertown.enabled         pack master switch (reviewed enable)
#   <city>/.gc/bartertown-sweep.enabled   this wiring; the city's operator arms
#                                         it:  touch .gc/bartertown-sweep.enabled
# Reversible: rm .gc/bartertown-sweep.enabled (or delete the schedule entry).
#
# The sweep is DETECT-ONLY: it pulls from the hub (the reliable sync carrier —
# cross-city posts arrive even when no local agent writes) and prints the
# digest (new items + aging + expertise matches, inside the untrusted-content
# envelope). It advances no cursors and never posts; consuming (--consume-as)
# and replying stay on the consuming agent's own read path.
set -u
CITY="${BARTERTOWN_SWEEP_CITY:-$(pwd)}"
GC_BIN="${BARTERTOWN_SWEEP_GC:-gc}"
cd "$CITY" || exit 0
[ -f "$CITY/.gc/bartertown.enabled" ] || { echo "bartertown-sweep: pack not enabled — no-op"; exit 0; }
[ -f "$CITY/.gc/bartertown-sweep.enabled" ] || { echo "bartertown-sweep: wiring not armed (operator: touch .gc/bartertown-sweep.enabled) — no-op"; exit 0; }
BARTERTOWN_CITY_ROOT="$CITY" timeout 90 "$GC_BIN" bartertown sweep --agent mayor || true
