# Deploying FirePlanner

## The one constraint that decides everything

**IBKR's API is not a cloud API.** Both transports need a long-lived, interactively
authenticated desktop-class process:

- `GatewayProvider` opens a TCP socket to TWS or IB Gateway on `localhost`.
- `WebApiProvider` talks to the Client Portal gateway, also on `localhost`, and its session
  expires after a few minutes of inactivity.

There is no API key you can paste into an environment variable. IBKR also forces a **daily
re-authentication**, and most accounts now require **2FA**, which means a push notification
someone has to approve. That rules out the usual answers:

| Platform | Works? | Why |
|---|---|---|
| Vercel / Netlify / Cloudflare Workers | ❌ | Serverless — no persistent process to hold the session |
| AWS Lambda / Cloud Functions | ❌ | Same, plus no way to complete 2FA |
| Heroku-style PaaS | ⚠️ | Only if you can run the gateway as a worker dyno; most people can't |
| **VPS / home server / NAS** | ✅ | A process can stay up and be re-authenticated |
| **Static host, for the output only** | ✅ | The generated pages are self-contained files |

## So: can it run on a web domain?

**Yes — the pages can. The broker connection can't.** The right shape is two tiers:

```
   PRIVATE (has broker access)                PUBLIC (or private) DOMAIN
 ┌───────────────────────────────┐          ┌──────────────────────────┐
 │  IB Gateway  ──socket──▶      │          │                          │
 │  fireplanner publish  ────────┼── HTML ──▶  any static file host    │
 │  (cron / systemd / container) │          │  behind auth + TLS       │
 └───────────────────────────────┘          └──────────────────────────┘
```

This works because `fireplanner publish` emits **fully self-contained HTML** — inline CSS,
inline SVG, inline JS, zero external requests, no server-side code. Verified: 0 external
references in either page. So tier two can be Caddy, nginx, S3+CloudFront, GitHub Pages,
Netlify — anything that serves files.

## One file or three?

`publish` has two layouts. They contain the same analysis; they differ in how it is split.

| | Three files (default) | One file (`--single`) |
|---|---|---|
| What you get | `index.html` landing page linking `signal_desk.html` and `dashboard.html` | one `index.html` holding both, behind a tab strip |
| Size | 85 KB + 315 KB | 389 KB (110 KB gzipped) |
| Best for | reading on a desk, two tabs open | **a domain** — one URL, one upload, nothing half-updated |

**`signal_desk.html` answers "what do I do today."** One instruction at the top — buy, sell,
hold, or wait — the allocation ladder showing where the target sits, three pass/fail checks
explaining why, the exact levels that would change it, and how often the signal has been
wrong. It is deliberately narrow. Roughly 6 sections.

**`dashboard.html` answers "what is the market doing."** The regime gate and its four
components, ten indicator tiles, price against its trend anchors, the watchlist scorecard,
your book, the backtest evidence, the best/worst-day analysis, and the VIX term structure.
Roughly 10 sections and most of the file size, because every chart embeds its own data.

The combined page keeps both, opens on the decision, and switches without a page load:

```bash
fireplanner publish SPY --single --redact -o site
```

Things the merge has to get right, all asserted in `tests/test_single_page.py`:

- **One staleness banner.** The script that ages the page looks it up by element id; two
  copies and only the first would ever update.
- **Unique chart handles.** Tooltips resolve `#<id>-hit` globally — a collision would wire
  one chart's crosshair to another chart's numbers.
- **Readable with JavaScript off.** The tab strip is progressive enhancement: no script, no
  hiding, and the document simply reads top to bottom. Same on a printout.
- **Deep links.** `…/index.html#analysis` opens straight to the analysis half.

Switching layouts deletes the files the other layout wrote. Left behind they would keep
resolving on your domain, serving whatever signal was current the day you switched.

## Before you point a domain at it: two real risks

**1. The pages contain your balance sheet.** Unredacted, they publish net liquidation, every
position, and your trade sizes. Always pass `--redact` for anything reachable by anyone but you:

```bash
fireplanner publish --redact -o site      # percentages and prices only
fireplanner publish -o site               # full detail — local use only
```

Redaction strips equity, share counts, notional, position rows and P&L, while keeping targets,
prices, scores and the regime — so the signal survives and the account does not.
`tests/test_publish.py` asserts no account figure survives.

**2. Market-data redistribution.** Your IBKR market data subscription is licensed to *you*.
Republishing IBKR quotes on a public website is a different activity from viewing them, and
can breach the agreement. Derived indicators and your own signal are a much safer thing to
publish than raw prices. **If in doubt, put the site behind auth and keep the audience to
yourself** — which you want anyway, per risk 1.

