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

## Option B — systemd on a box that already runs IB Gateway

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
fireplanner publish --redact -o site
rsync -av --delete site/ user@host:/var/www/fireplanner/
# or: aws s3 sync site/ s3://your-bucket/ --delete
```

Nothing about the output needs a runtime, so this is as robust as a static site gets.

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

## Operational reality check

- **The gateway will log you out daily.** IBC handles the restart; 2FA may still need a tap on
  IBKR Mobile. Budget for the fact that this is not a fully unattended system.
- **Pin the gateway image tag.** IBKR ships breaking gateway updates without notice.
- **Paper first.** `READ_ONLY_API=yes` plus `IB_MODE=paper` means the worst a bug can do is
  render a wrong number.
- **This publishes a signal, not orders.** Nothing in this deployment places a trade, and
  nothing should until you have watched the signal behave for a while.
