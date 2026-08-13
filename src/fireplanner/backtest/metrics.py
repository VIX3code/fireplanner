"""Performance statistics for an equity curve and its trade log."""

from __future__ import annotations

import numpy as np
import pandas as pd

__all__ = ["compute_stats", "drawdown_series", "buy_and_hold_stats"]

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


def buy_and_hold_stats(close: pd.Series, initial_equity: float = 100_000.0) -> dict:
    """Benchmark the strategy against simply owning the thing.

    A swing system that cannot beat buy-and-hold *risk-adjusted* is not earning
    its complexity, so this belongs next to every strategy result.
    """
    curve = initial_equity * (close / close.iloc[0])
    stats = compute_stats(curve.dropna())
    stats["label"] = "buy_and_hold"
    return stats
