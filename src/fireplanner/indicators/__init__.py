"""Indicator library: pure-pandas technicals plus regime/breadth aggregates."""

from __future__ import annotations

import pandas as pd

from .core import (
    adx,
    aroon,
    atr,
    bollinger,
    chaikin_money_flow,
    distance_from_high,
    donchian,
    drawdown,
    ema,
    keltner,
    macd,
    mfi,
    natr,
    obv,
    realized_vol,
    rma,
    roc,
    rolling_percentile,
    rsi,
    slope,
    sma,
    stochastic,
    supertrend,
    true_range,
    zscore,
)
from .regime import (
    REGIME_BANDS,
    REGIME_WEIGHTS,
    RegimeReading,
    breadth_ratio,
    compute_regime,
    latest_regime,
    relative_strength,
    term_structure,
    trend_state,
    universe_breadth,
    vol_state,
)

__all__ = [
    "adx", "aroon", "atr", "bollinger", "chaikin_money_flow", "distance_from_high",
    "donchian", "drawdown", "ema", "keltner", "macd", "mfi", "natr", "obv",
    "realized_vol", "rma", "roc", "rolling_percentile", "rsi", "slope", "sma",
    "stochastic", "supertrend", "true_range", "zscore",
    "REGIME_BANDS", "REGIME_WEIGHTS", "RegimeReading", "breadth_ratio",
    "compute_regime", "latest_regime", "relative_strength", "term_structure",
    "trend_state", "universe_breadth", "vol_state",
    "enrich",
]


def enrich(df: pd.DataFrame) -> pd.DataFrame:
    """Attach the full standard indicator set to an OHLCV frame.

    This is the single entry point the scanner, backtester, and dashboard all
    use, so every consumer sees identical column names and identical maths.
    """
    out = df.copy()

    # Trend
    out["sma20"] = sma(df["close"], 20)
    out["sma50"] = sma(df["close"], 50)
    out["sma200"] = sma(df["close"], 200)
    out["ema10"] = ema(df["close"], 10)
    out["ema21"] = ema(df["close"], 21)
    out["sma200_slope"] = slope(out["sma200"], 21)
    out["sma50_slope"] = slope(out["sma50"], 21)

    adx_df = adx(df)
    out["adx"] = adx_df["adx"]
    out["plus_di"] = adx_df["plus_di"]
    out["minus_di"] = adx_df["minus_di"]

    st = supertrend(df)
    out["supertrend"] = st["supertrend"]
    out["supertrend_dir"] = st["dir"]

    dc = donchian(df, 20)
    out["donchian_hi"] = dc["upper"]
    out["donchian_lo"] = dc["lower"]

    # Momentum
    out["rsi14"] = rsi(df["close"], 14)
    macd_df = macd(df["close"])
    out["macd"] = macd_df["macd"]
    out["macd_signal"] = macd_df["signal"]
    out["macd_hist"] = macd_df["hist"]
    out["roc21"] = roc(df["close"], 21)
    out["roc63"] = roc(df["close"], 63)
    out["roc126"] = roc(df["close"], 126)
    # 12-1 momentum: the academic workhorse — last 12 months, skipping the most
    # recent month to sidestep short-term reversal.
    out["mom_12_1"] = 100.0 * (df["close"].shift(21) / df["close"].shift(252) - 1.0)

    stoch = stochastic(df)
    out["stoch_k"] = stoch["k"]
    out["stoch_d"] = stoch["d"]

    # Volatility
    out["atr14"] = atr(df, 14)
    out["natr14"] = natr(df, 14)
    out["natr_pctile"] = rolling_percentile(out["natr14"], 252)
    out["rvol20"] = realized_vol(df["close"], 20)
    out["rvol60"] = realized_vol(df["close"], 60)

    bb = bollinger(df["close"])
    out["bb_upper"] = bb["upper"]
    out["bb_lower"] = bb["lower"]
    out["bb_pct_b"] = bb["pct_b"]
    out["bb_bandwidth"] = bb["bandwidth"]
    out["bb_squeeze"] = rolling_percentile(bb["bandwidth"], 252)

    # Volume / flow
    if "volume" in df.columns:
        out["obv"] = obv(df)
        out["obv_slope"] = slope(out["obv"], 21, normalize=False)
        out["mfi14"] = mfi(df, 14)
        out["cmf20"] = chaikin_money_flow(df, 20)
        out["vol_sma50"] = sma(df["volume"], 50)
        out["vol_ratio"] = df["volume"] / out["vol_sma50"]

    # Position within the longer-run range
    out["drawdown"] = drawdown(df["close"])
    out["dist_52w_high"] = distance_from_high(df["close"], 252)
    out["pct_from_sma50"] = 100.0 * (df["close"] / out["sma50"] - 1.0)
    out["pct_from_sma200"] = 100.0 * (df["close"] / out["sma200"] - 1.0)
    # How stretched price is from its anchor, in ATR units — the scale-free way
    # to ask "is this extended?"
    out["atr_from_ema21"] = (df["close"] - out["ema21"]) / out["atr14"]

    return out