---

## Option A — Docker Compose (recommended)

Three containers: the gateway (never exposed), the generator, and Caddy for TLS + auth.

```bash
cp deploy/.env.example deploy/.env      # fill in credentials, domain, auth hash
docker run --rm caddy:2-alpine caddy hash-password   # -> AUTH_HASH
cd deploy && docker compose up -d
```

Notes on the compose file, all deliberate:

- `ib-gateway` has **no `ports:` mapping** and sits on an `internal: true` network. Exposing
  4001/4002 to the internet hands over the account — there is no auth on that socket.
- `READ_ONLY_API: "yes"` blocks order entry at the gateway itself, not just in this code.
- The generator writes to a shared volume; Caddy mounts it read-only.
- A failed refresh **keeps the previous build** rather than blanking the page. The staleness
  banner on the page then tells you the bars are old, which is the honest failure mode.

Start with `IB_MODE=paper` and `IB_PORT=4002`. Move to live only once you have watched it
regenerate correctly for a few sessions.

## Option B — macOS + launchd (for a Mac that already runs TWS)

The natural fit if you keep a Mac on with IB Gateway logged in. `deploy/macos/` has
everything; it creates its own virtualenv and its own launchd label, and touches no other
scheduled job.

```bash
git clone https://github.com/VIX3code/fireplanner.git
cd fireplanner && git checkout claude/sp500-ibkr-trading-system-fgpxrc

IB_PORT=4001 ./deploy/macos/install.sh        # 4001 live · 4002 paper · 7496/7497 TWS
```

Prefer to keep it beside your existing jobs? Copy the two scripts anywhere and point them
at the checkout:

```bash
cp deploy/macos/{run_fireplanner.sh,install.sh,com.fireplanner.publish.plist} \
   ~/Claude/Scheduled/
FIREPLANNER_REPO=~/fireplanner IB_PORT=4001 ~/Claude/Scheduled/install.sh
```

Test it immediately rather than waiting for the schedule. Run the runner directly and it
prints its progress to the terminal instead of only to the log:

```bash
./deploy/macos/run_fireplanner.sh
open deploy/macos/fireplanner_site/index.html
```

This is the same script launchd runs, reading the same `fireplanner.env`, so what you see by
hand is exactly what happens at 07:00 — no second command with its own copy of the settings to
drift out of step. To exercise the launchd path itself:

```bash
launchctl kickstart -p gui/$UID/com.fireplanner.publish
tail -f deploy/macos/fireplanner.log
```

**It runs at 07:00 local, every day**, which is a pre-open read: the newest *complete* daily
bar at that hour is the previous session's close, so you get the decision before the bell.
launchd fires a missed calendar interval once on wake, so a sleeping Mac catches up rather
than skipping the day.

Change it in `fireplanner.env` and **re-run `install.sh`** — launchd reads a schedule when the
job is loaded, not when it runs, so this is the one setting an edit alone does not apply:

```bash
FIREPLANNER_TIMES="07:00"          # comma-separated HH:MM, the Mac's local time
FIREPLANNER_DAYS="daily"           # daily | weekdays
```

Any time is safe. A run only ever sees the last completed daily bar — a partial bar for a
session still in progress is dropped rather than charted, verified for the pre-open,
mid-session and after-close cases in `tests/test_system.py`. What the hour actually decides is
**how long the page lags the close that produced it**:

| Schedule | You see | Trade-off |
|---|---|---|
| `07:00` | yesterday's close, before the open | between yesterday's close and this morning's run the page is one session behind — an evening check shows the previous day |
| `16:35` | today's close, 35 minutes after it | nothing to read with your morning coffee until you open the page and it is already 16 hours old |
| `07:00,16:35` | both | two runs a day; the gateway has to be up for both |

Weekend runs on a `daily` schedule are harmless — daily bars do not change, so Saturday's run
rebuilds the same page from Friday's close.

The runner is written for unattended operation, which mostly means refusing to do harm:

- **It stages the build and only swaps it in on success.** A failed run leaves yesterday's
  complete pages in place; the staleness banner then tells you they are old. A half-written
  page is worse than an old one.
- **It checks the gateway is actually listening first.** Otherwise the failure is a
  30-second timeout buried in a stack trace.
- **It never uploads a failed build.** The upload step runs only after a build that
  succeeded, so a bad run cannot replace a good page on your host.
- **It always exits 0.** A closed market or a sleeping gateway is not an error worth having
  launchd retry.

