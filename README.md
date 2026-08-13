# FirePlanner

A swing/position trading stack for the S&P 500 and single names, wired to Interactive Brokers.
Daily bars only — nothing here assumes or requires intraday data.

It does four things:

1. **Pulls bars and account state from IBKR** through one interface with three transports.
2. **Computes indicators** — the full classic set, Wilder-correct, tested against brute force.
3. **Scores and sizes** — a regime-gated trend model with ATR-based position sizing.
4. **Renders a dashboard** — one self-contained HTML file, no server, no CDN.

```bash
pip install -e ".[gateway,cache,dev]"

fireplanner regime                                   # today's market gate
fireplanner scan SPY NVDA ZS MP FTNT                 # score a watchlist
fireplanner plan SPY --equity 77674                  # size a position
fireplanner backtest SPY                             # strategy vs buy-and-hold
fireplanner dashboard SPY NVDA ZS -o dashboard.html  # the full page
```

Every command runs out of the box against committed snapshots — real IBKR pulls frozen on
2026-08-13 — so you can evaluate the whole thing before connecting a broker. Add
`--provider gateway` to switch to live data.

---

## Connecting your IBKR account

Three transports, one `BarProvider` interface. The scanner, backtester, and dashboard only ever
see the interface, so you develop offline and flip one flag to go live.

| Transport | Use it when | Install |
|---|---|---|
| `snapshot` | offline work, tests, CI | built in |
| `gateway` | **the production path** — full history, order entry | `pip install "fireplanner[gateway]"` |
| `web` | no desktop app allowed; REST only | `pip install "fireplanner[web]"` |

### TWS / IB Gateway (recommended)

1. In TWS or IB Gateway: **Configure → API → Settings → Enable ActiveX and Socket Clients**.
2. Add `127.0.0.1` to trusted IPs.
3. Note the port — 7496 TWS live, 7497 TWS paper, 4001 Gateway live, 4002 Gateway paper.

```bash
fireplanner --provider gateway --port 4002 regime
```

```python
from fireplanner.data import GatewayProvider, CachedProvider

with GatewayProvider(port=4002, readonly=True) as gw:
    provider = CachedProvider(gw, throttle_seconds=0.3)   # respect IBKR pacing limits
    bars = provider.history("SPY", lookback_days=1260)
    positions = gw.positions()
```

`readonly=True` is the default and blocks order placement at the connection level. IBKR pacing
allows roughly 60 historical requests per 10 minutes, so wrap any multi-name scan in
`CachedProvider` — completed daily bars never change, which makes them safe to cache.

### Client Portal Web API

Run the CP gateway, log in at `https://localhost:5000`, then use `--provider web`. The session
expires after a few minutes idle; `WebApiProvider` re-tickles `/tickle` on every call.

### Your account state stays out of git

Live balances and positions are written to `data/snapshots/account.json`, which is **gitignored** —
portfolio sizes and net liquidation do not belong in version history, and git history is not
something you can quietly take back. The committed `account.example.json` carries the same schema
with synthetic quantities so a fresh clone still renders the full dashboard. `SnapshotProvider`
prefers the real file when present and falls back to the example.

### Market data entitlements

Historical daily bars are generally available without a market-data subscription, which is what a
non-intraday system needs. Live quotes may return delayed or empty without one.

---

## What the model actually is

A **trend-following system with a pullback entry filter, gated by market regime**. Not a
predictor. Two independent decisions:

**The gate** decides *how much you may risk*, from four weighted components on the index:

| Component | Weight | Reads |
|---|---|---|
| Trend | 40% | price vs 50/200-day, MA stacking, 200-day slope |
| Volatility | 25% | VIX level and its own trailing percentile |
| Participation | 20% | equal-weight (RSP) vs cap-weight (SPY) — is the advance broad? |
| Drawdown | 15% | distance below the 52-week closing high |
| Term structure | **0%** | VIX3M/VIX curve shape — computed, but see below |

Score → label → a hard cap on deployable equity: Risk-On 100%, Constructive 75%, Neutral 50%,
Defensive 25%, Risk-Off 0%.

**The score** decides *where*, per name, 0–100:

| Block | Weight | Question |
|---|---|---|
| Trend | 35% | Is it in an uptrend at all? |
| Momentum | 30% | Is that trend being paid for now? |
| Entry timing | 20% | Is *today* sane, or is it extended? |
| Quality | 15% | Tradeable — sized, liquid, not broken? |

The **entry block is what makes it a swing system rather than a momentum chase**. A name at
RSI 85, three ATRs above its 21-EMA, scores *worse* than the same name resting on that anchor.
The edge is in the pullback, not the spike.

