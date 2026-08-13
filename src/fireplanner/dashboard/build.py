"""Assemble everything the dashboard renders into one payload.

Kept separate from rendering so the same payload can feed the HTML page, a JSON
API, or a test assertion without duplicating any of the maths.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from .. import indicators as ind
from ..backtest import buy_and_hold_stats, run_backtest
from ..risk import RiskConfig, plan_position
from ..signals import latest_signal, score_frame

__all__ = ["DashboardPayload", "build_payload"]


@dataclass
class DashboardPayload:
    as_of: str
    benchmark: str
    bars: dict = field(default_factory=dict)
    enriched: dict = field(default_factory=dict)
    regime: pd.DataFrame | None = None
    regime_now: dict = field(default_factory=dict)
    signals: list = field(default_factory=list)
    trade_plan: dict = field(default_factory=dict)
    account: dict = field(default_factory=dict)
    positions: pd.DataFrame | None = None
    backtest: dict = field(default_factory=dict)
    quotes: dict = field(default_factory=dict)
    notes: list = field(default_factory=list)


def build_payload(
    provider,
    benchmark: str = "SPY",
    vol_symbol: str = "VIX",
    breadth_symbol: str = "RSP",
    watchlist: list[str] | None = None,
    lookback_days: int = 1260,
    equity: float | None = None,
    risk_cfg: RiskConfig | None = None,
) -> DashboardPayload:
    """Pull bars, compute indicators, score the watchlist, and size a trade."""
    risk_cfg = risk_cfg or RiskConfig()
    notes: list[str] = []

    bench_bars = provider.history(benchmark, lookback_days=lookback_days)

    vix = breadth = None
    try:
        vix = provider.history(vol_symbol, lookback_days=lookback_days)["close"]
    except Exception:
        notes.append(f"{vol_symbol} unavailable — regime computed without the volatility component.")
    try:
        breadth = provider.history(breadth_symbol, lookback_days=lookback_days)["close"]
    except Exception:
        notes.append(f"{breadth_symbol} unavailable — regime computed without the breadth component.")

    regime = ind.compute_regime(bench_bars["close"], vix=vix, equal_weight=breadth)
    regime_now = ind.latest_regime(regime).as_dict()

    # ---- account ---------------------------------------------------------
    account, positions = {}, None
    if hasattr(provider, "account"):
        account = provider.account() or {}
    if account.get("positions"):
        positions = pd.DataFrame(account["positions"])
    elif hasattr(provider, "positions"):
        try:
            positions = provider.positions()
        except Exception:
            notes.append("Positions unavailable — connect TWS/Gateway for the live book.")

    net_liq = equity
    if net_liq is None:
        net_liq = float(account.get("summary", {}).get("net_liquidation", 100_000.0))

    if positions is not None and not positions.empty:
        positions = positions.copy()
        if "market_value" not in positions and {"position", "market_price"} <= set(positions.columns):
            positions["market_value"] = positions["position"] * positions["market_price"]
        positions["weight_pct"] = 100.0 * positions["market_value"] / net_liq
        if {"market_price", "average_price"} <= set(positions.columns):
            positions["pnl_pct"] = 100.0 * (positions["market_price"] / positions["average_price"] - 1.0)
        positions = positions.sort_values("market_value", ascending=False)

    # ---- watchlist scoring ----------------------------------------------
    watchlist = watchlist or [benchmark]
    if benchmark not in watchlist:
        watchlist = [benchmark] + list(watchlist)

    bars_map, enriched_map, signals = {}, {}, []
    for sym in watchlist:
        try:
            b = provider.history(sym, lookback_days=lookback_days)
        except Exception:
            notes.append(f"{sym}: no history available — skipped.")
            continue
        if len(b) < 220:
            notes.append(f"{sym}: only {len(b)} bars — needs ~250 for the 200-day trend gate; skipped.")
            continue
        e = ind.enrich(b)
        scored = score_frame(e, regime=regime["score"])
        try:
            sig = latest_signal(sym, scored, e)
        except ValueError as exc:
            notes.append(str(exc))
            continue
        bars_map[sym], enriched_map[sym] = b, e
        signals.append(sig.as_dict())

    signals.sort(key=lambda s: (s["score"] is None, -(s["score"] or 0)))

    # ---- trade plan on the benchmark ------------------------------------
    trade_plan = {}
    if benchmark in enriched_map:
        e = enriched_map[benchmark]
        last = e.dropna(subset=["atr14"]).iloc[-1]
        try:
            plan = plan_position(
                benchmark,
                equity=net_liq,
                entry=float(last["close"]),
                atr=float(last["atr14"]),
                cfg=risk_cfg,
                exposure_cap=regime_now.get("exposure_cap", 1.0),
                open_heat=0.0,
            )
            trade_plan = plan.as_dict()
            trade_plan["atr_pct"] = float(last["natr14"])
        except ValueError as exc:
            notes.append(f"{benchmark} sizing skipped: {exc}")

    # ---- backtest evidence ----------------------------------------------
    backtest = {}
    if benchmark in enriched_map:
        result = run_backtest(enriched_map[benchmark], regime=regime["score"], symbol=benchmark)
        bh_curve = bench_bars["close"].reindex(result.equity_curve.index)
        bh = buy_and_hold_stats(bh_curve.dropna())
        backtest = {
            "stats": result.stats,
            "buy_hold": bh,
            "equity": result.equity_curve,
            "bh_equity": 100.0 * bh_curve / bh_curve.iloc[0],
            "strategy_indexed": 100.0 * result.equity_curve / result.equity_curve.iloc[0],
            "trades": result.trades,
            "daily": result.daily,
        }

    quotes = account.get("quotes", {}) if account else {}
    as_of = str(bench_bars.index[-1].date())

    return DashboardPayload(
        as_of=as_of,
        benchmark=benchmark,
        bars=bars_map,
        enriched=enriched_map,
        regime=regime,
        regime_now=regime_now,
        signals=signals,
        trade_plan=trade_plan,
        account=account,
        positions=positions,
        backtest=backtest,
        quotes=quotes,
        notes=notes,
    )
