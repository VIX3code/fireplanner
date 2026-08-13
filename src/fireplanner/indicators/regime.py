"""Market-regime, breadth, and relative-strength measures.

A swing system lives or dies on *when* it is allowed to be long, far more than on
which name it picks. This module produces that gate.

The regime score blends four independent read-outs, each derivable from data the
IBKR API already returns:

===============  ==============================================================
Trend            SPY vs its 200d/50d averages and the slope of the 200d
Participation    equal-weight (RSP) vs cap-weight (SPY) — is the rally broad?
Volatility       VIX level and its own trailing percentile
Damage           current drawdown from the 52-week closing high
===============  ==============================================================
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from .core import atr, drawdown, ema, rolling_percentile, rsi, sma, slope

__all__ = [
    "RegimeReading",
    "trend_state",
    "breadth_ratio",
    "vol_state",
    "compute_regime",
    "relative_strength",
    "universe_breadth",
]


# --------------------------------------------------------------------------
# component read-outs
# --------------------------------------------------------------------------

def trend_state(close: pd.Series, fast: int = 50, slow: int = 200) -> pd.DataFrame:
    """Primary trend structure for an index series.

    ``score`` runs 0-1 and awards a quarter each for: price above the slow MA,
    fast MA above slow MA, a rising slow MA, and price above the fast MA.
    """
    ma_fast = sma(close, fast)
    ma_slow = sma(close, slow)
    slow_slope = slope(ma_slow, 21)

    above_slow = (close > ma_slow).astype(float)
    golden = (ma_fast > ma_slow).astype(float)
    rising = (slow_slope > 0).astype(float)
    above_fast = (close > ma_fast).astype(float)

    score = (above_slow + golden + rising + above_fast) / 4.0
    return pd.DataFrame(
        {
            "ma_fast": ma_fast,
            "ma_slow": ma_slow,
            "slow_slope": slow_slope,
            "above_slow": above_slow,
            "above_fast": above_fast,
            "golden_cross": golden,
            "slow_rising": rising,
            "score": score.where(ma_slow.notna()),
        }
    )


def breadth_ratio(equal_weight: pd.Series, cap_weight: pd.Series, length: int = 50) -> pd.DataFrame:
    """Equal-weight vs cap-weight participation.

    When RSP/SPY rises, the average S&P name is keeping up with the index — a
    broad advance. When it falls the index is being carried by a handful of
    mega-caps, which is a materially more fragile tape for swing longs.
    """
    ratio = equal_weight / cap_weight
    ratio_ma = sma(ratio, length)
    ratio_slope = slope(ratio_ma, 21)
    return pd.DataFrame(
        {
            "ratio": ratio,
            "ratio_ma": ratio_ma,
            "ratio_slope": ratio_slope,
            "broadening": (ratio > ratio_ma).astype(float).where(ratio_ma.notna()),
            "score": (ratio_slope > 0).astype(float).where(ratio_slope.notna()),
        }
    )


def vol_state(vix: pd.Series, lookback: int = 252, calm: float = 16.0, stress: float = 25.0) -> pd.DataFrame:
    """Volatility regime from the VIX level and its trailing percentile.

    ``score`` is 1.0 in calm tape and decays to 0.0 as vol stretches, so it can be
    multiplied straight into exposure.
    """
    pct = rolling_percentile(vix, lookback)
    # Linear decay between the calm and stress thresholds.
    level_score = ((stress - vix) / (stress - calm)).clip(0.0, 1.0)
    pct_score = (1.0 - pct).clip(0.0, 1.0)
    return pd.DataFrame(
        {
            "vix": vix,
            "vix_pctile": pct,
            "vix_ma20": sma(vix, 20),
            "elevated": (vix > stress).astype(float),
            "score": (0.5 * level_score + 0.5 * pct_score),
        }
    )


# --------------------------------------------------------------------------
# composite
# --------------------------------------------------------------------------

@dataclass
class RegimeReading:
    """A single day's regime verdict."""

    date: pd.Timestamp
    score: float
    label: str
    exposure_cap: float
    components: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "date": str(self.date.date()) if hasattr(self.date, "date") else str(self.date),
            "score": round(float(self.score), 4),
            "label": self.label,
            "exposure_cap": round(float(self.exposure_cap), 4),
            "components": {k: (None if v is None or (isinstance(v, float) and np.isnan(v)) else round(float(v), 4))
                           for k, v in self.components.items()},
        }


#: Weight of each component in the composite regime score.
REGIME_WEIGHTS = {"trend": 0.40, "breadth": 0.20, "vol": 0.25, "drawdown": 0.15}

#: Regime score -> (label, maximum fraction of equity deployable).
REGIME_BANDS = [
    (0.80, "Risk-On", 1.00),
    (0.60, "Constructive", 0.75),
    (0.40, "Neutral", 0.50),
    (0.20, "Defensive", 0.25),
    (0.00, "Risk-Off", 0.00),
]


