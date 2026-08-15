#!/bin/bash
# Install the FirePlanner scheduled job on macOS.
#
# Creates its own virtualenv and its own launchd label. It does not touch any
# other job, script or config in this directory — if you already run other
# scheduled work here, this sits alongside it.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="${FIREPLANNER_REPO:-}"
VENV="$HERE/fireplanner-venv"
LABEL="com.fireplanner.publish"
PLIST_SRC="$HERE/com.fireplanner.publish.plist"
PLIST_DST="$HOME/Library/LaunchAgents/$LABEL.plist"

# Captured before fireplanner.env is sourced, and defaulted after, so that
# anything given on the command line wins over the config file. Without this,
# `IB_PORT=4002 ./install.sh` would be silently overridden by whatever the file
# happened to say — the opposite of what typing it means.
CLI_IB_PORT="${IB_PORT:-}"
CLI_TIMES="${FIREPLANNER_TIMES:-}"
CLI_DAYS="${FIREPLANNER_DAYS:-}"

say() { printf '  %s\n' "$*"; }

if [ -z "$REPO" ]; then
  # Assume this script is being run from inside a checkout.
  REPO="$(cd "$HERE/../.." && pwd)"
fi
if [ ! -f "$REPO/pyproject.toml" ]; then
  echo "error: cannot find the fireplanner checkout." >&2
  echo "       Set FIREPLANNER_REPO=/path/to/fireplanner and re-run." >&2
  exit 1
fi
say "repo      $REPO"

# ---- python ---------------------------------------------------------------
PY="${PYTHON:-}"
if [ -z "$PY" ]; then
  for c in python3.12 python3.11 python3.10 python3.9 python3; do
    if command -v "$c" >/dev/null 2>&1; then PY="$(command -v "$c")"; break; fi
  done
fi
[ -n "$PY" ] || { echo "error: no python3 found" >&2; exit 1; }
say "python    $PY  ($("$PY" -c 'import sys;print(".".join(map(str,sys.version_info[:3])))'))"

# ---- venv -----------------------------------------------------------------
if [ ! -d "$VENV" ]; then
  say "creating  $VENV"
  "$PY" -m venv "$VENV"
fi
"$VENV/bin/python" -m pip install --quiet --upgrade pip
say "installing fireplanner[gateway,cache]"
"$VENV/bin/python" -m pip install --quiet -e "$REPO[gateway,cache]"
say "installed  $("$VENV/bin/fireplanner" --help >/dev/null 2>&1 && echo ok || echo FAILED)"

# ---- config ---------------------------------------------------------------
# Seeded once and never overwritten, so re-running install.sh cannot clobber a
# deploy target you have already set.
if [ ! -f "$HERE/fireplanner.env" ] && [ -f "$HERE/fireplanner.env.example" ]; then
  sed -e "s|^IB_PORT=.*|IB_PORT=${CLI_IB_PORT:-4001}|" \
      "$HERE/fireplanner.env.example" > "$HERE/fireplanner.env"
  say "config    $HERE/fireplanner.env  (created — edit to publish to a domain)"
else
  say "config    $HERE/fireplanner.env  (left as is)"
fi
# It holds a deploy token once you set one up, so it is owner-read regardless of
# whether this run created it.
chmod 600 "$HERE/fireplanner.env" 2>/dev/null || true

# ---- schedule -------------------------------------------------------------
# The schedule lives in fireplanner.env so there is one config file, but unlike
# everything else in there it is baked into the plist at install time: launchd
# reads a schedule when the job is loaded, not when it runs. Changing it means
# re-running install.sh.
# shellcheck disable=SC1091
[ -f "$HERE/fireplanner.env" ] && . "$HERE/fireplanner.env"
IB_PORT="${CLI_IB_PORT:-${IB_PORT:-4001}}"
TIMES="${CLI_TIMES:-${FIREPLANNER_TIMES:-07:00}}"    # comma-separated HH:MM, Mac local
DAYS="${CLI_DAYS:-${FIREPLANNER_DAYS:-daily}}"       # daily | weekdays

# ---- launchd --------------------------------------------------------------
mkdir -p "$HOME/Library/LaunchAgents"
# plistlib rather than sed: the paths substituted below can contain characters
# that would need escaping in a sed expression or would break the XML, and a
# malformed plist fails at bootstrap with a message that names no cause.
"$VENV/bin/python" - "$PLIST_SRC" "$PLIST_DST" "$HERE" "$IB_PORT" "$TIMES" "$DAYS" <<'PYEOF'
import plistlib, sys

src, dst, here, ib_port, times, days = sys.argv[1:7]

with open(src, "rb") as fh:
    plist = plistlib.load(fh)

plist["ProgramArguments"] = ["/bin/bash", f"{here}/run_fireplanner.sh"]
plist["StandardOutPath"] = f"{here}/fireplanner.launchd.out"
plist["StandardErrorPath"] = f"{here}/fireplanner.launchd.err"
plist["WorkingDirectory"] = here
plist["EnvironmentVariables"]["IB_PORT"] = str(ib_port)

# launchd weekdays: 0 and 7 are both Sunday, 1 is Monday.
weekdays = [1, 2, 3, 4, 5] if days.strip().lower() == "weekdays" else [None]

schedule = []
for slot in times.split(","):
    slot = slot.strip()
    if not slot:
        continue
    hh, _, mm = slot.partition(":")
    entry_base = {"Hour": int(hh), "Minute": int(mm or 0)}
    for wd in weekdays:
        entry = dict(entry_base)
        if wd is not None:
            entry["Weekday"] = wd
        schedule.append(entry)

if not schedule:
    sys.exit(f"error: FIREPLANNER_TIMES={times!r} parsed to no runs")

plist["StartCalendarInterval"] = schedule

with open(dst, "wb") as fh:
    plistlib.dump(plist, fh)

print(f"  schedule  {times} local, {days}")
PYEOF
[ -f "$PLIST_DST" ] || { echo "error: failed to write $PLIST_DST" >&2; exit 1; }
say "plist     $PLIST_DST"

# bootout first so re-running install.sh is idempotent
launchctl bootout "gui/$UID/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$UID" "$PLIST_DST"
launchctl enable "gui/$UID/$LABEL"
say "loaded    $LABEL"

cat <<TXT

  Done. Next:
    1. Make sure TWS or IB Gateway is running and logged in (port $IB_PORT).
    2. Test it now, without waiting for the schedule:
         launchctl kickstart -p gui/$UID/$LABEL
       then watch:  tail -f $HERE/fireplanner.log
    3. Open the page:  open $HERE/fireplanner_site/index.html

  To publish to your own domain, set FIREPLANNER_RSYNC_TARGET (or
  FIREPLANNER_PUBLISH_CMD) in $HERE/fireplanner.env. Setting either one turns
  redaction on by default, so no balances or position sizes leave this machine.

  To remove:  launchctl bootout gui/$UID/$LABEL && rm $PLIST_DST
TXT
