"""The swing model: turn an enriched bar frame into a 0-100 score and an action.

Design intent
-------------
This is a **trend-following system with a pullback entry filter**, gated by
market regime. It is deliberately not a prediction engine. Each of the four
blocks answers one question:

============  =========================================================  ======
Block         Question                                                   Weight
============  =========================================================  ======
``trend``     Is this name in an uptrend at all?                          35%
``momentum``  Is that trend being paid for right now?                     30%
``entry``     Is *today* a sane place to start, or is it extended?        20%
``quality``   Is it tradeable — sized, liquid, not falling apart?         15%
============  =========================================================  ======

The `entry` block is what separates this from a momentum chase: a name at RSI 85
three ATRs above its 21-EMA scores *worse* than the same name resting on its
anchor, because the swing edge lives in the pullback, not the spike.

Nothing here peeks: every column is computed from bars up to and including the
signal date, and the backtester fills on the *next* open.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

__all__ = ["ScoreWeights", "SignalConfig", "score_frame", "latest_signal", "Signal", "ACTIONS"]


ACTIONS = ["BUY", "ADD", "HOLD", "REDUCE", "AVOID"]


@dataclass(frozen=True)
class ScoreWeights:
    trend: float = 0.35
    momentum: float = 0.30
    entry: float = 0.20
    quality: float = 0.15

    def normalized(self) -> dict[str, float]:
        total = self.trend + self.momentum + self.entry + self.quality
        return {
            "trend": self.trend / total,
            "momentum": self.momentum / total,
            "entry": self.entry / total,
            "quality": self.quality / total,
        }


@dataclass(frozen=True)
class SignalConfig:
    """Thresholds that turn a continuous score into a discrete action."""

    weights: ScoreWeights = field(default_factory=ScoreWeights)
    buy_threshold: float = 65.0
    add_threshold: float = 55.0
    reduce_threshold: float = 40.0
    #: Require the primary uptrend structure before any long is allowed.
    require_trend_gate: bool = True
    #: ADX below this is treated as "no trend to follow" and caps the score.
    min_adx: float = 18.0
    #: RSI above this is "extended" — entry quality collapses.
    extended_rsi: float = 78.0
    #: Distance from the 21-EMA (in ATRs) beyond which a name is chasing.
    extended_atr: float = 3.0


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def _clip01(s: pd.Series | float) -> pd.Series | float:
    return np.clip(s, 0.0, 1.0)


def _ramp(s: pd.Series, low: float, high: float) -> pd.Series:
    """Linear 0→1 ramp between ``low`` and ``high`` (inverted if low > high)."""
    return _clip01((s - low) / (high - low))


def _tent(s: pd.Series, lo: float, best_lo: float, best_hi: float, hi: float) -> pd.Series:
    """Trapezoid preference curve: 1.0 inside the sweet spot, 0.0 outside the tails.

    Used for indicators where *middle* is good and both extremes are bad — RSI in
    an uptrend being the canonical case.
    """
    rising = _ramp(s, lo, best_lo)
    falling = 1.0 - _ramp(s, best_hi, hi)
    return _clip01(np.minimum(rising, falling))


# --------------------------------------------------------------------------
# blocks
# --------------------------------------------------------------------------

def _trend_block(df: pd.DataFrame, cfg: SignalConfig) -> pd.DataFrame:
    above50 = (df["close"] > df["sma50"]).astype(float)
    above200 = (df["close"] > df["sma200"]).astype(float)
    stacked = (df["sma50"] > df["sma200"]).astype(float)
    rising200 = (df["sma200_slope"] > 0).astype(float)
    st_up = (df["supertrend_dir"] > 0).astype(float)
    # ADX earns partial credit from 15 up to 35 — strength, not just direction.
    adx_q = _ramp(df["adx"], 15.0, 35.0)

    score = (above50 + above200 + stacked + rising200 + st_up + adx_q) / 6.0
    return pd.DataFrame(
        {
            "trend": score,
            "gate": ((above50 > 0) & (stacked > 0) & (st_up > 0)).astype(float),
            "t_above50": above50,
            "t_above200": above200,
            "t_stacked": stacked,
            "t_adx": adx_q,
        }
    )


def _momentum_block(df: pd.DataFrame) -> pd.DataFrame:
    # Quarterly and monthly rate of change, squashed so +20% ≈ full credit.
    roc63 = _ramp(df["roc63"], -5.0, 20.0)
    roc21 = _ramp(df["roc21"], -3.0, 10.0)
    macd_pos = (df["macd_hist"] > 0).astype(float)
    # 12-1 momentum needs 252 bars; fall back to roc126 on shorter histories.
    long_mom = df["mom_12_1"] if "mom_12_1" in df else pd.Series(np.nan, index=df.index)
    long_mom = long_mom.fillna(df.get("roc126", pd.Series(np.nan, index=df.index)))
    long_q = _ramp(long_mom, -10.0, 40.0).fillna(0.5)

    score = 0.30 * roc63 + 0.20 * roc21 + 0.20 * macd_pos + 0.30 * long_q
    return pd.DataFrame({"momentum": score, "m_roc63": roc63, "m_macd": macd_pos, "m_long": long_q})


def _entry_block(df: pd.DataFrame, cfg: SignalConfig) -> pd.DataFrame:
    """Reward proximity to the trend anchor; punish vertical, overbought tape."""
    # RSI sweet spot for a *continuation* entry: pulled back but not broken.
    rsi_q = _tent(df["rsi14"], 30.0, 45.0, 65.0, cfg.extended_rsi)
    # Distance from the 21-EMA in ATRs: at or just above the anchor is ideal.
    stretch = df["atr_from_ema21"]
    stretch_q = _tent(stretch, -3.0, -0.5, 1.0, cfg.extended_atr)
    # %B mid-band is constructive; riding the upper band is late.
    bb_q = _tent(df["bb_pct_b"], -0.1, 0.35, 0.85, 1.15)
    # A volatility squeeze is a setup, not a warning.
    squeeze_q = 1.0 - df["bb_squeeze"].fillna(0.5)

    score = 0.35 * rsi_q + 0.35 * stretch_q + 0.20 * bb_q + 0.10 * squeeze_q
    return pd.DataFrame(
        {
            "entry": score,
            "e_rsi": rsi_q,
            "e_stretch": stretch_q,
            "e_extended": ((df["rsi14"] > cfg.extended_rsi) | (stretch > cfg.extended_atr)).astype(float),
        }
    )


def _quality_block(df: pd.DataFrame) -> pd.DataFrame:
    # Very high relative volatility is a sizing problem, not an opportunity.
    vol_q = 1.0 - df["natr_pctile"].fillna(0.5)
    # Names near their 52-week high are the ones trends actually continue from.
    prox_q = _ramp(df["dist_52w_high"], -35.0, -2.0)
    # Volume and flow confirmation, when volume is available.
    if "cmf20" in df:
        flow_q = _ramp(df["cmf20"], -0.15, 0.15).fillna(0.5)
    else:
        flow_q = pd.Series(0.5, index=df.index)
    if "vol_ratio" in df:
        part_q = _ramp(df["vol_ratio"], 0.6, 1.3).fillna(0.5)
    else:
        part_q = pd.Series(0.5, index=df.index)

    score = 0.35 * vol_q + 0.35 * prox_q + 0.20 * flow_q + 0.10 * part_q
    return pd.DataFrame({"quality": score, "q_vol": vol_q, "q_prox": prox_q, "q_flow": flow_q})


# --------------------------------------------------------------------------
# public API
# --------------------------------------------------------------------------

def score_frame(
    enriched: pd.DataFrame,
    cfg: SignalConfig | None = None,
    regime: pd.Series | None = None,
) -> pd.DataFrame:
    """Score every bar of an enriched frame.

    Parameters
    ----------
    enriched:
        Output of :func:`fireplanner.indicators.enrich`.
    cfg:
        Thresholds; defaults are the shipped configuration.
    regime:
        Optional daily regime score (0-1) aligned to ``enriched``. When supplied
        it scales the final score, so a great chart in a bad tape is correctly
        demoted rather than blindly bought.
    """
    cfg = cfg or SignalConfig()
    w = cfg.weights.normalized()

    trend = _trend_block(enriched, cfg)
    momentum = _momentum_block(enriched)
    entry = _entry_block(enriched, cfg)
    quality = _quality_block(enriched)

    raw = (
        w["trend"] * trend["trend"]
        + w["momentum"] * momentum["momentum"]
        + w["entry"] * entry["entry"]
        + w["quality"] * quality["quality"]
    )

    out = pd.concat([trend, momentum, entry, quality], axis=1)
    out["raw_score"] = 100.0 * raw

    if regime is not None:
        reg = regime.reindex(enriched.index).ffill()
        # Blend rather than multiply outright: even in a soft tape a genuinely
        # strong name keeps some of its score, but never all of it.
        out["regime"] = reg
        out["score"] = out["raw_score"] * (0.4 + 0.6 * reg.clip(0.0, 1.0))
    else:
        out["regime"] = np.nan
        out["score"] = out["raw_score"]

    # Chop penalty: without a trend to follow, cap the score at neutral.
    no_trend = enriched["adx"] < cfg.min_adx
    out.loc[no_trend, "score"] = out.loc[no_trend, "score"].clip(upper=55.0)

    out["action"] = _actions(out, enriched, cfg)
    out["close"] = enriched["close"]
    out["atr14"] = enriched["atr14"]
    return out


def _actions(scored: pd.DataFrame, enriched: pd.DataFrame, cfg: SignalConfig) -> pd.Series:
    score = scored["score"]
    gate = scored["gate"] > 0 if cfg.require_trend_gate else pd.Series(True, index=score.index)
    broken = enriched["close"] < enriched["sma50"]

    action = pd.Series("HOLD", index=score.index, dtype=object)
    action[score < cfg.reduce_threshold] = "REDUCE"
    action[(score >= cfg.add_threshold) & gate] = "ADD"
    action[(score >= cfg.buy_threshold) & gate] = "BUY"
    # A close below the 50-day is the system's structural stop, whatever the score.
    action[broken] = "REDUCE"
    action[(~gate) & (score < cfg.reduce_threshold)] = "AVOID"
    return action


@dataclass
class Signal:
    """A single name's current verdict, ready for display or order staging."""

    symbol: str
    date: pd.Timestamp
    score: float
    action: str
    close: float
    atr: float
    blocks: dict = field(default_factory=dict)
    detail: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        def clean(v):
            if v is None:
                return None
            f = float(v)
            return None if np.isnan(f) else round(f, 4)

        return {
            "symbol": self.symbol,
            "date": str(self.date.date()) if hasattr(self.date, "date") else str(self.date),
            "score": clean(self.score),
            "action": self.action,
            "close": clean(self.close),
            "atr": clean(self.atr),
            "blocks": {k: clean(v) for k, v in self.blocks.items()},
            "detail": {k: clean(v) for k, v in self.detail.items()},
        }


def latest_signal(symbol: str, scored: pd.DataFrame, enriched: pd.DataFrame | None = None) -> Signal:
    """Pull the most recent scored row into a `Signal`."""
    valid = scored.dropna(subset=["score"])
    if valid.empty:
        raise ValueError(f"{symbol}: no scoreable bars — need at least ~200 sessions of history")
    row = valid.iloc[-1]

    detail = {}
    if enriched is not None:
        e = enriched.loc[valid.index[-1]]
        detail = {
            k: e.get(k)
            for k in [
                "rsi14", "adx", "natr14", "macd_hist", "bb_pct_b", "atr_from_ema21",
                "dist_52w_high", "pct_from_sma50", "pct_from_sma200", "roc63",
                "supertrend", "supertrend_dir", "sma50", "sma200", "rvol20", "mfi14",
            ]
            if k in e
        }

    return Signal(
        symbol=symbol,
        date=valid.index[-1],
        score=row["score"],
        action=row["action"],
        close=row["close"],
        atr=row["atr14"],
        blocks={k: row[k] for k in ["trend", "momentum", "entry", "quality", "raw_score", "regime"] if k in row},
        detail=detail,
    )