def compute_regime(
    index_close: pd.Series,
    vix: pd.Series | None = None,
    equal_weight: pd.Series | None = None,
    weights: dict | None = None,
) -> pd.DataFrame:
    """Blend the components into a daily regime score, label, and exposure cap.

    Missing inputs are dropped and the remaining weights renormalized, so the
    model still runs if you have no VIX or no equal-weight series.
    """
    weights = dict(weights or REGIME_WEIGHTS)
    trend = trend_state(index_close)

    parts: dict[str, pd.Series] = {"trend": trend["score"]}

    vol_df = vol_state(vix.reindex(index_close.index).ffill()) if vix is not None else None
    if vol_df is not None:
        parts["vol"] = vol_df["score"]

    breadth_df = None
    if equal_weight is not None:
        breadth_df = breadth_ratio(equal_weight.reindex(index_close.index).ffill(), index_close)
        parts["breadth"] = breadth_df["score"]

    dd = drawdown(index_close)
    # Full credit at highs, zero credit at -20% or worse.
    parts["drawdown"] = (1.0 + dd / 20.0).clip(0.0, 1.0)

    active = {k: v for k, v in parts.items() if k in weights}
    total_w = sum(weights[k] for k in active)
    score = sum(weights[k] * active[k] for k in active) / total_w

    out = pd.DataFrame({"score": score})
    for name, series in active.items():
        out[f"c_{name}"] = series

    out["label"] = [_band(s)[0] for s in out["score"]]
    out["exposure_cap"] = [_band(s)[1] for s in out["score"]]

    out["ma_fast"] = trend["ma_fast"]
    out["ma_slow"] = trend["ma_slow"]
    out["drawdown"] = dd
    if vol_df is not None:
        out["vix"] = vol_df["vix"]
        out["vix_pctile"] = vol_df["vix_pctile"]
    if breadth_df is not None:
        out["breadth_ratio"] = breadth_df["ratio"]
        out["breadth_ratio_ma"] = breadth_df["ratio_ma"]

    return out


def _band(score: float) -> tuple[str, float]:
    if score is None or (isinstance(score, float) and np.isnan(score)):
        return ("Unknown", 0.0)
    for threshold, label, cap in REGIME_BANDS:
        if score >= threshold:
            return (label, cap)
    return ("Risk-Off", 0.0)


def latest_regime(regime: pd.DataFrame) -> RegimeReading:
    """Extract the most recent complete regime row as a structured reading."""
    valid = regime.dropna(subset=["score"])
    if valid.empty:
        raise ValueError("regime frame has no complete rows — need >200 bars of history")
    row = valid.iloc[-1]
    components = {c[2:]: row[c] for c in valid.columns if c.startswith("c_")}
    return RegimeReading(
        date=valid.index[-1],
        score=float(row["score"]),
        label=str(row["label"]),
        exposure_cap=float(row["exposure_cap"]),
        components=components,
    )


# --------------------------------------------------------------------------
# cross-sectional helpers
# --------------------------------------------------------------------------

def relative_strength(close: pd.Series, benchmark: pd.Series, length: int = 63) -> pd.DataFrame:
    """Performance of a name against a benchmark over ``length`` bars.

    ``rs_line`` is the raw ratio; ``excess`` is the return difference in percent.
    A name whose RS line is making highs while the index chops is the classic
    swing-long candidate.
    """
    bench = benchmark.reindex(close.index).ffill()
    rs_line = close / bench
    stock_ret = close / close.shift(length) - 1.0
    bench_ret = bench / bench.shift(length) - 1.0
    return pd.DataFrame(
        {
            "rs_line": rs_line,
            "rs_ma": sma(rs_line, 50),
            "excess": 100.0 * (stock_ret - bench_ret),
            "rs_rising": (rs_line > sma(rs_line, 50)).astype(float),
        }
    )


def universe_breadth(closes: pd.DataFrame, fast: int = 50, slow: int = 200) -> pd.DataFrame:
    """Percent of a universe trading above its own moving averages.

    Feed this a wide frame of closes (one column per symbol) and you get the
    breadth series most desks watch. With a full S&P 500 constituent pull this is
    the real thing; with a sector-ETF panel it is a serviceable proxy.
    """
    above_fast = closes.gt(closes.rolling(fast, min_periods=fast).mean())
    above_slow = closes.gt(closes.rolling(slow, min_periods=slow).mean())
    valid_fast = closes.rolling(fast, min_periods=fast).mean().notna()
    valid_slow = closes.rolling(slow, min_periods=slow).mean().notna()

    return pd.DataFrame(
        {
            f"pct_above_{fast}": 100.0 * above_fast.where(valid_fast).sum(axis=1) / valid_fast.sum(axis=1),
            f"pct_above_{slow}": 100.0 * above_slow.where(valid_slow).sum(axis=1) / valid_slow.sum(axis=1),
            "n_symbols": valid_fast.sum(axis=1),
        }
    )
