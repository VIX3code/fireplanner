"""The signal desk: one page that answers "in or out, and by how much".

The analysis dashboard reports what the market is doing. This page reports what
to *do*, and is deliberately narrow: a single instruction, the reasoning behind
it in three checks, the exact levels that would change it, and an honest account
of how often the signal has been wrong.

Everything is a single self-contained HTML file — inline SVG, inline CSS and JS,
no external requests.
"""

from __future__ import annotations

import html

import numpy as np
import pandas as pd

from .charts import Series, fmt, line_chart, sparkline
from .render import HEAD_META, _CSS, _JS, _details_table, _sign, _table

__all__ = ["render_desk", "desk_sections"]


ACTION_TONE = {"BUY": "good", "SELL": "critical", "HOLD": "warning", "WAIT": "warning"}


def _ladder(current: float, target: float, steps: list[float], width: int = 560, height: int = 104) -> str:
    """A horizontal ladder showing every allowed level, with current and target marked."""
    pad_l, pad_r = 16, 16
    plot_w = width - pad_l - pad_r
    y = 46

    lo, hi = min(steps), max(steps)
    span = (hi - lo) or 1.0
    x_of = lambda w: pad_l + plot_w * (w - lo) / span

    parts = [
        f'<svg viewBox="0 0 {width} {height}" class="viz" role="img" '
        f'aria-label="allocation ladder, target {target * 100:.0f}%" '
        f'preserveAspectRatio="xMidYMid meet">'
    ]
    # rail
    parts.append(
        f'<line x1="{pad_l}" y1="{y}" x2="{pad_l + plot_w}" y2="{y}" '
        f'stroke="var(--grid)" stroke-width="6" stroke-linecap="round"/>'
    )
    # filled portion up to target
    parts.append(
        f'<line x1="{pad_l}" y1="{y}" x2="{x_of(target):.1f}" y2="{y}" '
        f'stroke="var(--series-1)" stroke-width="6" stroke-linecap="round"/>'
    )
    # step ticks
    for s in steps:
        x = x_of(s)
        parts.append(
            f'<circle cx="{x:.1f}" cy="{y}" r="4" fill="var(--surface-1)" '
            f'stroke="var(--baseline)" stroke-width="1.5"/>'
        )
        parts.append(
            f'<text x="{x:.1f}" y="{y + 22}" class="axis" text-anchor="middle">{s * 100:.0f}%</text>'
        )
    # Keep a floating label inside the viewBox instead of letting it run off the
    # end of the rail, and away from the fixed step labels underneath.
    def anchor(x: float) -> tuple[str, float]:
        if x < pad_l + 34:
            return "start", pad_l
        if x > pad_l + plot_w - 34:
            return "end", pad_l + plot_w
        return "middle", x

    # target marker
    tx = x_of(target)
    ta, txa = anchor(tx)
    parts.append(
        f'<circle cx="{tx:.1f}" cy="{y}" r="8" fill="var(--series-1)" '
        f'stroke="var(--surface-1)" stroke-width="2.5"/>'
    )
    parts.append(
        f'<text x="{txa:.1f}" y="{y - 16}" class="ladder-label" text-anchor="{ta}" '
        f'fill="var(--series-1)">TARGET {target * 100:.0f}%</text>'
    )
    # current marker, only when it differs enough to matter
    if abs(current - target) > 0.005:
        cx = x_of(max(lo, min(hi, current)))
        ca, cxa = anchor(cx)
        parts.append(
            f'<path d="M{cx:.1f},{y + 11} l-5,9 l10,0 Z" fill="var(--series-2)"/>'
        )
        # Sits clear of the step labels on the row above it.
        parts.append(
            f'<text x="{cxa:.1f}" y="{y + 44}" class="ladder-label" text-anchor="{ca}" '
            f'fill="var(--series-2)">NOW {current * 100:.0f}%</text>'
        )
    parts.append("</svg>")
    return "".join(parts)