**Sizing** puts the stop first: stop = entry − 2.5 × ATR, then shares = whatever makes that
distance cost exactly 0.75% of equity. Every position risks the same dollar amount. Capped by
position weight, by total open risk ("portfolio heat"), and by the regime's exposure ceiling —
and `limited_by` tells you which one bound.

---

## VIX term structure, and why its gate weight is zero

`VIX3M / VIX` is the shape of the volatility curve. Above 1.0 is **contango** (calm); below 1.0 is
**backwardation** — traders paying more for protection now than in three months, which is what acute
panic looks like. Bucketing forward SPY returns by it over the shipped five years is cleanly
monotonic:

| VIX3M/VIX | Forward 21d SPY | Sessions |
|---|---|---|
| < 0.95 *(deep backwardation)* | **+6.70%** | 15 |
| 0.95 – 1.00 | +3.82% | 50 |
| 1.00 – 1.05 | +2.17% | 185 |
| 1.05 – 1.10 | +1.03% | 223 |
| 1.10 – 1.15 | +0.87% | 283 |
| > 1.15 *(steep contango)* | +0.10% | 476 |

Note the sign: **backwardation is bullish**, not bearish. It marks the bottom far more often than
the top, and reading it the intuitive way round would make the model worse.

So it is a real signal. But giving it weight *inside the gate* degraded the model monotonically:

| Regime weights | CAGR | Sharpe | Max DD |
|---|---|---|---|
| trend .40 / breadth .20, no term | 2.02% | **1.18** | −1.57% |
| trend .35 / breadth .15, no term | 1.72% | 0.96 | −2.07% |
| trend .35 / breadth .15 / term .10 | 1.09% | 0.69 | −2.42% |

That is a job mismatch, not a bad signal. The gate authorizes *trend-following* entries; term
structure is a *mean-reversion* signal that peaks when trend structure is at its worst. Blending
them dilutes the components that make the gate work and buys entries into downtrends that stop out.

**So `term` is weighted 0 in the score by default and earns its keep as the fast re-entry trigger**
(`BacktestConfig.fast_reentry`), where it improved max drawdown from −1.57% to −1.21% at unchanged
Sharpe. Right signal, right place. Raise `REGIME_WEIGHTS["term"]` only if you re-measure it.

---

## Missing the best days — the measured cost, and the fix

A rule that goes to cash risks missing the market's best sessions. On the shipped sample the purely
tactical rule captured **0 of SPY's 20 best days** while avoiding **20 of 20 worst** — forgoing
**+64.4%** of upside to dodge **−63.8%** of downside. It swaps one tail for the other almost exactly,
then pays costs and sits in cash. That is the whole mechanism behind its underperformance.

The best days hide where a defensive filter refuses to hold:

- **75%** of the top-20 days happened below the 200-day average, against 22% of all sessions.
- Average drawdown on a best day was **−15.0%**, versus −6.3% typically.
- **11 of the 20** best days fell within five sessions of a bottom-20 day.

You cannot dodge one tail without standing next to the other. What actually works:

| Configuration | CAGR | Vol | Sharpe | Max DD | Best days | Upside forgone |
|---|---|---|---|---|---|---|
| Tactical only | 2.02% | 1.71% | **1.18** | −1.57% | 0/20 | −64.4% |
| + term re-entry | 2.09% | 1.78% | **1.18** | −1.21% | 0/20 | −64.4% |
| + 40% core | 9.20% | 7.97% | 1.15 | −9.39% | **20/20** | 0.0% |
| + 60% core | 12.32% | 11.13% | 1.10 | −12.81% | **20/20** | 0.0% |
| Buy & hold | 16.08% | 16.73% | 0.98 | −19.00% | 20/20 | 0.0% |

1. **Hold a permanent core** (`--core-weight 0.4`). The only structural fix — a core is exposed to
   every up day by construction, so best-day capture goes to 20/20 and forgone return to zero. The
   tactical sleeve then adds and removes risk *around* it instead of switching the book off.
2. **Re-enter faster than you exit** (`fast_reentry`, on by default). Waiting for the 50-day to be
   reclaimed guarantees you are flat through the rebound.
3. **Don't read the vol curve backwards** — see the section above.

Every core variant beats buy-and-hold on **both** Sharpe and drawdown while giving up CAGR in
proportion to how much it holds. There is no free lunch in that table, only an explicit choice about
where on the frontier you want to sit.

```bash
fireplanner backtest SPY --core-weight 0.4    # prints best/worst day capture too
```

---

## Honest results

`fireplanner backtest SPY`, five years of IBKR daily bars, signals on the close, fills at the
next open, gap-aware stops, commission and slippage charged both ways:

