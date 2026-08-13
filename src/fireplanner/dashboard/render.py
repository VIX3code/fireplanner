"""Render a `DashboardPayload` into one self-contained HTML page.

No external requests: styles, script, and every chart are inline, so the file
opens from disk, survives a strict CSP, and can be emailed as a single artifact.
Light and dark are both selected explicitly — the dark palette is its own set of
steps validated against the dark surface, not an automatic inversion.
"""

from __future__ import annotations

import html

import numpy as np
import pandas as pd

from .charts import Series, fmt, hbar_chart, line_chart, meter, sparkline

__all__ = ["render_html"]


STATUS_FOR_LABEL = {
    "Risk-On": "good",
    "Constructive": "good",
    "Neutral": "warning",
    "Defensive": "serious",
    "Risk-Off": "critical",
    "Unknown": "muted",
}

ACTION_STATUS = {"BUY": "good", "ADD": "good", "HOLD": "warning", "REDUCE": "serious", "AVOID": "critical"}


def _tile(label: str, value: str, sub: str = "", spark: str = "", status: str | None = None) -> str:
    badge = f'<span class="dot dot-{status}"></span>' if status else ""
    return (
        f'<div class="tile"><div class="tile-label">{badge}{html.escape(label)}</div>'
        f'<div class="tile-value">{value}</div>'
        f'<div class="tile-sub">{sub}</div>{spark}</div>'
    )


def _table(headers: list[str], rows: list[list[str]], cls: str = "") -> str:
    head = "".join(f"<th>{html.escape(h)}</th>" for h in headers)
    body = "".join("<tr>" + "".join(f"<td>{c}</td>" for c in r) + "</tr>" for r in rows)
    return f'<table class="{cls}"><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>'


def _details_table(summary: str, headers: list[str], rows: list[list[str]]) -> str:
    """The table-view twin every chart ships with."""
    return (
        f'<details class="tableview"><summary>{html.escape(summary)}</summary>'
        f"{_table(headers, rows, 'data')}</details>"
    )


def _sign(v: float, digits: int = 2, suffix: str = "%") -> str:
    if v is None or (isinstance(v, float) and np.isnan(v)):
        return "—"
    cls = "pos" if v > 0 else ("neg" if v < 0 else "")
    return f'<span class="{cls}">{"+" if v > 0 else ""}{fmt(v, digits)}{suffix}</span>'


