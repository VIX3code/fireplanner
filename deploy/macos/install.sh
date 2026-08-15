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
# 3.10 is a hard floor, and it is not this package's choice: every release of
# ib_async — the library that opens the socket to TWS/IB Gateway — is published
# as requires-python >=3.10. On 3.9 pip finds no candidate at all and reports it
# as forty lines of rejected versions from every package in the tree, which
# names neither the real requirement nor the fix. Hence the check here, before a
# venv is built that could never work.
MIN_MAJOR=3; MIN_MINOR=10

py_ver() { "$1" -c 'import sys;print(".".join(map(str,sys.version_info[:3])))' 2>/dev/null; }
py_ok() {
  [ -x "$1" ] || command -v "$1" >/dev/null 2>&1 || return 1
  "$1" -c "import sys;sys.exit(0 if sys.version_info[:2] >= ($MIN_MAJOR,$MIN_MINOR) else 1)" 2>/dev/null
}

PY="${PYTHON:-}"
TOO_OLD=""
if [ -n "$PY" ]; then
  py_ok "$PY" || { TOO_OLD="$PY"; PY=""; }
else
  # Newest first, and a candidate that is too old does not stop the search:
  # a Mac with 3.9 as `python3` and 3.12 alongside it should get 3.12.
  for c in python3.13 python3.12 python3.11 python3.10 python3 python3.9; do
    p="$(command -v "$c" 2>/dev/null)" || continue
    if py_ok "$p"; then PY="$p"; break; fi
    [ -n "$TOO_OLD" ] || TOO_OLD="$p"
  done
fi

if [ -z "$PY" ]; then
  {
    if [ -n "$TOO_OLD" ]; then
      echo "error: Python $(py_ver "$TOO_OLD") is too old — FirePlanner's IBKR"
      echo "       connection needs $MIN_MAJOR.$MIN_MINOR or newer."
      echo "       (found: $TOO_OLD)"
    else
      echo "error: no python3 found."
    fi
    echo
    echo "  Install a newer Python, then re-run this script — it picks up the"
    echo "  newest one it can find and rebuilds the virtualenv automatically."
    echo
    # Lead with what will actually work on THIS machine. Suggesting Homebrew to
    # someone who does not have it just sends them to `brew: command not found`
    # and a second round trip.
    if command -v brew >/dev/null 2>&1; then
      echo "    brew install python@3.12"
      echo
      echo "  or the macOS installer from https://www.python.org/downloads/macos/"
    else
      echo "  You do not have Homebrew, so use the official installer:"
      echo
      echo "    1. Open https://www.python.org/downloads/macos/"
      echo "    2. Under 'Stable Releases', pick the latest Python 3.12.x"
      echo "    3. Download 'macOS 64-bit universal2 installer' (.pkg) and run it"
      echo "    4. Open '/Applications/Python 3.12/Install Certificates.command'"
      echo "       (one double-click — without it HTTPS from Python can fail)"
      echo
      echo "  3.12 rather than the newest release on purpose: pandas and numpy"
      echo "  ship ready-built wheels for it, so nothing has to compile."
    fi
    echo
    echo "  Nothing else on your Mac changes: this installs a second Python"
    echo "  alongside the one you have and uses it only for FirePlanner."
  } >&2
  exit 1
fi
say "python    $PY  ($(py_ver "$PY"))"

# ---- venv -----------------------------------------------------------------
# A venv built by an older interpreter keeps that interpreter forever, so an
# upgrade would otherwise be invisible: you install 3.12, re-run this, and it
# quietly reuses the 3.9 venv that failed in the first place.
if [ -d "$VENV" ] && ! py_ok "$VENV/bin/python"; then
  say "removing  $VENV (built with Python $(py_ver "$VENV/bin/python"), too old)"
  rm -rf "$VENV"
fi
if [ ! -d "$VENV" ]; then
  say "creating  $VENV"
  "$PY" -m venv "$VENV"
fi
"$VENV/bin/python" -m pip install --quiet --upgrade pip

# Full output to a file rather than --quiet to the void: when this fails, the
# reason is in pip's output, and "installing... ERROR" with nothing to read is
# the least useful thing this script could print.
INSTALL_LOG="$HERE/install.log"
say "installing fireplanner[gateway,cache]"
if ! "$VENV/bin/python" -m pip install -e "$REPO[gateway,cache]" > "$INSTALL_LOG" 2>&1; then
  {
    echo
    echo "error: pip could not install the dependencies. Last 25 lines"
    echo "       (full output in $INSTALL_LOG):"
    echo
    tail -25 "$INSTALL_LOG" | sed 's/^/    /'
  } >&2
  exit 1
fi

# Verify the two things that have to work, and name whichever does not. This
# used to print FAILED and carry on to schedule a job that could never run.
verify_fail() {
  {
    echo
    echo "error: $1"
    echo "       Full install output: $INSTALL_LOG"
    echo "       Python: $("$VENV/bin/python" -V 2>&1) at $VENV/bin/python"
  } >&2
  exit 1
}
"$VENV/bin/python" -c "import fireplanner" 2>/dev/null \
  || verify_fail "the fireplanner package did not import after installing."
"$VENV/bin/python" -c "import ib_async" 2>/dev/null \
  || verify_fail "ib_async is missing — the IBKR connection would not work.
       This is the library that needs Python 3.10+."
"$VENV/bin/fireplanner" --help >/dev/null 2>&1 \
  || verify_fail "the 'fireplanner' command did not run after installing."
say "installed  ok  (fireplanner + ib_async import cleanly)"

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
