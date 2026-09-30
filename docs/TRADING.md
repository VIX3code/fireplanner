# Trade management: the Swing Desk

The analysis side of FirePlanner tells you what the market is doing. This side manages a
position from the moment you buy it: it puts a GTC stop and a target on it at IBKR, moves the
stop up as the trade works, keeps the book from piling into volatile names, and remembers every
stop-out.

```bash
fireplanner trade demo -o swing_desk.html        # the dashboard on a simulated book, no IBKR needed
fireplanner --port 4002 trade once --dry-run     # one pass against your paper account, sends nothing
fireplanner --port 4002 trade run                # guardian + live dashboard at http://127.0.0.1:8765
fireplanner --port 4002 trade check 700:HK       # the pre-trade check for one stock
fireplanner trade watch add NVDA D05:SG 7203:JP  # edit the watchlist
fireplanner trade watch import tws_export.csv    # import a TWS watchlist export
fireplanner trade bucket NVDA volatile           # confirm a stock's type
```

---

## The rules

All of them live in the `trading:` section of `config/config.yaml`.

| Rule | Setting |
|---|---|
| Position size | **$5,000** per trade, converted to the stock's currency, rounded **down** to whole shares or board lots |
| Book limit | **20** open positions |
| Stop | GTC, the **tighter of 5% or 2.5 × the average daily range** below entry (max loss ≈ $250 before gaps) |
| Stock types | **Steady** (moves < 2% a day, target +10%, cap 20) · **Core** (2–3.5%, +15%, cap 10) · **Volatile** (> 3.5%, +20%, cap 5) |
| At the target | sell **half**, trail the rest |
| Stop ratchet | to the **entry price** once the stock has been +5%, then **3 × daily range** under the high since entry. Never down. |
| Two strikes | after a stop-out you may re-enter once; a **second stop-out in a row locks the stock for 10 trading days** |
| One per stock | no second position in a stock you already hold |
| Swing guide | a **warning** when Volatile would drive more than 50% of the book's daily swing |

The stock type is **suggested** from the 14-day average daily range and **confirmed** by you on
the dashboard (or with `trade bucket`). A confirmed type moves an open position's target.

### Why two allocation bars

Every slot is $5,000, so a book can look balanced by money and still get half its day-to-day
movement from its Volatile names: a Volatile stock moves about three times as much in a day as
a Steady one. The dashboard shows **share of money** and **share of daily swing**
(value × daily range) side by side. The bucket caps block on position counts; the swing guide
only warns.

---

## Five markets

| Market | Write it as | Currency | Lot | Notes |
|---|---|---|---|---|
| US | `NVDA` | USD | 1 | SMART routing |
| London | `HSBA:LN` | GBP | 1 | prices in **pence**; value = qty × price ÷ 100 |
| Hong Kong | `700:HK` | HKD | **per stock** | set a lot IBKR doesn't report: `trade watch add 700:HK --lot 100` |
| Singapore | `D05:SG` | SGD | 100 | |
| Tokyo | `7203:JP` | JPY | 100 | |

- **Board lots.** Sizes round down to whole lots, so a position never exceeds its slot. A stock
  whose single lot costs more than $5,000 is blocked for that reason (the check says so).
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

## Getting started, paper first

1. **Look at the demo.** `fireplanner trade demo -o swing_desk.html` runs a sample book
   through the real code on a simulated broker. Nothing about it is drawn by hand.
2. **Paper account, orders off.** In IB Gateway (paper, port 4002) turn **off**
   *Configure → Settings → API → Read-Only API*, since the guardian has to place orders. Then:
   ```bash
   pip install -e ".[gateway]"
   fireplanner --port 4002 trade once --dry-run
   ```
   Every position is listed with the stop and target the guardian *would* place.
3. **Run it in dry run for a few days:** `fireplanner --port 4002 trade run`. With
   `trading.enabled: false` (the default) every order is logged on the dashboard as
   "Dry run, not sent". Buy a few things on paper and watch what it would do.
4. **Turn orders on, still on paper:** set `trading.enabled: true`. Now stops and targets are
   placed for real on the paper account. Test a buy from the dashboard, one from TWS, and one
   from the IBKR phone app; cancel a stop by hand and watch it come back.
5. **Live.** Only after the paper checklist below passes: set `trading.allow_live: true` and use
   port 4001. Without that flag, live ports are refused at startup.

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

## Dashboard

`trade run` serves it on `127.0.0.1:8765`. Panels:

- **header:** mode (paper, LIVE, simulated), orders on or dry run, and how many positions have a stop
- **the book:** slots, invested, open P/L, what you would lose if every stop hit
- **bucket mix:** money and daily swing per type, slots left per bucket
- **open positions:** entry, last, stop (and whether it is at entry or trailing), target, progress, protection
- **check a new trade:** sizing, stop, target, every rule with its result, then **Send buy order →
  Confirm buy**
- **watchlist:** add or remove tickers, confirm types, see what is locked
- **two-strike tracker** and **activity** (every order and alert)

**From your phone:** don't expose the port. Put it behind the Caddy proxy in `deploy/` (TLS and a
password), and also set `FIREPLANNER_DASH_TOKEN`, then open `https://your-host/?token=…` once.
With a token set, every API call needs it.

**Watchlist from IBKR:** the TWS API has no watchlist access, so there's no live sync. Export a
watchlist from TWS (right-click → *Export page content*) and `trade watch import` the file, or
add tickers on the dashboard. `trading.watchlist` in the config seeds it on first run.

## Alerts

With `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` set (see the README's Telegram section), you
get a message for new positions, exits, stop moves to entry and into trailing, and every warning
or critical event, labelled *Swing Desk*. `--no-notify` turns them off.

## Files

`data/trading/journal.sqlite` holds every trade, exit, alert, stock-type confirmation and the
watchlist. It is gitignored. Back it up: the two-strike rule depends on it.