def render_html(p, title: str = "FirePlanner") -> str:
    """Build the complete page for a payload."""
    bench = p.benchmark
    e = p.enriched.get(bench)
    reg = p.regime
    rn = p.regime_now or {}
    label = rn.get("label", "Unknown")
    status = STATUS_FOR_LABEL.get(label, "muted")

    last = e.iloc[-1] if e is not None else None
    quote = (p.quotes or {}).get(bench, {})

    sections: list[str] = []

    # ---------------------------------------------------------------- header
    sections.append(
        f"""
<header class="page-head">
  <div>
    <h1>{html.escape(title)}</h1>
    <p class="sub">Swing &amp; position dashboard · {html.escape(bench)} and single names ·
       daily bars, no intraday · data via Interactive Brokers</p>
  </div>
  <div class="asof"><span class="asof-label">Bars through</span><span class="asof-date">{p.as_of}</span></div>
</header>"""
    )

    # ---------------------------------------------------------------- regime
    comps = rn.get("components", {})
    meters = "".join(
        meter(comps.get(k), name, status)
        for k, name in [
            ("trend", "Trend structure"),
            ("breadth", "Participation"),
            ("vol", "Volatility"),
            ("drawdown", "Drawdown"),
        ]
        if comps.get(k) is not None
    )
    cap = rn.get("exposure_cap", 0.0)
    sections.append(
        f"""
<section class="card regime regime-{status}">
  <div class="regime-verdict">
    <span class="regime-eyebrow">Market regime</span>
    <span class="regime-label">{html.escape(label)}</span>
    <span class="regime-score">{fmt(rn.get('score', 0) * 100, 0)}<span class="of">/100</span></span>
    <span class="regime-cap">Max deployable equity <strong>{fmt(cap * 100, 0)}%</strong></span>
  </div>
  <div class="regime-meters">{meters}</div>
  <p class="regime-note">The gate, not a forecast. It decides how much you are allowed to risk;
     the per-name score decides where. Both are recomputed from the close each session.</p>
</section>"""
    )

    # ---------------------------------------------------------------- tiles
    tiles = []
    if last is not None:
        close = float(last["close"])
        px_sub = f"52w high {fmt(quote.get('high_52w'), 2)}" if quote.get("high_52w") else ""
        tiles.append(
            _tile(
                f"{bench} close",
                fmt(close, 2),
                px_sub or f"{_sign(float(last['dist_52w_high']))} from 52w high",
                sparkline(e["close"].tail(120), color_role="series-1"),
            )
        )
        tiles.append(
            _tile(
                "Trend",
                f"{fmt(last['pct_from_sma50'], 1)}% / {fmt(last['pct_from_sma200'], 1)}%",
                "above 50d / 200d SMA",
                status="good" if last["close"] > last["sma200"] else "critical",
            )
        )
        tiles.append(
            _tile(
                "RSI(14)",
                fmt(last["rsi14"], 1),
                "overbought >70" if last["rsi14"] > 70 else ("oversold <30" if last["rsi14"] < 30 else "neutral band"),
                sparkline(e["rsi14"].tail(120), color_role="series-2"),
                status="warning" if last["rsi14"] > 70 or last["rsi14"] < 30 else "good",
            )
        )
        tiles.append(
            _tile(
                "ADX(14)",
                fmt(last["adx"], 1),
                "trend worth following" if last["adx"] >= 20 else "chop — trend signals unreliable",
                sparkline(e["adx"].tail(120), color_role="series-3"),
                status="good" if last["adx"] >= 20 else "warning",
            )
        )
        tiles.append(
            _tile(
                "ATR(14)",
                f"{fmt(last['natr14'], 2)}%",
                f"{fmt(last['atr14'], 2)} pts — the unit your stop is measured in",
                sparkline(e["natr14"].tail(120), color_role="series-2"),
            )
        )
        if reg is not None and "vix" in reg.columns:
            vix_now = float(reg["vix"].dropna().iloc[-1])
            vix_pct = reg["vix_pctile"].dropna()
            pct_txt = f"{fmt(float(vix_pct.iloc[-1]) * 100, 0)}th pct of past year" if len(vix_pct) else ""
            tiles.append(
                _tile(
                    "VIX",
                    fmt(vix_now, 2),
                    pct_txt,
                    sparkline(reg["vix"].tail(120), color_role="series-2"),
                    status="good" if vix_now < 20 else ("warning" if vix_now < 28 else "critical"),
                )
            )
        tiles.append(
            _tile(
                "Realized vol 20d",
                f"{fmt(last['rvol20'], 1)}%",
                "annualized, from daily log returns",
                sparkline(e["rvol20"].tail(120), color_role="series-3"),
            )
        )
        tiles.append(
            _tile(
                "Supertrend",
                "Up" if last["supertrend_dir"] > 0 else "Down",
                f"stop line {fmt(last['supertrend'], 2)}",
                status="good" if last["supertrend_dir"] > 0 else "critical",
            )
        )
    sections.append(f'<section class="tiles">{"".join(tiles)}</section>')

    # ---------------------------------------------------------------- price chart
    if e is not None:
        window = e.tail(252)
        chart = line_chart(
            [
                Series(bench, window["close"], "series-1", 2.0),
                Series("50d SMA", window["sma50"], "series-2", 1.5),
                Series("200d SMA", window["sma200"], "series-3", 1.5),
            ],
            chart_id="price",
            y_digits=0,
            height=290,
        )
        rows = [
            [d.strftime("%Y-%m-%d"), fmt(r["close"], 2), fmt(r["sma50"], 2), fmt(r["sma200"], 2)]
            for d, r in window.tail(12).iterrows()
        ]
        sections.append(
            f"""
<section class="card">
  <h2>{html.escape(bench)} — price against its trend anchors</h2>
  <p class="lede">Twelve months of daily closes. Price above a rising 200-day with the 50-day
     stacked over it is the structural condition the model requires before any long.</p>
  {chart}
  {_details_table("Table view — last 12 sessions", ["Date", "Close", "50d SMA", "200d SMA"], rows)}
</section>"""
        )

    # ---------------------------------------------------------------- regime history
    if reg is not None and reg["score"].notna().any():
        rwin = reg.dropna(subset=["score"]).tail(504)
        chart = line_chart(
            [Series("Regime score", rwin["score"] * 100, "series-1", 2.0)],
            chart_id="regime",
            y_digits=0,
            height=210,
            bands=[(80, 100, "status-good"), (0, 20, "status-critical")],
        )
        rows = [
            [d.strftime("%Y-%m-%d"), fmt(r["score"] * 100, 0), str(r["label"]), fmt(r["exposure_cap"] * 100, 0) + "%"]
            for d, r in rwin.tail(12).iterrows()
        ]
        sections.append(
            f"""
<section class="card">
  <h2>Regime score, two years</h2>
  <p class="lede">Green band is risk-on (≥80), red is risk-off (≤20). The exposure cap steps down
     with the score, so the book de-risks before you have to decide anything.</p>
  {chart}
  {_details_table("Table view — last 12 sessions", ["Date", "Score", "Label", "Exposure cap"], rows)}
</section>"""
        )

    # ---------------------------------------------------------------- signal + plan
    sig = next((s for s in p.signals if s["symbol"] == bench), None)
    if sig:
        blocks = sig.get("blocks", {})
        bmeters = "".join(
            meter(blocks.get(k), name, ACTION_STATUS.get(sig["action"], "series-1"))
            for k, name in [
                ("trend", "Trend  35%"),
                ("momentum", "Momentum  30%"),
                ("entry", "Entry timing  20%"),
                ("quality", "Quality  15%"),
            ]
            if blocks.get(k) is not None
        )
        plan = p.trade_plan or {}
        plan_rows = []
        if plan.get("shares"):
            tgt = plan.get("r_multiple_targets", {})
            plan_rows = [
                ["Entry (last close)", fmt(plan["entry"], 2)],
                ["Initial stop (2.5 × ATR)", f"{fmt(plan['stop'], 2)} &nbsp;<span class='muted-txt'>"
                 f"−{fmt(100 * (1 - plan['stop'] / plan['entry']), 2)}%</span>"],
                ["Shares", fmt(plan["shares"], 0)],
                ["Notional", "$" + fmt(plan["notional"], 0)],
                ["Risk if stopped", "$" + fmt(plan["risk_dollars"], 0) +
                 f" &nbsp;<span class='muted-txt'>{fmt(plan['risk_pct_equity'] * 100, 2)}% of equity</span>"],
                ["Position weight", fmt(plan["weight"] * 100, 1) + "%"],
                ["Targets 1R / 2R / 3R", " · ".join(fmt(tgt.get(k), 2) for k in ["1R", "2R", "3R"])],
                ["Binding constraint", f"<code>{html.escape(plan.get('limited_by', '—'))}</code>"],
            ]
        plan_html = (
            _table(["", ""], plan_rows, "kv")
            if plan_rows
            else '<p class="muted-txt">No position staged — the model is not asking for one here.</p>'
        )
        sections.append(
            f"""
<section class="grid-2">
  <div class="card">
    <h2>{html.escape(bench)} signal</h2>
    <div class="score-head">
      <span class="score-num">{fmt(sig['score'], 0)}<span class="of">/100</span></span>
      <span class="action action-{ACTION_STATUS.get(sig['action'], 'muted')}">{html.escape(sig['action'])}</span>
    </div>
    <div class="regime-meters">{bmeters}</div>
    <p class="lede">Entry timing is scored <em>against</em> extension: a name three ATRs above its
       21-day anchor loses points it would keep sitting on that anchor. The swing edge is in the
       pullback, not the spike.</p>
  </div>
  <div class="card">
    <h2>Trade plan at your equity</h2>
    <p class="lede">Sized from your live net liquidation, risking
       {fmt((p.account.get('summary', {}) or {}).get('risk_pct', 0.75), 2) if False else '0.75'}% per position
       with the stop set by volatility rather than a round percentage.</p>
    {plan_html}
  </div>
</section>"""
        )

    # ---------------------------------------------------------------- watchlist
    if p.signals:
        rows = []
        for s in p.signals:
            d = s.get("detail", {})
            act = s["action"]
            rows.append(
                [
                    f"<strong>{html.escape(s['symbol'])}</strong>",
                    f'<span class="action-pill action-{ACTION_STATUS.get(act, "muted")}">{html.escape(act)}</span>',
                    f"<strong>{fmt(s['score'], 0)}</strong>",
                    fmt(s["close"], 2),
                    fmt(d.get("rsi14"), 1),
                    fmt(d.get("adx"), 1),
                    fmt(d.get("natr14"), 2) + "%",
                    _sign(d.get("pct_from_sma50"), 1),
                    _sign(d.get("dist_52w_high"), 1),
                    "Up" if (d.get("supertrend_dir") or 0) > 0 else "Down",
                ]
            )
        sections.append(
            f"""
<section class="card">
  <h2>Watchlist scorecard</h2>
  <p class="lede">Every column here is derived from daily OHLCV the IBKR API already returns —
     no alternative data, no fundamentals feed.</p>
  {_table(
      ["Symbol", "Action", "Score", "Close", "RSI", "ADX", "ATR%", "vs 50d", "vs 52w high", "Supertrend"],
      rows, "data wide")}
</section>"""
        )

    # ---------------------------------------------------------------- portfolio
    if p.positions is not None and not p.positions.empty:
        pos = p.positions
        summary = p.account.get("summary", {})
        net_liq = float(summary.get("net_liquidation", 0) or 0)
        gross = float(summary.get("gross_position_value", pos["market_value"].sum()))
        cash = float(summary.get("total_cash_value", 0) or 0)
        top = pos.head(12)
        sym_col = "symbol" if "symbol" in pos.columns else "contract_description"

        bars = hbar_chart(
            top[sym_col].astype(str).tolist(),
            top["weight_pct"].astype(float).tolist(),
            suffix="%",
            digits=1,
        )
        prows = [
            [
                f"<strong>{html.escape(str(r[sym_col]))}</strong>",
                fmt(r.get("position"), 2),
                fmt(r.get("market_price"), 2),
                "$" + fmt(r.get("market_value"), 0),
                fmt(r.get("weight_pct"), 2) + "%",
                _sign(r.get("pnl_pct"), 1),
                "$" + fmt(r.get("unrealized_pnl"), 0),
            ]
            for _, r in pos.iterrows()
        ]
        gross_pct = 100.0 * gross / net_liq if net_liq else 0.0
        cap_pct = float(rn.get("exposure_cap", 0)) * 100
        headroom = cap_pct - gross_pct
        conc = float(pos["weight_pct"].head(5).sum())

        sections.append(
            f"""
<section class="card">
  <h2>Your book</h2>
  <p class="lede">Live positions from the account. Weights are against net liquidation, so they
     account for the cash you are holding.</p>
  <div class="grid-2 tight">
    <div>
      <h3>Largest positions by weight</h3>
      {bars}
    </div>
    <div>
      <h3>Exposure</h3>
      {_table(["", ""], [
        ["Net liquidation", "$" + fmt(net_liq, 0)],
        ["Gross positions", "$" + fmt(gross, 0) + f" &nbsp;<span class='muted-txt'>{fmt(gross_pct, 1)}% of equity</span>"],
        ["Cash", "$" + fmt(cash, 0)],
        ["Regime exposure cap", fmt(cap_pct, 0) + "%"],
        ["Headroom vs cap",
         f"<span class='{'pos' if headroom >= 0 else 'neg'}'>{fmt(headroom, 1)}%</span>"],
        ["Top-5 concentration", fmt(conc, 1) + "% of equity"],
        ["Positions", str(len(pos))],
      ], "kv")}
    </div>
  </div>
  {_details_table("All positions",
      ["Symbol", "Qty", "Price", "Market value", "Weight", "P&L %", "Unrealized"], prows)}
</section>"""
        )

    # ---------------------------------------------------------------- backtest
    bt = p.backtest or {}
    if bt:
        st, bh = bt["stats"], bt["buy_hold"]
        chart = line_chart(
            [
                Series("Strategy", bt["strategy_indexed"], "series-1", 2.0),
                Series("Buy & hold", bt["bh_equity"], "series-2", 1.5),
            ],
            chart_id="equity",
            y_digits=0,
            height=250,
        )
        cmp_rows = [
            ["CAGR", fmt(st.get("cagr_pct"), 2) + "%", fmt(bh.get("cagr_pct"), 2) + "%"],
            ["Volatility", fmt(st.get("vol_pct"), 2) + "%", fmt(bh.get("vol_pct"), 2) + "%"],
            ["Sharpe", fmt(st.get("sharpe"), 2), fmt(bh.get("sharpe"), 2)],
            ["Sortino", fmt(st.get("sortino"), 2), fmt(bh.get("sortino"), 2)],
            ["Max drawdown", fmt(st.get("max_drawdown_pct"), 2) + "%", fmt(bh.get("max_drawdown_pct"), 2) + "%"],
            ["Calmar", fmt(st.get("calmar"), 2), fmt(bh.get("calmar"), 2)],
            ["Time in market", fmt(st.get("exposure_pct"), 1) + "%", "100.0%"],
            ["Trades", str(st.get("trades", 0)), "1"],
            ["Win rate", fmt(st.get("win_rate_pct"), 1) + "%", "—"],
            ["Profit factor", fmt(st.get("profit_factor"), 2), "—"],
            ["Avg hold", fmt(st.get("avg_hold_days"), 1) + " days", "—"],
        ]
        trades = bt.get("trades")
        trows = []
        if trades is not None and not trades.empty:
            for _, t in trades.tail(15).iterrows():
                trows.append(
                    [
                        str(pd.Timestamp(t["entry_date"]).date()),
                        str(pd.Timestamp(t["exit_date"]).date()),
                        fmt(t["entry"], 2),
                        fmt(t["exit"], 2),
                        _sign(t["return_pct"], 2),
                        str(int(t["hold_days"])),
                        f"<code>{html.escape(str(t['reason']))}</code>",
                    ]
                )

        sections.append(
            f"""
<section class="card">
  <h2>Does the model actually earn its complexity?</h2>
  <p class="lede">Signals computed on the close, filled at the next open, gap-aware stops,
     commission and slippage charged both ways. Both lines are indexed to 100 at the start so
     they share one axis.</p>
  {chart}
  <div class="grid-2 tight">
    <div>{_table(["Metric", "Strategy", "Buy & hold"], cmp_rows, "data")}</div>
    <div class="verdict">
      <h3>Read this before trusting the numbers</h3>
      <ul>
        <li><strong>It does not beat owning the index outright.</strong> {fmt(st.get('cagr_pct'), 1)}% CAGR
            against {fmt(bh.get('cagr_pct'), 1)}% — because it holds
            {fmt(st.get('exposure_pct'), 0)}% of the time and risks 0.75% per trade, it captures roughly
            its time-in-market share of the move.</li>
        <li><strong>The edge that is real is risk-adjusted.</strong> Sharpe
            {fmt(st.get('sharpe'), 2)} vs {fmt(bh.get('sharpe'), 2)}, drawdown
            {fmt(st.get('max_drawdown_pct'), 1)}% vs {fmt(bh.get('max_drawdown_pct'), 1)}%.
            Levered to the same volatility as buy-and-hold it compounds faster — but that leverage
            costs financing this test does not charge.</li>
        <li><strong>The sample flatters trend-following.</strong> The window contains one real
            bear phase. A regime filter tuned on it will look better here than it will live.</li>
        <li><strong>Only "Risk-Off" genuinely preceded losses</strong> in this sample.
            "Defensive" readings had the <em>best</em> forward 21-day returns — buy-the-dip dominated
            this period, so treat the middle bands as position-sizing input, not exit signals.</li>
      </ul>
    </div>
  </div>
  {_details_table("Trade log — last 15",
      ["Entry", "Exit", "In", "Out", "Return", "Days", "Reason"], trows) if trows else ""}
</section>"""
        )

    # ---------------------------------------------------------------- notes
    if p.notes:
        items = "".join(f"<li>{html.escape(n)}</li>" for n in p.notes)
        sections.append(f'<section class="card"><h2>Data notes</h2><ul class="notes">{items}</ul></section>')

    sections.append(
        """
<footer class="page-foot">
  <p>Generated by FirePlanner from Interactive Brokers daily bars. Indicators, regime, scores and
     sizing are computed locally — nothing here is a recommendation, a forecast, or investment advice.
     Backtested results are not a promise about future returns.</p>
</footer>"""
    )

    # <title> first so any host that scans only the head of the file finds it.
    return (
        f"<title>{html.escape(title)}</title>"
        + _CSS
        + f'<main class="viz-root">{"".join(sections)}</main>'
        + _JS
    )


