# Trade management: the Swing Desk

The analysis side of FirePlanner tells you what the market is doing. This side manages a
position from the moment you buy it: it sizes every trade for the same fixed loss, puts a GTC
stop and a target on it at IBKR, moves the stop up as the trade works, keeps the book from piling
into volatile names or one sector, pauses new buys when the market or your results turn, and
journals every trade in R.

```bash
fireplanner trade demo -o swing_desk.html        # the dashboard on a simulated book, no IBKR needed
fireplanner --port 4002 trade once --dry-run     # one pass against your paper account, sends nothing
fireplanner --port 4002 trade run                # guardian + live dashboard at http://127.0.0.1:8765
fireplanner --port 4002 trade check 700:HK       # the pre-trade check for one stock
fireplanner trade watch add NVDA D05:SG 7203:JP  # edit the watchlist
fireplanner trade watch import tws_export.csv    # import a TWS watchlist export
fireplanner trade bucket NVDA volatile           # confirm a stock's type
fireplanner trade earnings NVDA 2026-10-28       # set an earnings date (or "clear")
fireplanner trade unlock MP                      # unlock a stock after two stop-outs
```

---

## The rules

All of them live in the `trading:` section of `config/config.yaml`. The few marked ⚙ can also be
changed on the dashboard's Settings card.

| Rule | Setting |
|---|---|
| **Fixed loss per trade** ⚙ | every position is sized so its stop costs **$250** (before gaps). A tight stop means a bigger position. |
| Position cap | no single position (or add) over **$10,000**; the book at most **$100,000** and **20** positions |
| Rounding | share and lot counts round **up**, unless that adds more than 20% to the loss, then down |
| Stop | GTC, the **tighter of 5% or 2.5 × the average daily range** below entry |
| Stock types | **Steady** (moves < 2% a day, target +10%, max 10) · **Core** (2–3.5%, +15%, max 10) · **Volatile** (> 3.5%, +20%, max 7) |
| At the target | sell **half**, trail the rest |
| Stop ratchet | to the **entry price** once the stock has been +5%, then **3 × daily range** under the high. Never down. |
| Two strikes | after a stop-out you may re-enter once; a **second stop-out in a row locks the stock until you unlock it** |
| Add to a winner | **one** add, only once the stop is at entry or better, sized for the same fixed loss; the whole position's stop moves up so it still risks about one fixed loss |
| Time stop ⚙ | a position that hasn't reached **+5% within 4 weeks** is flagged (or sold, with `time_stop_action: sell`) |
| Earnings ⚙ | shown beside every stock; a buy within **7 days** of a report is warned (or blocked, with `earnings_block: true`) |
| Market weather | regime blended with breadth per market; new buys in a **Defensive** market risk half the fixed loss, **Risk-Off** blocks them |
| Anchored VWAP | earnings / breakout / swing-low VWAP levels in the check; the nearest below the price is offered as a **pullback limit** |
| Sector cap | at most **4** positions in one industry group |
| Open-risk cap | the book can't lose more than **$5,000** if every stop hits at once |
| Swing guide | a **warning** when Volatile would drive more than 50% of the book's daily swing |
| Circuit breakers | new buys pause after **$750** lost in a day, **$1,500** in a week, or **3 stop-outs in a row** (until you resume); a **kill switch** cancels working buys; at most **10** buys a day |

A stop-out is any exit worse than −0.5%, including a sale you make at a loss; a win or a scratch
resets the count.

### Why a fixed loss

With a fixed amount per trade, a calm stock with a 3% stop gets roughly a $8,300 position and a
volatile one with a 5% stop about $5,000. Both lose $250 if stopped. No single stock can hurt more
than another, and the journal's R-multiples mean the same thing on every row.

### Why two allocation bars

A Volatile stock moves about three times as much in a day as a Steady one. The dashboard shows
**share of money** and **share of daily swing** (value × daily range) side by side; the second is
the one that shows concentration. The bucket and sector caps block on position counts; the swing
guide only warns.

---

## Five markets

