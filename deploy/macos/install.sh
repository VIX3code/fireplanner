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
IB_PORT="${IB_PORT:-4001}"

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

# ---- launchd --------------------------------------------------------------
mkdir -p "$HOME/Library/LaunchAgents"
sed -e "s|__SCRIPT__|$HERE/run_fireplanner.sh|g" \
    -e "s|__LOGDIR__|$HERE|g" \
    -e "s|__WORKDIR__|$HERE|g" \
    -e "s|__IBPORT__|$IB_PORT|g" \
    "$PLIST_SRC" > "$PLIST_DST"
say "plist     $PLIST_DST"

# bootout first so re-running install.sh is idempotent
launchctl bootout "gui/$UID/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$UID" "$PLIST_DST"
launchctl enable "gui/$UID/$LABEL"
say "loaded    $LABEL (weekdays 16:35 local)"

cat <<TXT

  Done. Next:
    1. Make sure TWS or IB Gateway is running and logged in (port $IB_PORT).
    2. Test it now, without waiting for the schedule:
         launchctl kickstart -p gui/$UID/$LABEL
       then watch:  tail -f $HERE/fireplanner.log
    3. Open the pages:  open $HERE/fireplanner_site/index.html

  To remove:  launchctl bootout gui/$UID/$LABEL && rm $PLIST_DST
TXT
