#!/bin/bash
# Regenerate the FirePlanner page from live IBKR data, and optionally push it to
# a web host. Run by launchd after the close; safe to run by hand any time.
#
# Deliberately defensive, because this runs unattended: it refuses to overwrite a
# good build with a broken one, it refuses to upload account figures to a public
# host by accident, and it never exits non-zero for a reason that is not
# actionable (a closed market, a sleeping gateway) — launchd would otherwise
# retry pointlessly.
#
# Configuration lives in fireplanner.env beside this script (see
# fireplanner.env.example). Editing that file takes effect on the next run; no
# need to regenerate the plist or reload launchd.

set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Sourced early so it can set anything below. Kept out of git — it holds your
# host name and paths, and it is the natural place for a deploy target.
[ -f "$HERE/fireplanner.env" ] && . "$HERE/fireplanner.env"

VENV="${FIREPLANNER_VENV:-$HERE/fireplanner-venv}"
OUT="${FIREPLANNER_OUT:-$HERE/fireplanner_site}"
LOG="${FIREPLANNER_LOG:-$HERE/fireplanner.log}"
IB_PORT="${IB_PORT:-4001}"     # 4001 Gateway live · 4002 Gateway paper · 7496/7497 TWS
IB_HOST="${IB_HOST:-127.0.0.1}"
CORE="${FIREPLANNER_CORE:-0.40}"
SYMBOLS="${FIREPLANNER_SYMBOLS:-SPY}"
SINGLE="${FIREPLANNER_SINGLE:-1}"          # 1 = one index.html, 0 = three linked files
RSYNC_TARGET="${FIREPLANNER_RSYNC_TARGET:-}"   # e.g. me@host:/var/www/fireplanner/
PUBLISH_CMD="${FIREPLANNER_PUBLISH_CMD:-}"     # anything else; receives $OUT as $1

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

# ---------------------------------------------------------------- redaction
# The pages carry your net liquidation, your positions and your trade sizes.
# That is fine on this Mac and unrecoverable on a domain, so anything with a
# deploy target is redacted unless you have explicitly said otherwise. The
# default is chosen by whether this build leaves the machine, not by taste.
if [ -n "$RSYNC_TARGET" ] || [ -n "$PUBLISH_CMD" ]; then
  REDACT="${FIREPLANNER_REDACT:-1}"
  if [ "$REDACT" != "1" ]; then
    log "WARNING: uploading UNREDACTED pages — balances, positions and trade sizes will be public"
  fi
else
  REDACT="${FIREPLANNER_REDACT:-0}"
fi

ARGS=(--provider gateway --host "$IB_HOST" --port "$IB_PORT" --cache
      publish $SYMBOLS --core "$CORE")
[ "$SINGLE" = "1" ] && ARGS+=(--single)
[ "$REDACT" = "1" ] && ARGS+=(--redact)

# Build into a staging directory, and only swap it in if it succeeded. A half
# written page is worse than yesterday's complete one.
STAGE="$(mktemp -d "${TMPDIR:-/tmp}/fireplanner.XXXXXX")"
trap 'rm -rf "$STAGE"' EXIT

if ! "$VENV/bin/fireplanner" "${ARGS[@]}" -o "$STAGE" >> "$LOG" 2>&1; then
  log "FAILED: keeping the previous build — the pages will show their own staleness"
  log "----- run end -----"
  exit 0
fi

mkdir -p "$OUT"
# cp rather than mv: keeps $OUT's inode, so anything serving it is undisturbed.
cp -f "$STAGE"/*.html "$STAGE"/status.json "$OUT"/ 2>>"$LOG"
# Switching layouts leaves the old pages behind; they would keep resolving and
# keep serving the signal that was current the day you switched.
if [ "$SINGLE" = "1" ]; then
  rm -f "$OUT/signal_desk.html" "$OUT/dashboard.html"
fi
log "OK: wrote $OUT (single=$SINGLE redact=$REDACT)"

if [ -f "$OUT/status.json" ]; then
  /usr/bin/python3 -c "
import json,sys
d=json.load(open('$OUT/status.json'))
dec=d.get('decision',{})
print('  signal: %s  target %s%%  as of %s' % (
    dec.get('action'), int(float(dec.get('target_pct') or 0)*100), d.get('as_of')))
" >> "$LOG" 2>/dev/null
fi

# ---------------------------------------------------------------- upload
# Only ever runs against a build that already succeeded, so a bad build cannot
# replace a good page on the host.
if [ -n "$RSYNC_TARGET" ]; then
  # --delete so a file dropped from the build is dropped from the host too.
  # BatchMode: a launchd job has no terminal, so a key that wants a passphrase
  # must fail fast rather than hang until the next run stacks up behind it.
  if rsync -az --delete -e "ssh -o BatchMode=yes -o ConnectTimeout=10" \
        "$OUT"/ "$RSYNC_TARGET" >> "$LOG" 2>&1; then
    log "UPLOADED: $RSYNC_TARGET"
  else
    log "UPLOAD FAILED: $RSYNC_TARGET — the local build in $OUT is still good"
  fi
fi

if [ -n "$PUBLISH_CMD" ]; then
  if bash -c "$PUBLISH_CMD" _ "$OUT" >> "$LOG" 2>&1; then
    log "PUBLISHED via FIREPLANNER_PUBLISH_CMD"
  else
    log "PUBLISH COMMAND FAILED — the local build in $OUT is still good"
  fi
fi

log "----- run end -----"
exit 0