| Market | Write it as | Currency | Lot | Notes |
|---|---|---|---|---|
| US | `NVDA` | USD | 1 | SMART routing |
| London | `HSBA:LN` | GBP | 1 | prices in **pence**; value = qty × price ÷ 100 |
| Hong Kong | `700:HK` | HKD | **per stock** | set a lot IBKR doesn't report: `trade watch add 700:HK --lot 100` |
| Singapore | `D05:SG` | SGD | 100 | |
| Tokyo | `7203:JP` | JPY | 100 | |

- **Board lots.** Sizes round to whole lots. When one lot would lose more than 20% over the fixed
  loss (a pricey Tokyo or Hong Kong stock), the check blocks it and says why.
- **Tick sizes.** Stops round **up** (toward the price, so a stop-out never loses more than the
  stop %), targets round **down** (so they stay reachable), on the exchange's own grid from
  IBKR's market rules.
- **Cash account.** You can't borrow, so the check blocks a buy that settled cash doesn't cover,
  and warns when you don't hold enough of the stock's currency. Sale proceeds are usable only
  after settlement (T+1 US, T+2 elsewhere), which matters for the "re-enter right away" rule:
  keep some cash spare.
- **Inverse ETFs** are allowed. The check notes a -1× fund and warns on a leveraged one
  (-2×, -3×): they reset daily and drift over multi-week holds.

---

## Running it live with IBKR

The dashboard has two modes. `trade demo` and `trade snapshot` write a static page. **`trade run`
is live**: it holds a connection to TWS or IB Gateway all day, runs the guardian every 30
seconds (at once on a fill), and serves a dashboard that refreshes every 5 seconds and can check
trades, send buys and adds, sell, pause and unlock.

### On your own computer

1. **Log in to TWS or IB Gateway** with your **paper** account. Under *Configure → Settings →
   API → Settings*: tick *Enable ActiveX and Socket Clients*, add `127.0.0.1` to trusted IPs,
   and note the port (7497 TWS paper, 4002 Gateway paper).