def _check(passed: bool | None, label: str, detail: str) -> str:
    if passed is None:
        tone, mark = "muted", "–"
    else:
        tone, mark = ("good", "✓") if passed else ("critical", "✕")
    return (
        f'<div class="check"><span class="check-mark check-{tone}">{mark}</span>'
        f'<div><div class="check-label">{html.escape(label)}</div>'
        f'<div class="check-detail">{detail}</div></div></div>'
    )


def stale_banner(bars_through: str, staleness_days: int | None) -> str:
    """The "these bars have aged" banner, filled in by the reader's clock.

    Staleness has to be judged when the page is *read*, not when it was built.
    These are static files: one generated today shows "1 day old" forever,
    including three weeks later when the signal has moved on. The script in
    ``_JS`` rewrites this element against the viewer's own clock; the build-time
    text below is only the no-JavaScript fallback.
    """
    prebuilt = (
        f"Bars are {staleness_days} days old. Refresh before acting on this."
        if staleness_days is not None and staleness_days > 4
        else ""
    )
    return (
        f'<div class="banner banner-warning" id="stale-banner" '
        f'data-bars-through="{bars_through}"{"" if prebuilt else " hidden"}>{prebuilt}</div>'
    )


def render_desk(p, title: str = "S&P 500 Signal Desk") -> str:
    """Render the decision page from a `DashboardPayload` carrying `decision`."""
    return (
        f"<title>{html.escape(title)}</title>"
        + HEAD_META
        + _CSS
        + _DESK_CSS
        + f'<main class="viz-root">{"".join(desk_sections(p, title))}</main>'
        + _JS
    )