### Pushing it to your own domain

`install.sh` seeds `deploy/macos/fireplanner.env` (gitignored, `chmod 600`, never overwritten
on re-install). The runner sources it every run, so edits take effect immediately — no plist
regeneration, no `launchctl` reload.

**Setting any deploy target turns redaction on by default.** The pages otherwise carry your net
liquidation, your positions and your trade sizes, and a page published with those in it cannot
be un-published. `FIREPLANNER_REDACT=0` overrides it and the runner logs a warning every run.

Every page also ships `<meta name="robots" content="noindex, nofollow, noarchive">`. Hosting
it makes it reachable; it should not also make it findable, and a page nobody links to still
gets indexed once the URL appears in a referrer log. The tag travels with the file, so it
works on any host.

#### Netlify

```bash
npm install -g netlify-cli        # once
```

```bash
# deploy/macos/fireplanner.env
FIREPLANNER_SINGLE=1
FIREPLANNER_NETLIFY_SITE="00000000-1111-2222-3333-444444444444"
NETLIFY_AUTH_TOKEN="nfp_..."
```

The site ID is under **Site configuration → General → Site ID**; the token under **User
settings → Applications → Personal access tokens**. The runner deploys with `CI=1` so the CLI
never tries to prompt in a job with no terminal, and resolves the `netlify` binary by path
because launchd's PATH does not include Homebrew's directory on Apple Silicon.

#### rsync over SSH

```bash
FIREPLANNER_RSYNC_TARGET="me@myhost.com:/var/www/fireplanner/"   # trailing slash matters
```

`--delete` keeps the host from accumulating files the build no longer produces. One trap: a
launchd job has no terminal, so an SSH key with a passphrase would hang rather than prompt.
The runner passes `BatchMode=yes` so it fails fast and logs instead. Use a passphrase-less
deploy key, or add the key to the login keychain (`ssh-add --apple-use-keychain
~/.ssh/id_ed25519`) with `UseKeychain yes` in `~/.ssh/config`.

#### Anything else

`FIREPLANNER_PUBLISH_CMD` receives the build directory as `$1`:

```bash
FIREPLANNER_PUBLISH_CMD='aws s3 sync "$1" s3://my-bucket/ --delete'
FIREPLANNER_PUBLISH_CMD='npx --yes wrangler pages deploy "$1" --project-name fireplanner'
FIREPLANNER_PUBLISH_CMD='rclone sync "$1" mydrive:fireplanner'
```

All of these run **only after a build that succeeded**, so a failed run can never replace a
good page on your host — and a failed upload leaves the local build intact for the next
attempt.

Everything lands next to the scripts: `fireplanner.log`, `fireplanner_site/`,
`fireplanner-venv/`, `fireplanner.env`. To remove it:

```bash
launchctl bootout gui/$UID/com.fireplanner.publish
rm ~/Library/LaunchAgents/com.fireplanner.publish.plist
```

### Python on macOS

**You need Python 3.10 or newer, and macOS does not ship it.** Every published release of
`ib_async` — the library that opens the socket to TWS/IB Gateway — is `requires-python
>=3.10`, so on 3.9 there is no version to install at all:

```
ERROR: Could not find a version that satisfies the requirement ib_async>=1.0
ERROR: No matching distribution found for ib_async>=1.0
```

`install.sh` now checks for this before building anything and tells you what to run. The fix:

```bash
brew install python@3.12          # or the installer from python.org/downloads
./deploy/macos/install.sh         # picks up the newest it can find
```

This installs a second Python alongside your existing one and uses it only for FirePlanner.
Re-running `install.sh` also **deletes and rebuilds a virtualenv built by a too-old
interpreter** — a venv keeps whichever Python created it, so without that step upgrading would
appear to change nothing.

The rest of the package does run on 3.9: the suite is green on 3.9.23 with pandas 2.3.3,
because it exercises the model against committed snapshots and never imports `ib_async`. That
is exactly what made 3.9 look supportable when it was not — the tests cover the model, and the
broker adapter is the part with the floor.

## Option B2 — systemd on a box that already runs IB Gateway

```bash
sudo cp deploy/fireplanner.{service,timer} /etc/systemd/system/
sudo systemctl enable --now fireplanner.timer
systemctl list-timers fireplanner        # confirm the next run
```

The timer fires **weekdays at 16:30 New York**, just after the close. This is a daily-bar
system — running it more often only burns IBKR's pacing budget (~60 historical requests per
10 minutes) for data that has not changed. `Persistent=true` catches up if the machine was
asleep.

