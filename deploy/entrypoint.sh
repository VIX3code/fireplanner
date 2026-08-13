#!/bin/sh
# Regenerate the static site once per cycle, then sleep.
#
# This is a daily-bar system, so there is nothing to gain from running it more
# often than the data changes. The default fires shortly after the US close and
# then idles — a cron-style scheduler would work equally well, but a loop keeps
# the gateway connection warm and avoids a second moving part.
set -eu

: "${OUT_DIR:=/site}"
: "${REFRESH_SECONDS:=3600}"
: "${SYMBOLS:=SPY}"
: "${CORE:=0.40}"
: "${REDACT:=--redact}"

log() { echo "[$(date -u '+%Y-%m-%dT%H:%M:%SZ')] $*"; }

# shellcheck disable=SC2086
run_once() {
  log "generating -> ${OUT_DIR} (redact='${REDACT}')"
  if fireplanner \
        --provider gateway \
        --host "${IB_HOST}" --port "${IB_PORT}" --client-id "${IB_CLIENT_ID}" \
        --cache \
        publish ${SYMBOLS} --core "${CORE}" ${REDACT} -o "${OUT_DIR}"
  then
    log "ok"
  else
    # A failed refresh must not blank the site. The previous files stay in
    # place and status.json keeps its old timestamp, which is what the
    # staleness banner on the page is there to surface.
    log "FAILED — keeping the previous build; page will show its own staleness"
  fi
}

log "starting; refresh every ${REFRESH_SECONDS}s"
while true; do
  run_once
  sleep "${REFRESH_SECONDS}"
done