2. **Install and check the connection:**
   ```bash
   pip install -e ".[gateway]"
   fireplanner --port 7497 trade doctor
   ```
   The doctor connects, reads the account id (paper accounts start with "D"), checks whether
   the gateway accepts orders (with a what-if order that can't trade), reads positions and cash,
   gets a price in each of the five markets, and finds daily history, exchange rates and the
   weather indexes. It places no orders, and says what to fix.
3. **Start it:** `fireplanner --port 7497 trade run`, then open **http://127.0.0.1:8765**. The
   header shows *IBKR paper · DU•••1234* when connected, and turns red if the connection drops;
   the guardian keeps retrying. Orders stay in **dry run** until `trading.enabled: true`.

### Always on, reachable from your phone (Docker)

`deploy/docker-compose.yml` runs IB Gateway, the guardian with its dashboard, and Caddy for HTTPS
and a password. The dashboard is at **https://your-domain/desk/**.

```bash
cp deploy/.env.example deploy/.env        # IBKR login, domain, password hash, dashboard token
docker run --rm caddy:2-alpine caddy hash-password          # -> AUTH_HASH
cd deploy && docker compose up -d
docker compose exec swing-desk fireplanner --host ib-gateway --port 4004 trade doctor
```

Then open `https://your-domain/desk/?token=<FIREPLANNER_DASH_TOKEN>` once: the browser asks for
the password, and the page remembers the token for the session. Nothing but Caddy has a public
port; the gateway and the dashboard are only reachable through it. IBKR asks for a daily
re-login, and most accounts need 2FA: approve it on your phone when IB Gateway restarts.

### Paper first, then live

1. **Dry run on paper** (the default) for a few days. Every order the guardian would place
   shows as "Dry run, not sent" in Activity. Buy a few things on paper and watch it.
2. **Orders on, still on paper:** set `trading.enabled: true` (and, in Docker,
   `IB_READ_ONLY_API=no`). Stops and targets are now placed on the paper account. Test a buy from
   the dashboard, one from TWS and one from the IBKR app; cancel a stop by hand and watch it
   come back.
3. **Live**: see the next section.

## Switching to your live account

The live account uses the same code and the same rules; only three settings and a login change.
Do it in two steps, so the first real-money session sends nothing.

**Before you start, in TWS / IB Gateway (logged in to the live account)**

- *Configure → Settings → API → Settings*: tick *Enable ActiveX and Socket Clients*, **untick
  Read-Only API**, add `127.0.0.1` to trusted IPs. The live port is 7496 (TWS) or 4001 (Gateway).
- *API → Precautions*: tick **Bypass Order Precautions for API Orders**. Otherwise TWS may hold a
  stop behind a confirmation pop-up that nobody is there to click.
- Market-data subscriptions for the exchanges you trade, so the ratchet and sizing get prices.

**Step 1: live account, nothing sent.** In `config/config.yaml`:

```yaml
trading:
  allow_live: true        # connect to the live account
  enabled: false          # but log orders instead of sending them
```

```bash
fireplanner --port 7496 trade doctor      # 4001 for IB Gateway
fireplanner --port 7496 trade run
```

The header turns red: *LIVE ACCOUNT · IBKR live · U•••1234*. On this first run, **every position
already in the account is left alone**: it appears under *Not managed*, with no stop and no
target, and this system will never sell it. For each one you want protected by the 5% /
2.5 × range stop and the target, press **Manage** (it starts from your average cost; a stock
already below that stop gets a protective stop under the market instead, and an alert). Watch
Activity for a day: every order the guardian would place is listed as "Dry run, not sent".

**Step 2: orders on.** Set `enabled: true` and restart. The header shows *Real orders ON*, and the
confirm buttons read *Confirm LIVE buy*. Then:

- Make the first trade small: set the fixed loss to $50 in Settings for the first few trades.
- After the first fill, check in TWS that the stop and target are there, GTC, with the order
  reference `fp:stop:…` / `fp:target:…`, and in one OCA group.
- Remember the kill switch (*Circuit breakers*): it cancels working buys and pauses new ones,
  leaving every stop in place. **Release** on a position stops managing it.

**In Docker** the same two steps are `IB_MODE=live`, `IB_PORT=4003`, `IB_READ_ONLY_API=no` in
`deploy/.env`, plus `allow_live` / `enabled` in `config/config.yaml`.

**What changes nothing:** stops are stop orders, so a gap (earnings, news overnight) fills below
them. The fixed loss is a plan, not a guarantee.

### Check these on paper before going live

The test suite runs everything against a simulator. These depend on IBKR itself and can only
be confirmed against a real paper account:

- [ ] **London price units.** The guardian values London prices using IBKR's `priceMagnifier`.
  Buy one LSE stock on paper and check the dashboard's cost matches TWS. Before sending any buy,
  the service also asks IBKR for its own valuation (a what-if order) and refuses the order if the
  two disagree by more than 40%.
- [ ] **Hong Kong lots.** Check `trade check 700:HK` shows the right board lot; set it with
  `--lot` if not.
- [ ] **Stops on SEHK, SGX and TSE** are accepted as GTC (IBKR may simulate them) and appear in
  TWS with the `fp:stop:…` order reference.
- [ ] **A restart** (stop and start `trade run`) can still move its own stops. This needs the
  same `guardian.client_id` every time.
- [ ] **Exchange rates** come through (the check shows "Approximate exchange rate" when it falls
  back to the table in the config).
- [ ] **Market weather** shows a reading for all five markets. Each proxy (`ISF:LN`, `2800:HK`,
  `ES3:SG`, `1306:JP`) must resolve at IBKR with daily history; swap any that doesn't in
  `trading.weather`.
- [ ] **Sectors** come from IBKR's industry category. Check they group the way you think of your
  themes, and override any with the API (`POST /api/sector`).
- [ ] **An add** on paper moves the whole position's stop up, as the activity feed says.
- [ ] **Breadth baskets** resolve at IBKR: after the first hour the weather cards should read
  "30 of 30 big stocks" (US) and so on. A ticker IBKR lists differently (London names with a
  trailing dot, for example) is skipped; replace it under `trading.breadth.baskets`.

---

## The guardian

Every 30 seconds, and immediately after any fill, it compares the journal with what IBKR reports
and fixes the difference:

- **A new position**, whether bought on the dashboard, in TWS or in the phone app, gets a GTC stop
  for its full size and a GTC target for half, in one One-Cancels-All group.
- **The target fills:** its OCA partner (the stop) is cancelled by IBKR, and the guardian puts a
  new stop on the remaining half, trailing.
- **A stop is missing** (cancelled by hand, rejected, anything): a new one is placed and you get a
  critical alert.
- **A stop you tightened in TWS** is adopted, never loosened. A stop **you** placed covering the
  whole position is left alone.
- **A leftover order** whose position is gone is cancelled, so it can't sell a later position.
- **A buy that breaks a rule** (bucket full, locked stock) can't be blocked if it was placed
  outside the dashboard. It still gets its stop and target, plus an alert.

It will not:

- place or move a sell stop **at or above the market**. That would sell immediately. A stock
  already below its stop gets a protective stop just under the market and a critical alert.
- touch orders it didn't place, except to respect them.
- open a position. Buys only happen when you press **Confirm buy**.

What it can't prevent: a stop is a trigger, not a price guarantee. A stock that opens 15% lower
after earnings fills around -15%, not -5%. Stops trigger in regular hours only
(`outside_rth: false`).

Orders placed at IBKR stay live when the guardian is down. What stops is the ratchet (moving
stops up) and the repair of missing stops, so run it on an always-on machine.

---

## Market weather

Each market's weather is the analysis side's regime model blended with **breadth**, run once a
day per market:

- **Regime** (75%): the index proxy in `trading.weather`. For the US that's SPY with VIX,
  equal-weight breadth and the VIX curve; for the others (`ISF:LN`, `2800:HK`, `ES3:SG`,
  `1306:JP`), trend and drawdown.
- **Breadth** (25%, `breadth_weight`): the share of the market's big stocks above their 20, 50
  and 200-day averages (weighted 0.2 / 0.4 / 0.4), from a basket of 18–30 large constituents per
  market. An index can hold up on a few giants while most stocks fall; breadth shows that. The
  baskets are in `trading/breadth.py` and can be replaced under `trading.breadth.baskets`.

A new buy's fixed loss is multiplied by its market's label: Risk-On, Constructive and Neutral × 1,
Defensive × 0.5, Risk-Off × 0. Open positions are never touched by the weather; their stops do
that job.

**IBKR pacing.** IBKR allows about 60 historical-data requests per 10 minutes. Every stock's
daily bars are fetched once a day and reused for its daily range, its VWAP levels and breadth;
the basket is read 8 stocks per cycle; and when the budget runs short, requests wait for a later
cycle instead of failing. On a cold start the full picture fills in over roughly the first half
hour; positions come first.

## Anchored VWAP: where to buy a pullback

An anchored VWAP is the volume-weighted average price since a day that mattered: the cost basis
of everyone who bought because of it. Buyers defend it, so a pullback to it is a common swing
entry, and a price below it means those buyers are losing money and tend to sell into rallies.

Three anchors, from each stock's daily bars:

- **Earnings**: the report day, if you've entered its date, or else the most recent **gap day**
  (opened at least 4% away on twice the usual volume, usually the earnings reaction).
- **Breakout**: the first close above the prior 50 days' high after at least 10 quiet sessions,
  i.e. the start of the latest leg up (not simply yesterday's new high).
- **Swing low**: the lowest low of the last 60 sessions.

The watchlist shows each stock's **pullback level**: the nearest of these below the price, if it
is within three average days' range. The trade check lists all three with their distance, and
**Use as limit** re-checks the trade with a limit at that level. The stop is then set from that
lower price, so the same $250 fixed loss buys more shares. The check also warns when the price is
**below** a breakout or gap-up VWAP, and notes when it's **extended** with no support within reach.

Dashboard buys are day orders, so a pullback limit that doesn't fill today needs placing again.

## Earnings dates

IBKR's API has no free earnings calendar, so dates come from you (the date box on the watchlist,
or `trade earnings`) and, if you set `FMP_API_KEY`, from Financial Modeling Prep's free tier.
A date you set is never overwritten while it is in the future. Positions with a report within two
days get an alert: a stop can't protect against the gap.

## The journal

Every trade is kept with its setup tag (chosen when you buy), your note, its result in dollars
and in **R** (a full stop-out is −1R), how many days it was held, and its best and worst price
along the way. The dashboard grades the results by stock type and by setup: win rate, average win
and loss in R, and **expectancy**, the average R per trade. Above zero is an edge; with 30 or more
trades in a group it starts to mean something. `GET /api/journal.csv` downloads it all.

## Dashboard

`trade run` serves it on `127.0.0.1:8765`. From the top:

- **header:** mode (paper, LIVE, simulated), orders on or dry run, how many positions have a stop,
  and whether buys are paused
- **the book:** positions, invested vs the limit, open P/L, what every stop hitting would lose
- **market weather** per market (regime and breadth: % of big stocks above their 20/50/200-day), and
  **circuit breakers** with Pause / Resume / Kill switch
- **bucket mix:** money and daily swing per type, positions left per bucket, positions per sector
- **open positions:** stop (at entry, trailing, and what it risks), target, progress, flags
  (time stop, earnings, strikes, added) and **Add** / **Sell** buttons
- **check a new trade:** sizing, stop, target, the anchored-VWAP levels with **Use as limit**,
  every rule with its result, a setup tag and a note, then **Send → Confirm**
- **two-strike tracker** with **Unlock**, and **activity** (every order and alert)
- **journal:** expectancy for all trades, the last 7 and 30 days, by type and setup, and every
  closed trade
- **watchlist:** add or remove tickers, pullback level, confirm types, set earnings dates, see what
  is locked
- **settings:** time-stop weeks and gain, earnings window, fixed loss

**From your phone:** don't expose the port. Put it behind the Caddy proxy in `deploy/` (TLS and a
password), and also set `FIREPLANNER_DASH_TOKEN`, then open `https://your-host/?token=…` once.
With a token set, every API call needs it, and the page carries no data until it has it.