def desk_sections(p, title: str = "S&P 500 Signal Desk", chrome: bool = True) -> list[str]:
    """The decision page's sections, in document order.

    ``chrome`` covers the page header (with its staleness banner) and the
    footer. The combined single-file page turns it off and supplies its own, so
    the staleness banner keeps a unique element id — the script that fills it in
    looks the banner up by id and would otherwise only ever find the first one.
    """
    d = p.decision
    if not d:
        raise ValueError("payload has no decision — build it with include_decision=True")

    alloc = p.allocation
    rel = p.reliability or {}
    bench = p.benchmark
    e = p.enriched.get(bench)
    last = e.iloc[-1] if e is not None else None

    target = d["target_pct"]
    current = d["current_pct"]
    action = d["action"]
    tone = ACTION_TONE.get(action, "muted")
    shares = d["shares_delta"]
    dollars = d["dollars_delta"]

    sections: list[str] = []

    # ---------------------------------------------------------------- header
    if chrome:
        stale_note = stale_banner(d["date"], p.staleness_days)
        sections.append(
            f"""
<header class="page-head">
  <div>
    <h1>{html.escape(title)}</h1>
    <p class="sub">How much of your equity should sit in {html.escape(bench)} today ·
       daily bars, decisions reviewed at the close</p>
  </div>
  <div class="asof"><span class="asof-label">Signal as of</span><span class="asof-date">{d['date']}</span></div>
</header>{stale_note}"""
        )

    # ---------------------------------------------------------------- the instruction
    redacted = bool(d.get("redacted"))
    if redacted and action in {"BUY", "SELL"}:
        # Account figures are stripped for public hosting, so express the
        # instruction as a move along the ladder rather than a share count.
        verb = "Increase" if action == "BUY" else "Reduce"
        headline = f"{verb} to {target * 100:.0f}%"
        sub = f"From {current * 100:.0f}% of equity to {target * 100:.0f}%."
    elif action == "HOLD":
        headline = "No action today"
        sub = f"Hold {target * 100:.0f}% of equity in {bench}."
    elif action == "WAIT":
        headline = "Hold — a change is pending"
        raw_pct = float(p.allocation["raw_target"].iloc[-1]) * 100 if p.allocation is not None else target * 100
        sub = (
            f"Committed target {target * 100:.0f}%, but the signal has moved to {raw_pct:.0f}%. "
            f"Trading now would reverse within days."
        )
    elif action == "BUY":
        headline = f"Buy {abs(shares):,.0f} shares"
        sub = f"≈ ${abs(dollars):,.0f} — moves you from {current * 100:.0f}% to {target * 100:.0f}%."
    else:
        headline = f"Sell {abs(shares):,.0f} shares"
        sub = f"≈ ${abs(dollars):,.0f} — moves you from {current * 100:.0f}% to {target * 100:.0f}%."

    eb = p.exposure_basis or {}
    if eb.get("basis") == "total_equity":
        across = f" across {eb['n_positions']} positions" if "n_positions" in eb else ""
        basis_line = (
            f"&ldquo;Now&rdquo; is your <strong>total equity exposure</strong> "
            f"({eb['total_equity_pct']:.0f}%{across}), not your "
            f"{html.escape(bench)} position of {eb['benchmark_pct']:.0f}%."
        )
    elif eb.get("basis") == "explicit":
        basis_line = "&ldquo;Now&rdquo; is the exposure you supplied."
    else:
        basis_line = f"&ldquo;Now&rdquo; is your {html.escape(bench)} position as a share of equity."

    exposure_banner = ""
    if p.exposure_note:
        exposure_banner = (
            '<div class="banner banner-warning banner-tight"><strong>How to act on this:</strong> '
            + p.exposure_note + "</div>"
        )

    held_days = d["days_in_state"]
    sections.append(
        f"""
<section class="card instruction instruction-{tone}">
  <span class="eyebrow">Today's instruction</span>
  <h2 class="headline">{html.escape(headline)}</h2>
  <p class="headline-sub">{html.escape(sub)}</p>
  {_ladder(current, target, list(p.ladder_steps))}
  <p class="basis">{basis_line}</p>
  <p class="reason">{html.escape(d['reason'])} This target has stood for
     <strong>{held_days} session{'s' if held_days != 1 else ''}</strong>.</p>
</section>{exposure_banner}"""
    )

    # ---------------------------------------------------------------- why
    if last is not None:
        raw_t = p.allocation["raw_target"].iloc[-1] if p.allocation is not None else target
        committed_fill = d["sleeve_fill"]
        if abs(float(raw_t) - target) > 1e-9:
            raw_fill = (float(raw_t) - p.ladder_steps[0]) / (p.ladder_steps[-1] - p.ladder_steps[0])
            sleeve_detail = (
                f"On its own this score would fill <strong>{raw_fill * 100:.0f}%</strong> of the "
                f"sleeve — a {float(raw_t) * 100:.0f}% target. The committed target is still "
                f"<strong>{target * 100:.0f}%</strong> "
                f"({'sleeve empty' if committed_fill < 0.005 else f'sleeve {committed_fill * 100:.0f}% full'}) "
                f"because a reversal has to wait out the cooldown."
            )
        else:
            sleeve_detail = (
                f"Fills <strong>{committed_fill * 100:.0f}%</strong> of the sleeve. Smoothed, because "
                f"the raw score moves about &plusmn;5 points a day on noise alone."
            )
        cap = d["regime_cap"]
        above50 = bool(last["close"] > last["sma50"])
        above200 = bool(last["close"] > last["sma200"])
        smooth = p.score_smooth
        checks = "".join(
            [
                _check(
                    cap > 0,
                    f"Market regime — {d['regime_label']}",
                    f"Gate allows up to <strong>{cap * 100:.0f}%</strong> of the tactical sleeve.",
                ),
                _check(
                    above50,
                    "Above the 50-day average",
                    f"{fmt(last['close'], 2)} vs {fmt(last['sma50'], 2)} "
                    f"({_sign(float(last['pct_from_sma50']), 1)}). "
                    f"Two closes below force the sleeve flat.",
                ),
                _check(
                    above200,
                    "Above the 200-day average",
                    f"{fmt(last['close'], 2)} vs {fmt(last['sma200'], 2)} "
                    f"({_sign(float(last['pct_from_sma200']), 1)}). The primary trend.",
                ),
                _check(
                    None,
                    f"Trend score {fmt(d['score'], 0)} (smoothed {fmt(smooth, 0)})",
                    sleeve_detail,
                ),
            ]
        )
        sections.append(
            f"""
<section class="card">
  <h2>Why</h2>
  <p class="lede">Three independent conditions. The regime sets the ceiling, the trend structure is a
     hard gate, and the score decides how much of the allowance to use.</p>
  <div class="checks">{checks}</div>
</section>"""
        )

    # ---------------------------------------------------------------- triggers
    trig = d.get("triggers", {})
    if trig:
        rows = []
        for key in ["sma50", "supertrend", "sma200", "atr_stop", "vix", "term"]:
            t = trig.get(key)
            if not t:
                continue
            dist = t["distance_pct"]
            # The ATR stop sits ~2.5 ATRs away by construction, so it is never news.
            near = abs(dist) < 3.0 and key != "atr_stop"
            rows.append(
                [
                    f"<strong>{html.escape(t['label'])}</strong>",
                    fmt(t.get("current", t["level"]), 2) if "current" in t else fmt(t["level"], 2),
                    fmt(t["level"], 2) if "current" in t else "—",
                    f"<span class=\"{'warn-txt' if near else ''}\">{_sign(dist, 1)}</span>",
                    html.escape(t["meaning"]),
                ]
            )
        next_down, next_up = p.next_steps.get("down"), p.next_steps.get("up")
        step_txt = []
        if next_down:
            step_txt.append(
                f"<li>Smoothed score below <strong>{fmt(next_down['threshold'], 0)}</strong> "
                f"(now {fmt(p.score_smooth, 0)}) steps the target down to "
                f"<strong>{next_down['target'] * 100:.0f}%</strong>.</li>"
            )
        if next_up:
            step_txt.append(
                f"<li>Smoothed score above <strong>{fmt(next_up['threshold'], 0)}</strong> "
                f"steps the target up to <strong>{next_up['target'] * 100:.0f}%</strong>.</li>"
            )
        step_txt.append(
            f"<li>Either way, no committed change for <strong>{p.cooldown_days} sessions</strong> "
            f"after a move — except a confirmed break of the 50-day, which is acted on immediately.</li>"
        )
        sections.append(
            f"""
<section class="card">
  <h2>What would change this</h2>
  <p class="lede">Set alerts on these and you can ignore the screen until one is hit.</p>
  {_table(["Level", "Now", "Trigger", "Distance", "What it means"], rows, "data wide")}
  <h3 style="margin-top:18px">Next steps on the ladder</h3>
  <ul class="notes">{''.join(step_txt)}</ul>
</section>"""
        )

    # ---------------------------------------------------------------- reliability
    if rel:
        hist = p.decision_history
        hrows = [
            [
                str(pd.Timestamp(r["date"]).date()),
                f"{r['from'] * 100:.0f}% → <strong>{r['to'] * 100:.0f}%</strong>",
                f'<span class="action-pill action-{"good" if r["to"] > r["from"] else "serious"}">'
                f'{"ADD" if r["to"] > r["from"] else "TRIM"}</span>',
                fmt(r["score"], 0),
                html.escape(str(r["regime"])),
                str(r["held_days"]),
            ]
            for r in hist
        ]
        shown, total = len(hrows), p.total_changes or len(hrows)
        ab = p.alloc_backtest or {}
        bh = p.alloc_buyhold or {}
        sections.append(
            f"""
<section class="card">
  <h2>How much should you trust this?</h2>
  <p class="lede">A signal you cannot rely on is worse than no signal, because it costs money to
     follow. These are the numbers that decide whether this is usable.</p>

  <div class="grid-2 tight">
    <div>
      {_table(["", ""], [
        ["Target changes per year", f"<strong>{fmt(rel['changes_per_year'], 1)}</strong>"],
        ["Reversed within 10 sessions",
         f"<strong>{fmt(rel['reversal_pct'], 0)}%</strong> &nbsp;"
         f"<span class='muted-txt'>was {fmt(rel['baseline_reversal_pct'], 0)}% before hysteresis</span>"],
        ["Median sessions between changes", f"{fmt(rel['median_gap'], 0)}"],
        ["Average target held", f"{fmt(rel['avg_target_pct'], 0)}%"],
        ["Rebalances in 5 years", f"{rel.get('rebalances', 0)}"],
        ["Total trading cost", f"${fmt(rel.get('total_costs', 0), 0)}"],
      ], "kv")}
    </div>
    <div class="verdict">
      <h3>Read this honestly</h3>
      <ul>
        <li><strong>It changes about {fmt(rel['changes_per_year'], 0)} times a year.</strong> That is
            roughly once a quarter — this is a position-sizing dial, not a trading signal. If you
            find yourself checking it daily for action, you are using it wrong.</li>
        <li><strong>A first version whipsawed badly.</strong> Without smoothing, a dead band and a
            cooldown it changed 24 times a year and reversed
            {fmt(rel['baseline_reversal_pct'], 0)}% of the time. The current settings cut that to
            {fmt(rel['reversal_pct'], 0)}% <em>without</em> costing return — the fix was free, which
            is the only reason to trust it.</li>
        <li><strong>It will not dodge a fast crash.</strong> The gate reacts to a confirmed break of
            the 50-day, so a one-week collapse hits the core in full. The core is the price of
            catching the best days, which cluster in exactly those weeks.</li>
        <li><strong>The core is a policy choice, not a signal.</strong> At
            {p.ladder_steps[0] * 100:.0f}% it never goes lower no matter how bad things look.
            If you cannot hold that through a 25% index drawdown, lower it before you need to.</li>
      </ul>
    </div>
  </div>

  {'<h3 style="margin-top:20px">Following this vs owning the index</h3>' if ab else ''}
  {_table(["Metric", "Following the signal", "Buy & hold"], [
      ["CAGR", fmt(ab.get('cagr_pct'), 2) + "%", fmt(bh.get('cagr_pct'), 2) + "%"],
      ["Volatility", fmt(ab.get('vol_pct'), 2) + "%", fmt(bh.get('vol_pct'), 2) + "%"],
      ["Sharpe", f"<strong>{fmt(ab.get('sharpe'), 2)}</strong>", fmt(bh.get('sharpe'), 2)],
      ["Max drawdown", f"<strong>{fmt(ab.get('max_drawdown_pct'), 2)}%</strong>",
       fmt(bh.get('max_drawdown_pct'), 2) + "%"],
      ["Best 20 days captured", f"{ab.get('bd_best_captured', 0)}/20", "20/20"],
      ["Average exposure", fmt(ab.get('avg_weight_pct'), 0) + "%", "100%"],
  ], "data") if ab else ''}

  <h3 style="margin-top:20px">Last {shown} of {total} target changes</h3>
  {_table(["Date", "Target", "Direction", "Score", "Regime", "Previous target held"], hrows, "data wide")}
</section>"""
        )

    # ---------------------------------------------------------------- history chart
    if alloc is not None and not alloc.empty:
        win = alloc.dropna(subset=["target"]).tail(504)
        chart = line_chart(
            [Series("Target %", win["target"] * 100, "series-1", 2.0)],
            chart_id="alloc",
            y_digits=0,
            y_suffix="%",
            height=190,
        )
        price = line_chart(
            [Series(bench, win["close"], "series-2", 1.5)],
            chart_id="allocpx",
            y_digits=0,
            height=190,
        )
        rows = [
            [d_.strftime("%Y-%m-%d"), f"{r['target'] * 100:.0f}%", fmt(r["score"], 0),
             str(r["regime_label"]), fmt(r["close"], 2)]
            for d_, r in win.tail(12).iterrows()
        ]
        sections.append(
            f"""
<section class="card">
  <h2>Two years of instructions</h2>
  <p class="lede">The target above, the index below, on a shared timeline. Flat stretches are the
     point — the signal is designed to leave you alone.</p>
  {chart}
  {price}
  {_details_table("Table view — last 12 sessions",
      ["Date", "Target", "Score", "Regime", f"{bench} close"], rows)}
</section>"""
        )

    if chrome:
        sections.append(DISCLAIMER)

    return sections