| | Strategy | Buy & hold |
|---|---|---|
| CAGR | 2.02% | **16.08%** |
| Volatility | 1.71% | 16.73% |
| Sharpe | **1.18** | 0.98 |
| Max drawdown | **−1.57%** | −19.00% |
| Time in market | 31.3% | 100% |

**Read that honestly.** The model does **not** beat owning the index. It holds 31% of the time
and risks 0.75% per trade, so it captures roughly its time-in-market share of the move. The low
volatility is mostly an artifact of being in cash, not skill — and as the section above shows, that
cash position is exactly what costs it the best days. If you want this to be a primary allocation
rather than a hedge overlay, run it with `--core-weight 0.4` or higher.

The edge that *is* real is risk-adjusted: levered to buy-and-hold's volatility the strategy
compounds at **20.1% vs 16.1%** with a **−14.6% vs −19.0%** drawdown. But that leverage costs
financing this test does not charge, so treat it as an upper bound.

Two further cautions, both measured rather than assumed:

- **The sample flatters trend-following.** 2021–2026 contains one real bear phase. Any regime
  filter tuned on it looks better here than it will live.
- **The regime bands are not monotonic.** Sorting forward 21-day SPY returns by regime label,
  only *Risk-Off* actually preceded losses (−2.2% mean). *Defensive* readings had the **best**
  forward returns (+2.9%) — buy-the-dip dominated this period. Use the middle bands as
  position-sizing input, not as exit signals.

---

## Dashboard

`fireplanner dashboard -o dashboard.html` writes one self-contained file: inline SVG charts,
inline CSS/JS, zero external requests, works from `file://` and under a strict CSP. Light and
dark are both explicitly designed, with hover crosshairs and a table view behind every chart.

Panels: regime banner with component meters · indicator tiles with sparklines · price against
its trend anchors · two years of regime score · signal breakdown · a trade plan sized to your
real net liquidation · watchlist scorecard · your live book with concentration and headroom vs
the regime cap · backtest evidence with the caveats above printed next to the numbers.

---

## Layout

```
src/fireplanner/
  indicators/    core.py    SMA/EMA/RMA, ATR, RSI, MACD, ADX, Supertrend, Bollinger,
                            Keltner, Donchian, Aroon, OBV, MFI, CMF, realized vol
                 regime.py  trend/vol/breadth state, composite regime, relative strength
  data/          base.py    the BarProvider contract + bar normalization
                 providers.py  Snapshot / Gateway (ib_async) / WebApi (CP REST)
                 cache.py   on-disk parquet cache with pacing-aware fallback
  signals/       model.py   the four-block score and the action rules
  risk/          sizing.py  ATR sizing, portfolio heat, vol targeting
  backtest/      engine.py  daily engine, next-open fills, gap-aware stops
                 metrics.py CAGR/Sharpe/Sortino/Calmar/drawdown + buy-and-hold benchmark
  dashboard/     build.py, render.py, charts.py
tests/           37 tests
data/snapshots/  real IBKR pulls: SPY, VIX, RSP, NVDA, ZS, MP, FTNT + account state
```

## Correctness

```bash
PYTHONPATH=src python3 -m pytest tests/ -q      # 37 passed
```

The tests that matter most:

- **RSI, ATR, SMA and Bollinger are checked against explicit loop implementations**, not against
  themselves. A rounding convention error here would silently corrupt every score downstream.
- **`test_indicators_do_not_look_ahead`** truncates the series and asserts no already-computed
  value changes. If this fails, every backtest is fiction.
- **Stop fills are asserted never better than the stop**, so a gap-down cannot be filled at a
  price the market never offered.

### One data caveat worth knowing

IBKR's SMART-aggregated daily bars occasionally report a close a cent or two outside the
session's own high/low — the consolidated closing-auction print lands outside the range built
from the aggregated intraday feed. It affects ~1% of SPY sessions (2022-03-17: high 441.02,
close 441.07). Left alone it corrupts true range, ATR stops, %B and Donchian breaks, so
`normalize_bars` widens the bar to contain its own open and close. The repair never narrows a
range and is bounded by the discrepancy.

---

## Extending it

- **Real breadth.** `universe_breadth()` takes a wide frame of closes and returns % above 50/200-day.
  Feed it actual S&P 500 constituents rather than the RSP/SPY proxy.
- **Order staging.** `GatewayProvider` connects `readonly=True`. Bracket orders from
  `PositionPlan` (entry + ATR stop + R-multiple targets) are the natural next step.
- **Core rebalancing.** The core is currently bought once and held. A quarterly rebalance back to
  target weight would be more realistic, and would let the tactical sleeve harvest into strength.

---

*Indicators, regime, scores and sizing are computed locally from your own broker data. Nothing
here is a recommendation, a forecast, or investment advice, and backtested results are not a
promise about future returns.*
