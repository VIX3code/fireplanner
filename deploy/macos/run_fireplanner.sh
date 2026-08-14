#!/bin/bash
# Regenerate the FirePlanner pages from live IBKR data. Run by launchd after the
# close; safe to run by hand any time.
#
# Deliberately defensive, because this runs unattended: it refuses to overwrite a
# good build with a broken one, and it never exits non-zero for a reason that is
# not actionable (a closed market, a sleeping gateway) — launchd would otherwise
# retry pointlessly.

set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="${FIREPLANNER_VENV:-$HERE/fireplanner-venv}"
OUT="${FIREPLANNER_OUT:-$HERE/fireplanner_site}"
LOG="${FIREPLANNER_LOG:-$HERE/fireplanner.log}"
IB_PORT="${IB_PORT:-4001}"     # 4001 Gateway live · 4002 Gateway paper · 7496/7497 TWS
IB_HOST="${IB_HOST:-127.0.0.1}"
CORE="${FIREPLANNER_CORE:-0.40}"
SYMBOLS="${FIREPLANNER_SYMBOLS:-SPY}"

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" >> "$LOG"; }

log "----- run start (port $IB_PORT) -----"

if [ ! -x "$VENV/bin/fireplanner" ]; then
  log "ABORT: no venv at $VENV — run install.sh first"
  exit 0
fi

# Is the gateway actually listening? Without this the failure is a 30-second
# timeout buried in a stack trace.
if ! nc -z -G 2 "$IB_HOST" "$IB_PORT" 2>/dev/null; then
  log "SKIP: nothing listening on $IB_HOST:$IB_PORT — is TWS/IB Gateway running and logged in?"
  exit 0
fi

# Build into a staging directory, and only swap it in if it succeeded. A half
# written page is worse than yesterday's complete one.
STAGE="$(mktemp -d "${TMPDIR:-/tmp}/fireplanner.XXXXXX")"
trap 'rm -rf "$STAGE"' EXIT

if "$VENV/bin/fireplanner" \
      --provider gateway --host "$IB_HOST" --port "$IB_PORT" --cache \
      publish $SYMBOLS --core "$CORE" -o "$STAGE" >> "$LOG" 2>&1
then
  mkdir -p "$OUT"
  # cp rather than mv: keeps $OUT's inode, so anything serving it is undisturbed.
  cp -f "$STAGE"/*.html "$STAGE"/status.json "$OUT"/ 2>>"$LOG"
  log "OK: wrote $OUT"
  if [ -f "$OUT/status.json" ]; then
    /usr/bin/python3 -c "
import json,sys
d=json.load(open('$OUT/status.json'))
dec=d.get('decision',{})
print('  signal: %s  target %s%%  as of %s' % (
    dec.get('action'), int(float(dec.get('target_pct') or 0)*100), d.get('as_of')))
" >> "$LOG" 2>/dev/null
  fi
else
  log "FAILED: keeping the previous build — the pages will show their own staleness"
fi

log "----- run end -----"
exit 0