DISCLAIMER = """
<footer class="page-foot">
  <p>Generated by FirePlanner from Interactive Brokers daily bars. The target is the output of a
     mechanical rule computed locally from your own broker data — it is not a recommendation, a
     forecast, or investment advice, and backtested results are not a promise about future returns.</p>
</footer>"""


_DESK_CSS = """
<style>
.instruction { border-left: 3px solid var(--muted); text-align: center; padding: 26px 20px 20px; }
.instruction-good { border-left-color: var(--status-good); }
.instruction-warning { border-left-color: var(--status-warning); }
.instruction-critical { border-left-color: var(--status-critical); }
.eyebrow { font-size: 11px; color: var(--muted); text-transform: uppercase;
           letter-spacing: 0.08em; display: block; }
.headline { font-size: 38px; font-weight: 650; letter-spacing: -0.025em; margin: 6px 0 4px;
            text-wrap: balance; }
.headline-sub { font-size: 15px; color: var(--text-secondary); margin: 0 0 6px; }
.instruction .viz { max-width: 560px; margin: 6px auto 0; }
.reason { font-size: 13px; color: var(--text-secondary); margin: 12px auto 0; max-width: 66ch;
          line-height: 1.6; }
.ladder-label { font-size: 10.5px; font-weight: 650; letter-spacing: 0.05em; font-family: inherit; }

.checks { display: grid; gap: 14px; }
.check { display: flex; gap: 12px; align-items: flex-start; }
.check-mark { font-size: 13px; font-weight: 700; width: 22px; height: 22px; border-radius: 50%;
              display: inline-flex; align-items: center; justify-content: center; flex: none;
              margin-top: 1px; }
.check-good { background: rgba(12,163,12,0.14); color: var(--status-good); }
.check-critical { background: rgba(208,59,59,0.15); color: var(--status-critical); }
.check-muted { background: var(--grid); color: var(--text-secondary); }
.check-label { font-size: 13.5px; font-weight: 600; }
.check-detail { font-size: 12.5px; color: var(--text-secondary); margin-top: 2px; }

.banner { padding: 10px 14px; border-radius: 8px; font-size: 13px; margin-bottom: 16px;
          border: 1px solid var(--border); }
.banner-warning { background: rgba(250,178,25,0.12); color: var(--text-primary); }
.banner-tight { margin: 14px 0 0; text-align: left; font-size: 12.5px; line-height: 1.55; }
.basis { font-size: 12px; color: var(--text-secondary); margin: 10px 0 0; }
.banner-card { border-left: 3px solid var(--status-warning); }
.warn-txt { color: var(--status-critical); font-weight: 600; }

@media (max-width: 480px) {
  /* 38px puts a four-word instruction on three lines on a phone. */
  .headline { font-size: 27px; }
  /* SVG text is in viewBox units, so it shrinks with the drawing: the ladder's
     560-unit box rendered ~330 wide lands 10.5 units near 6px on screen. These
     are scoped to the ladder because every chart has its own box width. */
  .instruction .viz .axis { font-size: 16px; }
  .instruction .viz .ladder-label { font-size: 16px; }
}
</style>
"""
