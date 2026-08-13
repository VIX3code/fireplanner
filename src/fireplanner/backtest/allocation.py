"""Backtest a target-weight schedule rather than discrete trades.

The trade engine in :mod:`fireplanner.backtest.engine` answers "was this rule
profitable". This one answers the question the dashboard actually poses: **if I
had held the allocation the dashboard told me to hold each day, what would have
happened?**

That is a different simulation and it deserves its own accounting. Rebalances
happen at the next open after a target change, the traded notional pays
commission and slippage, and drift between rebalances is left alone — because
that is what a person following the dashboard would really do.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from .metrics import best_days_analysis, compute_stats

__all__ = ["run_allocation_backtest", "AllocationBacktest"]


class AllocationBacktest:
    """Result of a target-weight simulation."""

    def __init__(self, equity: pd.Series, weights: pd.DataFrame, trades: pd.DataFrame, stats: dict):
        self.equity_curve = equity
        self.weights = weights
        self.trades = trades
        self.stats = stats


def run_allocation_backtest(
    close: pd.Series,
    open_: pd.Series,
    target: pd.Series,
    initial_equity: float = 100_000.0,
    commission_per_share: float = 0.0035,
    min_commission: float = 1.00,
    slippage_bps: float = 2.0,
    band: float = 0.0,
) -> AllocationBacktest:
    """Hold ``target`` fraction of equity in the asset, rebalancing on change.

    ``band`` suppresses rebalances smaller than that fraction of equity, matching
    the dashboard's ``min_trade_pct`` so the simulation and the instruction agree.
    """
    idx = close.index
    tgt = target.reindex(idx).ffill()
    valid = tgt.notna()
    if not valid.any():
        raise ValueError("target series has no usable values")

    start = int(np.argmax(valid.to_numpy()))
    px_c = close.to_numpy(float)
    px_o = open_.reindex(idx).to_numpy(float)
    t = tgt.to_numpy(float)

    n = len(idx)
    cash = initial_equity
    shares = 0.0
    equity = np.full(n, np.nan)
    held_w = np.full(n, np.nan)
    trades: list[dict] = []
    pending_target: float | None = None

    for i in range(start, n):
        # Execute a rebalance decided on the previous close.
        if pending_target is not None and np.isfinite(px_o[i]):
            eq = cash + shares * px_o[i]
            want_shares = (pending_target * eq) / px_o[i]
            delta = want_shares - shares
            if abs(delta * px_o[i]) >= band * eq and abs(delta) >= 1:
                fill = px_o[i] * (1 + slippage_bps / 1e4 * np.sign(delta))
                fee = max(min_commission, abs(delta) * commission_per_share)
                cash -= delta * fill + fee
                shares += delta
                trades.append(
                    {
                        "date": idx[i],
                        "target": pending_target,
                        "shares_delta": delta,
                        "price": fill,
                        "notional": abs(delta * fill),
                        "fee": fee,
                    }
                )
            pending_target = None

        eq = cash + shares * px_c[i]
        equity[i] = eq
        held_w[i] = (shares * px_c[i]) / eq if eq > 0 else 0.0

        # Decide for tomorrow: has the instruction changed from what we hold?
        if i + 1 < n and np.isfinite(t[i]):
            drift = abs(held_w[i] - t[i])
            if drift >= max(band, 1e-9):
                pending_target = t[i]

    eq_series = pd.Series(equity, index=idx).dropna()
    w = pd.DataFrame({"target": tgt, "held": pd.Series(held_w, index=idx)}).loc[eq_series.index]

    stats = compute_stats(eq_series, in_market=(w["held"] > 0.01).astype(float))
    trades_df = pd.DataFrame(trades)
    stats["rebalances"] = int(len(trades_df))
    if not trades_df.empty:
        stats["total_costs"] = float(trades_df["fee"].sum())
        stats["traded_notional"] = float(trades_df["notional"].sum())
    stats.update(
        {f"bd_{k}": v for k, v in best_days_analysis(close, (w["held"] > 0.01).astype(float)).items()}
    )
    stats["avg_weight_pct"] = 100.0 * float(w["held"].mean())
    return AllocationBacktest(eq_series, w, trades_df, stats)
