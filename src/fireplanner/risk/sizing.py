"""Position sizing and portfolio risk limits.

The sizing rule is the part of a swing system that actually determines whether
you survive, so it is deliberately explicit rather than clever:

1. **Stop first.** The stop is ``k × ATR`` below entry — a volatility distance,
   not a round percentage. Wide-ranging names get wider stops and therefore
   fewer shares.
2. **Risk second.** Shares are whatever makes "entry to stop" cost exactly
   ``risk_pct`` of equity. Every position risks the same dollar amount, so no
   single idea can dominate the outcome.
3. **Caps third.** Cap by max position weight, by total open risk (portfolio
   heat), and by the regime's exposure ceiling.

Heat is the number most retail sizing ignores: ten positions each risking 1% is
a 10% drawdown if they are correlated and all stop out together — and in a
selloff they are correlated.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
import pandas as pd

__all__ = ["RiskConfig", "PositionPlan", "plan_position", "portfolio_heat", "chandelier_stop", "vol_target_scalar"]


@dataclass(frozen=True)
class RiskConfig:
    """Risk limits for a single account."""

    #: Fraction of equity risked between entry and stop on one position.
    risk_pct: float = 0.0075
    #: Stop distance in ATRs.
    atr_stop_mult: float = 2.5
    #: Trailing-stop distance in ATRs once a position is working.
    atr_trail_mult: float = 3.0
    #: Hard cap on any one position as a fraction of equity.
    max_position_pct: float = 0.10
    #: Cap on the sum of open risk across all positions.
    max_portfolio_heat: float = 0.06
    #: Cap on gross exposure as a fraction of equity.
    max_gross_exposure: float = 1.00
    #: Annualized volatility the portfolio is steered toward.
    target_vol: float = 0.15
    #: Never stage an order below this notional — commissions dominate.
    min_notional: float = 500.0
    #: Whole shares only (set False for fractional-share accounts).
    whole_shares: bool = True


@dataclass
class PositionPlan:
    """A fully specified, ready-to-stage position."""

    symbol: str
    entry: float
    stop: float
    shares: float
    notional: float
    risk_dollars: float
    risk_pct_equity: float
    weight: float
    r_multiple_targets: dict
    limited_by: str

    def as_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "entry": round(self.entry, 4),
            "stop": round(self.stop, 4),
            "shares": self.shares,
            "notional": round(self.notional, 2),
            "risk_dollars": round(self.risk_dollars, 2),
            "risk_pct_equity": round(self.risk_pct_equity, 5),
            "weight": round(self.weight, 5),
            "r_multiple_targets": {k: round(v, 4) for k, v in self.r_multiple_targets.items()},
            "limited_by": self.limited_by,
        }


def plan_position(
    symbol: str,
    equity: float,
    entry: float,
    atr: float,
    cfg: RiskConfig | None = None,
    exposure_cap: float = 1.0,
    open_heat: float = 0.0,
) -> PositionPlan:
    """Size one long position under all active constraints.

    ``limited_by`` reports which constraint actually bound the result, which is
    the difference between a sizing tool you trust and one you argue with.
    """
    cfg = cfg or RiskConfig()

    if not np.isfinite(entry) or entry <= 0:
        raise ValueError(f"{symbol}: entry price must be positive, got {entry!r}")
    if not np.isfinite(atr) or atr <= 0:
        raise ValueError(f"{symbol}: ATR must be positive to derive a stop, got {atr!r}")

    stop = entry - cfg.atr_stop_mult * atr
    if stop <= 0:
        raise ValueError(f"{symbol}: ATR stop falls below zero — instrument is too volatile to size this way")

    per_share_risk = entry - stop

    # 1. risk-based size
    budget = equity * cfg.risk_pct
    # Respect remaining heat before anything else.
    remaining_heat = max(0.0, cfg.max_portfolio_heat - open_heat)
    heat_budget = equity * remaining_heat
    limited_by = "risk_pct"
    if heat_budget < budget:
        budget, limited_by = heat_budget, "portfolio_heat"

    shares = budget / per_share_risk if per_share_risk > 0 else 0.0

    # 2. weight cap (also honours the regime's exposure ceiling)
    weight_cap = min(cfg.max_position_pct, cfg.max_gross_exposure) * max(0.0, min(1.0, exposure_cap))
    max_shares_by_weight = (equity * weight_cap) / entry
    if max_shares_by_weight < shares:
        shares, limited_by = max_shares_by_weight, "max_position_pct" if exposure_cap >= 1.0 else "regime_exposure_cap"

    if cfg.whole_shares:
        shares = float(math.floor(shares))

    notional = shares * entry
    if shares <= 0 or notional < cfg.min_notional:
        shares, notional, limited_by = 0.0, 0.0, "below_min_notional"

    risk_dollars = shares * per_share_risk
    return PositionPlan(
        symbol=symbol,
        entry=entry,
        stop=stop,
        shares=shares,
        notional=notional,
        risk_dollars=risk_dollars,
        risk_pct_equity=(risk_dollars / equity) if equity > 0 else 0.0,
        weight=(notional / equity) if equity > 0 else 0.0,
        r_multiple_targets={
            "1R": entry + per_share_risk,
            "2R": entry + 2 * per_share_risk,
            "3R": entry + 3 * per_share_risk,
        },
        limited_by=limited_by,
    )


def portfolio_heat(positions: pd.DataFrame, equity: float) -> float:
    """Total open risk as a fraction of equity.

    Expects columns ``shares``, ``price``, and ``stop``. Positions already above
    their stop still count their full remaining distance-to-stop as risk.
    """
    if positions.empty or equity <= 0:
        return 0.0
    per_share = (positions["price"] - positions["stop"]).clip(lower=0.0)
    return float((positions["shares"] * per_share).sum() / equity)


def chandelier_stop(high_since_entry: float, atr: float, mult: float = 3.0) -> float:
    """Trailing stop anchored to the highest high since entry."""
    return high_since_entry - mult * atr


def vol_target_scalar(realized_vol_pct: float, target_vol: float = 0.15, cap: float = 1.5) -> float:
    """Exposure multiplier that steers realized volatility toward a target.

    In a calm tape this scales *up* (bounded by ``cap``); when volatility
    expands it scales down roughly linearly, which is the cheapest known way to
    flatten a drawdown profile without predicting anything.
    """
    if not np.isfinite(realized_vol_pct) or realized_vol_pct <= 0:
        return 1.0
    return float(np.clip((target_vol * 100.0) / realized_vol_pct, 0.0, cap))