The unit runs with `ProtectSystem=strict` and a read-write path allowlist: it only ever reads
from the broker and writes one directory.

## Option C — generate locally, host the output anywhere

The simplest thing that works, and a good default if you already have a laptop that runs TWS:

```bash
fireplanner publish SPY --single --redact -o site
rsync -az --delete site/ user@host:/var/www/fireplanner/
# or: aws s3 sync site/ s3://your-bucket/ --delete
```

That is two files — `index.html` and `status.json` — and neither needs a runtime, so this is
as robust as a static site gets. Drop `--single` if you would rather have the three-file
layout with a landing page.

---

## Serving it safely

`deploy/Caddyfile` ships with three approaches — pick one:

1. **Basic auth** — one line, fine for a single reader.
2. **An identity proxy** (Cloudflare Access, oauth2-proxy) — better if more than one person reads it.
3. **No public exposure at all** — bind it to a Tailscale or WireGuard address. Safest, and the
   one to prefer if you are the only audience.

The bundled CSP is deliberately tight, which the pages can afford because they load nothing
external:

```
default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; img-src data:; font-src data:
```

## Monitoring

Every build writes `status.json` next to the pages:

```json
{ "as_of": "2026-08-12", "generated_utc": "...", "redacted": true,
  "decision": { "action": "WAIT", "target_pct": 0.4, "regime_label": "Risk-On" } }
```

Point a check at it and alert when `generated_utc` falls behind — a silently stale signal is
the failure mode that actually costs money.

The pages also police themselves. Each carries the date of its last bar and compares it to
**the viewer's clock**, showing a warning banner once the bars are more than four days old
(four absorbs a normal weekend). This is computed on open, not at build: staleness baked in at
generation time would freeze — a page built today would report "1 day old" forever, including
weeks later. No network is involved, so it works from `file://` and under the strict CSP.

## Telegram alerts

IBKR's own alerts only reach email or IBKR Desktop, and they fire on a **fixed price
level, intraday**. The rule that matters here is neither: it is *two consecutive closes
below the 50-day*, judged after the close, against an average that rises every session. A
price alert can only approximate it, and drifts out of date as the average moves.

`fireplanner notify` watches the real condition instead, because the model has already
computed it.

```bash
export TELEGRAM_BOT_TOKEN=...   # @BotFather -> /newbot
export TELEGRAM_CHAT_ID=...     # message the bot, then GET /bot<TOKEN>/getUpdates

fireplanner notify --dry-run --force    # see the message without sending
fireplanner notify                      # send only if something changed
```

### Reusing a bot you already have

If a bot already serves another project, point FirePlanner at that project's env file
rather than copying the token. Two copies of a secret is one more than necessary, and the
second is the one that gets committed by accident.

```bash
fireplanner notify --env-file ~/daily-market-update/.env
```

Three things matter when one bot serves several systems:

- **`--source`** (default `FirePlanner`) prefixes every message. Without it, "Target cut to
  40%" arrives in the same chat as your other alerts with nothing saying which system
  spoke.
- **`--thread-id`** posts into a specific forum topic, so these do not interleave with the
  other project's feed in a shared group. Set `TELEGRAM_THREAD_ID` to make it permanent.
- **The state file is per-project.** Keep `--state` distinct so the two systems cannot
  suppress each other's notifications.

Precedence is explicit argument → `--env-file` → process environment.

**It stays quiet on purpose.** Nothing is sent unless one of these happens, and state is
kept on disk so a scheduler firing twice a day does not message you twice:

| Event | Urgency |
|---|---|
| Confirmed break of the 50-day (two closes) | ⚠️ urgent — this overrides the cooldown |
| First close below the 50-day | 🔔 heads-up; nothing has changed yet |
| Committed target moved | 🔔 the actionable one |
| Instruction changed to BUY/SELL | 🔔 |

A notifier that pings you daily is one you learn to ignore, which is the same as not
having it. Set both env vars in `deploy/.env` and the Compose stack sends automatically
after each successful build; leave either blank and it silently does nothing. A failed
send is logged and never fails the build.

## Operational reality check

- **The gateway will log you out daily.** IBC handles the restart; 2FA may still need a tap on
  IBKR Mobile. Budget for the fact that this is not a fully unattended system.
- **Pin the gateway image tag.** IBKR ships breaking gateway updates without notice.
- **Paper first.** `READ_ONLY_API=yes` plus `IB_MODE=paper` means the worst a bug can do is
  render a wrong number.
- **This publishes a signal, not orders.** Nothing in this deployment places a trade, and
  nothing should until you have watched the signal behave for a while.