**Watchlist from IBKR:** the TWS API has no watchlist access, so there's no live sync. Export a
watchlist from TWS (right-click → *Export page content*) and `trade watch import` the file, or
add tickers on the dashboard. `trading.watchlist` in the config seeds it on first run.

## Hosting a copy

Both commands write one self-contained HTML file (inline styles and script, no outside
requests), so it works on any static host or straight from the disk:

```bash
fireplanner trade demo -o site/swing-desk/index.html       # the simulated demo book
fireplanner trade snapshot -o site/swing-desk/index.html   # YOUR book, read from the running dashboard
```

A hosted page is a **snapshot**: it shows the book as it was when written, and its buttons are
off. For a copy that stays current, run `trade snapshot` on a schedule (every 5 minutes, say)
and upload the file. `deploy/macos` and `deploy/fireplanner.timer` show the pattern, and
`--direct` reads IBKR itself when the dashboard isn't running (give it a
`--trade-client-id` the guardian isn't using).

**Your snapshot shows your positions, sizes and P/L, so never host it publicly.** Put it behind
a password: the Caddy basic-auth setup in `deploy/`, Netlify or Cloudflare Pages with access
control, or a private GitHub Pages site. The page also asks search engines not to index it.
The demo page carries a notice that its data is simulated, and is safe to share.

To *trade* from a phone, host the live dashboard instead: `trade run` behind Caddy with TLS, a
password and `FIREPLANNER_DASH_TOKEN` (see Dashboard above).

## Alerts

With `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` set (see the README's Telegram section), you
get a message for new positions, adds, exits, stop moves to entry and into trailing, time stops,
earnings within two days, the circuit breakers, and every warning or critical event, labelled
*Swing Desk*. `--no-notify` turns them off.

## Files

`data/trading/journal.sqlite` holds every trade, exit, alert, stock-type confirmation, unlock,
earnings date, setting and the watchlist. It is gitignored. Back it up: the two-strike rule and
the circuit breakers depend on it. An older journal is upgraded in place when opened.