_CSS = """
<style>
.viz-root {
  color-scheme: light;
  --surface-1: #fcfcfb;
  --page: #f9f9f7;
  --text-primary: #0b0b0b;
  --text-secondary: #52514e;
  --muted: #898781;
  --grid: #e1e0d9;
  --baseline: #c3c2b7;
  --border: rgba(11,11,11,0.10);
  --series-1: #2a78d6;
  --series-2: #eb6834;
  --series-3: #1baf7a;
  --status-good: #0ca30c;
  --status-warning: #fab219;
  --status-serious: #ec835a;
  --status-critical: #d03b3b;
  --pos: #006300;
  --neg: #d03b3b;
}
@media (prefers-color-scheme: dark) {
  :root:where(:not([data-theme="light"])) .viz-root {
    color-scheme: dark;
    --surface-1: #1a1a19;
    --page: #0d0d0d;
    --text-primary: #ffffff;
    --text-secondary: #c3c2b7;
    --muted: #898781;
    --grid: #2c2c2a;
    --baseline: #383835;
    --border: rgba(255,255,255,0.10);
    --series-1: #3987e5;
    --series-2: #d95926;
    --series-3: #199e70;
    --pos: #0ca30c;
    --neg: #e66767;
  }
}
:root[data-theme="dark"] .viz-root {
  color-scheme: dark;
  --surface-1: #1a1a19;
  --page: #0d0d0d;
  --text-primary: #ffffff;
  --text-secondary: #c3c2b7;
  --muted: #898781;
  --grid: #2c2c2a;
  --baseline: #383835;
  --border: rgba(255,255,255,0.10);
  --series-1: #3987e5;
  --series-2: #d95926;
  --series-3: #199e70;
  --pos: #0ca30c;
  --neg: #e66767;
}

body { background: var(--page, #f9f9f7); margin: 0; }
.viz-root {
  background: var(--page);
  color: var(--text-primary);
  font-family: system-ui, -apple-system, "Segoe UI", sans-serif;
  font-size: 14px; line-height: 1.5;
  max-width: 1120px; margin: 0 auto; padding: 28px 20px 60px;
  box-sizing: border-box;
}
.viz-root * { box-sizing: border-box; }

h1 { font-size: 28px; margin: 0 0 4px; letter-spacing: -0.02em; }
h2 { font-size: 17px; margin: 0 0 6px; letter-spacing: -0.01em; }
h3 { font-size: 13px; margin: 0 0 10px; color: var(--text-secondary);
     text-transform: uppercase; letter-spacing: 0.06em; font-weight: 600; }
p { margin: 0 0 10px; }
.sub { color: var(--text-secondary); font-size: 13px; margin: 0; }
.lede { color: var(--text-secondary); font-size: 13px; margin: 0 0 14px; max-width: 68ch; }
.muted-txt { color: var(--muted); font-size: 12px; }
code { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12px;
       background: var(--page); padding: 1px 5px; border-radius: 4px; }

.page-head { display: flex; justify-content: space-between; align-items: flex-start;
             gap: 20px; flex-wrap: wrap; margin-bottom: 20px; }
.asof { text-align: right; }
.asof-label { display: block; font-size: 11px; color: var(--muted);
              text-transform: uppercase; letter-spacing: 0.07em; }
.asof-date { font-size: 15px; font-weight: 600; font-variant-numeric: tabular-nums; }

.card { background: var(--surface-1); border: 1px solid var(--border);
        border-radius: 12px; padding: 18px 20px; margin-bottom: 16px; }
.grid-2 { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; margin-bottom: 16px; }
.grid-2.tight { margin-bottom: 0; gap: 24px; }
@media (max-width: 780px) { .grid-2 { grid-template-columns: 1fr; } }

/* regime banner */
.regime { border-left: 3px solid var(--muted); }
.regime-good { border-left-color: var(--status-good); }
.regime-warning { border-left-color: var(--status-warning); }
.regime-serious { border-left-color: var(--status-serious); }
.regime-critical { border-left-color: var(--status-critical); }
.regime-verdict { display: flex; align-items: baseline; gap: 14px; flex-wrap: wrap; margin-bottom: 14px; }
.regime-eyebrow { font-size: 11px; color: var(--muted); text-transform: uppercase;
                  letter-spacing: 0.07em; width: 100%; }
.regime-label { font-size: 30px; font-weight: 650; letter-spacing: -0.02em; }
.regime-score { font-size: 22px; color: var(--text-secondary); }
.regime-score .of, .score-num .of { font-size: 13px; color: var(--muted); }
.regime-cap { margin-left: auto; font-size: 13px; color: var(--text-secondary); }
.regime-note { font-size: 12px; color: var(--muted); margin: 12px 0 0; max-width: 72ch; }
.regime-meters { display: grid; gap: 7px; }

.meter-row { display: grid; grid-template-columns: 128px 1fr 34px; align-items: center; gap: 10px; }
.meter-label { font-size: 12px; color: var(--text-secondary); }
.meter-track { height: 6px; background: var(--grid); border-radius: 4px; overflow: hidden; }
.meter-fill { display: block; height: 100%; border-radius: 4px; }
.meter-val { font-size: 12px; color: var(--text-secondary); text-align: right;
             font-variant-numeric: tabular-nums; }

/* tiles */
.tiles { display: grid; grid-template-columns: repeat(auto-fit, minmax(178px, 1fr));
         gap: 12px; margin-bottom: 16px; }
.tile { background: var(--surface-1); border: 1px solid var(--border);
        border-radius: 10px; padding: 13px 14px; }
.tile-label { font-size: 11px; color: var(--muted); text-transform: uppercase;
              letter-spacing: 0.06em; display: flex; align-items: center; gap: 6px; }
.tile-value { font-size: 23px; font-weight: 600; letter-spacing: -0.02em; margin: 3px 0 2px; }
.tile-sub { font-size: 11.5px; color: var(--text-secondary); min-height: 16px; }
.dot { width: 7px; height: 7px; border-radius: 50%; display: inline-block; flex: none; }
.dot-good { background: var(--status-good); }
.dot-warning { background: var(--status-warning); }
.dot-serious { background: var(--status-serious); }
.dot-critical { background: var(--status-critical); }
.dot-muted { background: var(--muted); }
.spark { display: block; width: 100%; height: 30px; margin-top: 6px; }

/* score */
.score-head { display: flex; align-items: center; gap: 14px; margin-bottom: 14px; }
.score-num { font-size: 34px; font-weight: 650; letter-spacing: -0.02em; }
.action { font-size: 13px; font-weight: 650; padding: 5px 12px; border-radius: 999px;
          letter-spacing: 0.04em; }
.action-pill { font-size: 11px; font-weight: 650; padding: 2px 9px; border-radius: 999px; }
.action-good { background: rgba(12,163,12,0.14); color: var(--status-good); }
.action-warning { background: rgba(250,178,25,0.18); color: #8a6100; }
.action-serious { background: rgba(236,131,90,0.18); color: #a4451c; }
.action-critical { background: rgba(208,59,59,0.15); color: var(--status-critical); }
.action-muted { background: var(--grid); color: var(--text-secondary); }
:root[data-theme="dark"] .action-warning, :root[data-theme="dark"] .action-serious { color: #fab219; }
@media (prefers-color-scheme: dark) {
  :root:where(:not([data-theme="light"])) .action-warning,
  :root:where(:not([data-theme="light"])) .action-serious { color: #fab219; }
}

/* charts */
.chart-wrap { position: relative; }
.viz { display: block; width: 100%; height: auto; overflow: visible; }
.axis { font-size: 10.5px; fill: var(--muted); font-family: inherit;
        font-variant-numeric: tabular-nums; }
.endlabel { font-size: 11px; font-weight: 600; font-family: inherit; }
.barval { font-size: 11px; fill: var(--text-secondary); font-family: inherit;
          font-variant-numeric: tabular-nums; }
.legend { display: flex; gap: 16px; flex-wrap: wrap; margin-bottom: 6px; }
.chip { display: inline-flex; align-items: center; gap: 6px; font-size: 12px;
        color: var(--text-secondary); }
.chip i { width: 11px; height: 2.5px; border-radius: 2px; display: inline-block; }
.chart-empty { color: var(--muted); font-size: 13px; padding: 20px 0; }
.tooltip { position: absolute; pointer-events: none; background: var(--surface-1);
           border: 1px solid var(--border); border-radius: 8px; padding: 7px 10px;
           font-size: 12px; box-shadow: 0 4px 14px rgba(0,0,0,0.12); white-space: nowrap;
           z-index: 5; font-variant-numeric: tabular-nums; }
.tooltip b { display: block; margin-bottom: 3px; font-size: 11px; color: var(--muted);
             font-weight: 500; }
.tooltip .row { display: flex; align-items: center; gap: 7px; }
.tooltip .row i { width: 9px; height: 2.5px; border-radius: 2px; }

/* tables */
table { width: 100%; border-collapse: collapse; font-size: 13px; }
table.data th, table.data td { text-align: right; padding: 7px 9px;
                               border-bottom: 1px solid var(--grid); }
table.data th:first-child, table.data td:first-child { text-align: left; }
table.data th { font-size: 11px; color: var(--muted); text-transform: uppercase;
                letter-spacing: 0.05em; font-weight: 600; white-space: nowrap; }
table.data td { font-variant-numeric: tabular-nums; }
table.data tbody tr:last-child td { border-bottom: none; }
table.kv td { padding: 6px 0; border-bottom: 1px solid var(--grid); font-size: 13px; }
table.kv td:first-child { color: var(--text-secondary); }
table.kv td:last-child { text-align: right; font-variant-numeric: tabular-nums; font-weight: 500; }
table.kv thead { display: none; }
table.kv tbody tr:last-child td { border-bottom: none; }
.wide { display: block; overflow-x: auto; }
.pos { color: var(--pos); }
.neg { color: var(--neg); }

details.tableview { margin-top: 12px; }
details.tableview summary { cursor: pointer; font-size: 12px; color: var(--text-secondary);
                            padding: 5px 0; user-select: none; }
details.tableview summary:hover { color: var(--text-primary); }
details.tableview[open] summary { margin-bottom: 8px; }

.verdict ul { margin: 0; padding-left: 18px; }
.verdict li { font-size: 12.5px; color: var(--text-secondary); margin-bottom: 9px; }
.verdict strong { color: var(--text-primary); }
ul.notes { margin: 0; padding-left: 18px; font-size: 13px; color: var(--text-secondary); }

.page-foot { margin-top: 24px; padding-top: 16px; border-top: 1px solid var(--border); }
.page-foot p { font-size: 11.5px; color: var(--muted); margin: 0; max-width: 80ch; }
</style>
"""


