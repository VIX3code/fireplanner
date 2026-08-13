"""Performance statistics for an equity curve and its trade log."""

from __future__ import annotations

import numpy as np
import pandas as pd

__all__ = ["compute_stats", "drawdown_series", "buy_and_hold_stats", "best_days_analysis"]

TRADING_DAYS = 252


def drawdown_series(equity: pd.Series) -> pd.Series:
    """Percentage drawdown from the running peak."""
    return 100.0 * (equity / equity.cummax() - 1.0)


def compute_stats(
    equity: pd.Series,
    trades: pd.DataFrame | None = None,
    in_market: pd.Series | None = None,
    risk_free: float = 0.0,
) -> dict:
    """Standard risk/return statistics.

    Sharpe and Sortino are computed on daily returns and annualized by
    ``sqrt(252)``. Sortino uses downside deviation about zero, so it is not
    comparable to implementations that use the mean as the target.
    """
    equity = equity.dropna()
    if len(equity) < 2:
        return {"bars": len(equity)}

    ret = equity.pct_change().dropna()
    years = max((equity.index[-1] - equity.index[0]).days / 365.25, 1e-9)
    total_return = equity.iloc[-1] / equity.iloc[0] - 1.0

    vol = float(ret.std(ddof=1) * np.sqrt(TRADING_DAYS))
    excess = ret - risk_free / TRADING_DAYS
    sharpe = float(excess.mean() / ret.std(ddof=1) * np.sqrt(TRADING_DAYS)) if ret.std(ddof=1) > 0 else float("nan")

    downside = ret[ret < 0]
    dd_dev = float(downside.std(ddof=1) * np.sqrt(TRADING_DAYS)) if len(downside) > 1 else float("nan")
    sortino = float(excess.mean() * TRADING_DAYS / dd_dev) if dd_dev and dd_dev > 0 else float("nan")

    dd = drawdown_series(equity)
    max_dd = float(dd.min())
    cagr = float((equity.iloc[-1] / equity.iloc[0]) ** (1 / years) - 1.0)

    stats = {
        "bars": int(len(equity)),
        "start": str(equity.index[0].date()),
        "end": str(equity.index[-1].date()),
        "years": round(years, 2),
        "initial_equity": float(equity.iloc[0]),
        "final_equity": float(equity.iloc[-1]),
        "total_return_pct": 100.0 * float(total_return),
        "cagr_pct": 100.0 * cagr,
        "vol_pct": 100.0 * vol,
        "sharpe": sharpe,
        "sortino": sortino,
        "max_drawdown_pct": max_dd,
        "calmar": float(cagr / abs(max_dd / 100.0)) if max_dd < 0 else float("nan"),
        "best_day_pct": 100.0 * float(ret.max()),
        "worst_day_pct": 100.0 * float(ret.min()),
    }

    if in_market is not None and len(in_market):
        stats["exposure_pct"] = 100.0 * float(in_market.reindex(equity.index).fillna(0).mean())

    if trades is not None and not trades.empty:
        wins = trades[trades["pnl"] > 0]
        losses = trades[trades["pnl"] <= 0]
        gross_win = float(wins["pnl"].sum())
        gross_loss = float(abs(losses["pnl"].sum()))
        stats.update(
            {
                "trades": int(len(trades)),
                "win_rate_pct": 100.0 * len(wins) / len(trades),
                "avg_win_pct": float(wins["return_pct"].mean()) if len(wins) else float("nan"),
                "avg_loss_pct": float(losses["return_pct"].mean()) if len(losses) else float("nan"),
                "avg_trade_pct": float(trades["return_pct"].mean()),
                "profit_factor": (gross_win / gross_loss) if gross_loss > 0 else float("inf"),
                "avg_hold_days": float(trades["hold_days"].mean()),
                "largest_win_pct": float(trades["return_pct"].max()),
                "largest_loss_pct": float(trades["return_pct"].min()),
                "exit_reasons": trades["reason"].value_counts().to_dict(),
            }
        )
    else:
        stats["trades"] = 0

    return stats


def best_days_analysis(
    close: pd.Series,
    in_market: pd.Series,
    n: int = 20,
    sma_len: int = 200,
) -> dict:
    """How much of the market's best and worst days a strategy actually saw.

    This is the diagnostic for the "missing the ten best days destroys your
    return" problem, and it is worth running on any rule that goes to cash. The
    uncomfortable part is *where* those days live: they cluster inside
    drawdowns, in exactly the tape a defensive filter refuses to hold.

    Returns capture counts, the return forgone, and the conditions the best days
    occurred in — so the cost is a measured number rather than a worry.
    """
    ret = close.pct_change() * 100.0
    idx = in_market.index.intersection(ret.index)
    ret = ret.reindex(idx).dropna()
    flag = in_market.reindex(ret.index).fillna(0) > 0

    best = ret.nlargest(n)
    worst = ret.nsmallest(n)
    best_held = flag.reindex(best.index)
    worst_held = flag.reindex(worst.index)

    below = close.reindex(ret.index) < close.rolling(sma_len, min_periods=sma_len).mean().reindex(ret.index)
    dd = (close / close.cummax() - 1.0).reindex(ret.index) * 100.0

    # How often is a best day within a week of a worst day?
    worst_set = set(worst.index)
    positions = {d: i for i, d in enumerate(ret.index)}
    adjacent = sum(
        1
        for d in best.index
        if worst_set & set(ret.index[max(0, positions[d] - 5) : positions[d] + 6])
    )

    return {
        "n": n,
        "best_captured": int(best_held.sum()),
        "worst_avoided": int((~worst_held).sum()),
        "best_total_pct": float(best.sum()),
        "best_captured_pct": float(best[best_held].sum()),
        "best_forgone_pct": float(best[~best_held].sum()),
        "worst_total_pct": float(worst.sum()),
        "worst_avoided_pct": float(worst[~worst_held].sum()),
        "best_pct_below_sma": 100.0 * float(below.reindex(best.index).mean()),
        "baseline_pct_below_sma": 100.0 * float(below.mean()),
        "best_mean_drawdown": float(dd.reindex(best.index).mean()),
        "baseline_mean_drawdown": float(dd.mean()),
        "best_adjacent_to_worst": adjacent,
    }


def buy_and_hold_stats(close: pd.Series, initial_equity: float = 100_000.0) -> dict:
    """Benchmark the strategy against simply owning the thing.

    A swing system that cannot beat buy-and-hold *risk-adjusted* is not earning
    its complexity, so this belongs next to every strategy result.
    """
    curve = initial_equity * (close / close.iloc[0])
    stats = compute_stats(curve.dropna())
    stats["label"] = "buy_and_hold"
    return stats
