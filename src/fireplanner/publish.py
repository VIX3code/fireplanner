"""Generate the pages as static files, optionally with account figures removed.

The deployment shape this package is built for is **two-tier**: a private machine
that holds the broker connection regenerates static HTML on a schedule, and that
HTML is served from wherever you like. The pages have no external requests and no
server-side code, so the second tier is a plain file host.

Redaction exists because the pages otherwise carry your net liquidation, your
positions and your trade sizes. That is fine on a laptop and a bad idea on a
domain. ``redact_payload`` strips every absolute currency figure while keeping
everything expressed as a percentage, so the signal survives and the balance
sheet does not.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pandas as pd

__all__ = ["redact_payload", "publish_site", "INDEX_TEMPLATE"]


def redact_payload(payload):
    """Return a copy with every absolute money figure removed.

    Percentages, prices, scores and targets are preserved — they are public
    market facts or dimensionless. What goes is anything that reveals the size of
    the account: equity, notional, share counts, position values, P&L.
    """
    p = copy.copy(payload)

    # ---- account summary and positions ---------------------------------
    p.account = {"summary": {}, "quotes": (payload.account or {}).get("quotes", {})}
    p.positions = None

    # ---- the decision: keep the target, drop the trade size -------------
    if payload.decision:
        d = dict(payload.decision)
        for key in ("equity", "shares_delta", "dollars_delta"):
            d[key] = None
        d["redacted"] = True
        p.decision = d

    if payload.exposure_basis:
        eb = dict(payload.exposure_basis)
        eb.pop("n_positions", None)
        p.exposure_basis = eb

    # The note names the position count and exposure; rewrite it generically.
    if payload.exposure_note:
        p.exposure_note = (
            "Current exposure is measured as <strong>total equity risk</strong> — all equity "
            "positions as a share of net liquidation — not as the benchmark position alone. "
            "A book of correlated single names is not cash, so treating it as flat would invert "
            "the instruction."
        )

    # ---- trade plan: keep the levels, drop the size ---------------------
    if payload.trade_plan:
        tp = dict(payload.trade_plan)
        for key in ("shares", "notional", "risk_dollars"):
            tp.pop(key, None)
        p.trade_plan = tp

    # ---- backtests are indexed to 100, but stats carry equity ----------
    for attr in ("backtest", "alloc_backtest", "alloc_buyhold"):
        stats = getattr(payload, attr, None)
        if isinstance(stats, dict) and stats:
            cleaned = {k: v for k, v in stats.items() if k not in {"initial_equity", "final_equity"}}
            if "stats" in cleaned and isinstance(cleaned["stats"], dict):
                cleaned["stats"] = {
                    k: v for k, v in cleaned["stats"].items()
                    if k not in {"initial_equity", "final_equity"}
                }
            setattr(p, attr, cleaned)

    for v in getattr(p, "variants", []) or []:
        if isinstance(v.get("stats"), dict):
            v["stats"] = {
                k: val for k, val in v["stats"].items()
                if k not in {"initial_equity", "final_equity"}
            }

    p.reliability = {
        k: v for k, v in (payload.reliability or {}).items() if k != "total_costs"
    }
    return p


INDEX_TEMPLATE = """<title>FirePlanner</title>
<style>
:root {{ color-scheme: light; --bg:#f9f9f7; --card:#fcfcfb; --ink:#0b0b0b;
        --dim:#52514e; --line:rgba(11,11,11,0.10); --accent:#2a78d6; }}
@media (prefers-color-scheme: dark) {{ :root:not([data-theme="light"]) {{
  color-scheme: dark; --bg:#0d0d0d; --card:#1a1a19; --ink:#fff; --dim:#c3c2b7;
  --line:rgba(255,255,255,0.10); --accent:#3987e5; }} }}
:root[data-theme="dark"] {{ color-scheme: dark; --bg:#0d0d0d; --card:#1a1a19; --ink:#fff;
  --dim:#c3c2b7; --line:rgba(255,255,255,0.10); --accent:#3987e5; }}
body {{ background:var(--bg); color:var(--ink); margin:0;
       font-family:system-ui,-apple-system,"Segoe UI",sans-serif; }}
main {{ max-width:660px; margin:0 auto; padding:56px 20px; }}
h1 {{ font-size:28px; letter-spacing:-0.02em; margin:0 0 6px; }}
p.sub {{ color:var(--dim); margin:0 0 32px; font-size:14px; }}
a.card {{ display:block; background:var(--card); border:1px solid var(--line);
         border-radius:12px; padding:20px 22px; margin-bottom:14px;
         text-decoration:none; color:inherit; }}
a.card:hover {{ border-color:var(--accent); }}
a.card h2 {{ font-size:17px; margin:0 0 4px; }}
a.card p {{ font-size:13px; color:var(--dim); margin:0; }}
.meta {{ font-size:12px; color:var(--dim); margin-top:28px; padding-top:16px;
        border-top:1px solid var(--line); }}
</style>
<main>
  <h1>FirePlanner</h1>
  <p class="sub">Generated {generated} · bars through {as_of}{redaction}</p>
  <a class="card" href="signal_desk.html">
    <h2>Signal desk →</h2>
    <p>What to do today: target allocation, the trade to reach it, and the levels that would
       change it.</p>
  </a>
  <a class="card" href="dashboard.html">
    <h2>Analysis dashboard →</h2>
    <p>What the market is doing: regime, indicators, term structure, and the backtest evidence.</p>
  </a>
  <p class="meta">Computed locally from Interactive Brokers daily bars. Not a recommendation,
     a forecast, or investment advice.</p>
</main>
"""


def publish_site(
    payload,
    out_dir: str | Path = "site",
    redact: bool = False,
    title_prefix: str = "",
) -> dict:
    """Write ``index.html``, ``signal_desk.html`` and ``dashboard.html`` to ``out_dir``.

    Returns a manifest of what was written, which is convenient for a deploy
    script to log or diff.
    """
    from .dashboard import render_desk, render_html

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    source = redact_payload(payload) if redact else payload

    written = {}
    desk_html = render_desk(source, title=f"{title_prefix}S&P 500 Signal Desk".strip())
    (out / "signal_desk.html").write_text(desk_html)
    written["signal_desk.html"] = len(desk_html)

    dash_html = render_html(source, title=f"{title_prefix}FirePlanner Swing Desk".strip())
    (out / "dashboard.html").write_text(dash_html)
    written["dashboard.html"] = len(dash_html)

    index = INDEX_TEMPLATE.format(
        generated=pd.Timestamp.now('UTC').strftime("%Y-%m-%d %H:%M UTC"),
        as_of=payload.as_of,
        redaction=" · account figures redacted" if redact else "",
    )
    (out / "index.html").write_text(index)
    written["index.html"] = len(index)

    # A tiny machine-readable summary, handy for alerting or a status endpoint.
    summary = {
        "as_of": payload.as_of,
        "generated_utc": pd.Timestamp.now('UTC').isoformat(),
        "redacted": redact,
        "decision": {
            k: v for k, v in (source.decision or {}).items()
            if k in {"date", "action", "target_pct", "previous_pct", "regime_label", "score"}
        },
        "reliability": source.reliability,
    }
    (out / "status.json").write_text(json.dumps(summary, indent=2, default=str))
    written["status.json"] = len(json.dumps(summary))

    return {"out_dir": str(out), "redacted": redact, "files": written}