_JS = """
<script>
(function () {
  var COLORS = ['--series-1', '--series-2', '--series-3'];
  document.querySelectorAll('.chart-wrap').forEach(function (wrap) {
    var id = wrap.getAttribute('data-chart');
    var pts, names;
    try {
      pts = JSON.parse(wrap.getAttribute('data-points'));
      names = JSON.parse(wrap.getAttribute('data-series'));
    } catch (e) { return; }
    var suffix = '', digits = 0;
    try { suffix = JSON.parse(wrap.getAttribute('data-suffix')) || ''; } catch (e) {}
    try { digits = parseInt(wrap.getAttribute('data-digits'), 10) || 0; } catch (e) {}

    var svg = wrap.querySelector('svg');
    var cross = wrap.querySelector('#' + id + '-cross');
    var hit = wrap.querySelector('#' + id + '-hit');
    var tip = wrap.querySelector('#' + id + '-tip');
    if (!svg || !hit || !tip || !pts.length) return;

    function nearest(svgX) {
      var best = 0, bestD = Infinity;
      for (var i = 0; i < pts.length; i++) {
        var d = Math.abs(pts[i].x - svgX);
        if (d < bestD) { bestD = d; best = i; }
      }
      return pts[best];
    }

    function show(evt) {
      var rect = svg.getBoundingClientRect();
      var vb = svg.viewBox.baseVal;
      var scale = vb.width / rect.width;
      var svgX = (evt.clientX - rect.left) * scale;
      var p = nearest(svgX);

      cross.setAttribute('x1', p.x);
      cross.setAttribute('x2', p.x);
      cross.setAttribute('opacity', '0.5');

      var rows = '<b>' + p.d + '</b>';
      for (var i = 0; i < p.v.length; i++) {
        if (p.v[i] === null) continue;
        rows += '<div class="row"><i style="background:var(' + COLORS[i % COLORS.length] + ')"></i>' +
                names[i] + ' ' +
                p.v[i].toLocaleString(undefined, {minimumFractionDigits: digits,
                                                  maximumFractionDigits: digits}) +
                suffix + '</div>';
      }
      tip.innerHTML = rows;
      tip.hidden = false;

      var px = (p.x / scale);
      var tw = tip.offsetWidth;
      tip.style.left = Math.max(0, Math.min(px + 12, rect.width - tw - 4)) + 'px';
      tip.style.top = '8px';
    }

    function hide() {
      cross.setAttribute('opacity', '0');
      tip.hidden = true;
    }

    hit.addEventListener('mousemove', show);
    hit.addEventListener('mouseleave', hide);
    hit.addEventListener('touchmove', function (e) {
      if (e.touches && e.touches[0]) { show(e.touches[0]); }
    }, {passive: true});
    hit.addEventListener('touchend', hide);
  });
})();
</script>
"""
