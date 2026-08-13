"""Classic technical indicators, computed from daily OHLCV bars.

Everything here is a pure function of a ``pandas`` Series/DataFrame — no network,
no state, no lookahead. Wilder-smoothed indicators (RSI, ATR, ADX) use the same
recursive average IBKR/TWS charts use, so values line up with what you see in
the platform.

Conventions
-----------
* Input frames carry lowercase columns: ``open, high, low, close, volume``.
* Every function returns a Series/DataFrame indexed identically to the input.
* Warm-up periods are ``NaN`` rather than back-filled. Nothing downstream is
  allowed to see a value the market could not have produced yet.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

__all__ = [
    "sma",
    "ema",
    "rma",
    "slope",
    "true_range",
    "atr",
    "natr",
    "rsi",
    "macd",
    "roc",
    "stochastic",
    "bollinger",
    "keltner",
    "donchian",
    "adx",
    "supertrend",
    "aroon",
    "obv",
    "mfi",
    "chaikin_money_flow",
    "realized_vol",
    "drawdown",
    "distance_from_high",
    "rolling_percentile",
    "zscore",
]


# --------------------------------------------------------------------------
# moving averages / smoothing primitives
# --------------------------------------------------------------------------

def sma(s: pd.Series, length: int) -> pd.Series:
    """Simple moving average."""
    return s.rolling(length, min_periods=length).mean()


def ema(s: pd.Series, length: int) -> pd.Series:
    """Exponential moving average (standard 2/(n+1) smoothing)."""
    return s.ewm(span=length, adjust=False, min_periods=length).mean()


def rma(s: pd.Series, length: int) -> pd.Series:
    """Wilder's smoothed moving average — the 1/n recursion used by RSI/ATR/ADX.

    Equivalent to an EMA with ``alpha = 1/length``. This is what separates a
    "matches TWS" RSI from a "close enough" one.
    """
    return s.ewm(alpha=1.0 / length, adjust=False, min_periods=length).mean()


def slope(s: pd.Series, length: int, normalize: bool = True) -> pd.Series:
    """Least-squares slope of ``s`` over a rolling window.

    With ``normalize=True`` the slope is expressed as fraction-of-level per bar,
    which makes it comparable across instruments trading at different prices.
    """
    x = np.arange(length, dtype=float)
    x_centered = x - x.mean()
    denom = (x_centered**2).sum()

    def _fit(window: np.ndarray) -> float:
        return float(np.dot(x_centered, window) / denom)

    raw = s.rolling(length, min_periods=length).apply(_fit, raw=True)
    return raw / s if normalize else raw


# --------------------------------------------------------------------------
# volatility / range
# --------------------------------------------------------------------------

def true_range(df: pd.DataFrame) -> pd.Series:
    """True range: max of today's range and the two gap-adjusted ranges."""
    prev_close = df["close"].shift(1)
    ranges = pd.concat(
        [
            df["high"] - df["low"],
            (df["high"] - prev_close).abs(),
            (df["low"] - prev_close).abs(),
        ],
        axis=1,
    )
    return ranges.max(axis=1)


def atr(df: pd.DataFrame, length: int = 14) -> pd.Series:
    """Average true range (Wilder)."""
    return rma(true_range(df), length)


def natr(df: pd.DataFrame, length: int = 14) -> pd.Series:
    """Normalized ATR — ATR as a percentage of price.

    The unit that actually matters for position sizing: it makes a $30 stock and
    a $1,200 stock directly comparable on risk.
    """
    return 100.0 * atr(df, length) / df["close"]


def realized_vol(close: pd.Series, length: int = 20, periods: int = 252) -> pd.Series:
    """Annualized realized volatility from log returns, in percent."""
    log_ret = np.log(close / close.shift(1))
    return 100.0 * log_ret.rolling(length, min_periods=length).std(ddof=1) * np.sqrt(periods)


# --------------------------------------------------------------------------
# momentum / oscillators
# --------------------------------------------------------------------------

def rsi(close: pd.Series, length: int = 14) -> pd.Series:
    """Relative strength index (Wilder smoothing)."""
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = (-delta).clip(lower=0.0)

    avg_gain = rma(gain, length)
    avg_loss = rma(loss, length)

    rs = avg_gain / avg_loss.replace(0.0, np.nan)
    out = 100.0 - (100.0 / (1.0 + rs))
    # An all-gain window has zero average loss -> RSI is 100 by definition.
    return out.where(avg_loss != 0.0, 100.0).where(avg_gain.notna())


def macd(
    close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9
) -> pd.DataFrame:
    """MACD line, signal line, and histogram."""
    macd_line = ema(close, fast) - ema(close, slow)
    signal_line = macd_line.ewm(span=signal, adjust=False, min_periods=signal).mean()
    return pd.DataFrame(
        {
            "macd": macd_line,
            "signal": signal_line,
            "hist": macd_line - signal_line,
        }
    )


def roc(close: pd.Series, length: int = 63) -> pd.Series:
    """Rate of change over ``length`` bars, in percent (63 ≈ one quarter)."""
    return 100.0 * (close / close.shift(length) - 1.0)


def stochastic(df: pd.DataFrame, k: int = 14, d: int = 3, smooth: int = 3) -> pd.DataFrame:
    """Slow stochastic oscillator (%K and %D)."""
    low_k = df["low"].rolling(k, min_periods=k).min()
    high_k = df["high"].rolling(k, min_periods=k).max()
    span = (high_k - low_k).replace(0.0, np.nan)

    fast_k = 100.0 * (df["close"] - low_k) / span
    slow_k = fast_k.rolling(smooth, min_periods=smooth).mean()
    return pd.DataFrame({"k": slow_k, "d": slow_k.rolling(d, min_periods=d).mean()})


def aroon(df: pd.DataFrame, length: int = 25) -> pd.DataFrame:
    """Aroon up/down/oscillator — how recently the window's extremes were set."""
    def _bars_since_max(w: np.ndarray) -> float:
        return float(len(w) - 1 - int(np.argmax(w)))

    def _bars_since_min(w: np.ndarray) -> float:
        return float(len(w) - 1 - int(np.argmin(w)))

    window = length + 1
    since_high = df["high"].rolling(window, min_periods=window).apply(_bars_since_max, raw=True)
    since_low = df["low"].rolling(window, min_periods=window).apply(_bars_since_min, raw=True)

    up = 100.0 * (length - since_high) / length
    down = 100.0 * (length - since_low) / length
    return pd.DataFrame({"up": up, "down": down, "osc": up - down})


# --------------------------------------------------------------------------
# bands / channels
# --------------------------------------------------------------------------

def bollinger(close: pd.Series, length: int = 20, mult: float = 2.0) -> pd.DataFrame:
    """Bollinger bands plus %B and bandwidth.

    ``bandwidth`` is the squeeze detector: low readings mark volatility
    compression, which historically precedes range expansion.
    """
    mid = sma(close, length)
    sd = close.rolling(length, min_periods=length).std(ddof=0)
    upper = mid + mult * sd
    lower = mid - mult * sd
    span = (upper - lower).replace(0.0, np.nan)
    return pd.DataFrame(
        {
            "mid": mid,
            "upper": upper,
            "lower": lower,
            "pct_b": (close - lower) / span,
            "bandwidth": 100.0 * (upper - lower) / mid,
        }
    )


def keltner(df: pd.DataFrame, length: int = 20, mult: float = 2.0, atr_length: int = 10) -> pd.DataFrame:
    """Keltner channels (EMA basis, ATR envelope)."""
    mid = ema(df["close"], length)
    band = mult * atr(df, atr_length)
    return pd.DataFrame({"mid": mid, "upper": mid + band, "lower": mid - band})


def donchian(df: pd.DataFrame, length: int = 20) -> pd.DataFrame:
    """Donchian channel — the breakout reference used by classic trend systems.

    Extremes are taken over the *prior* ``length`` bars (today excluded) so that
    "close breaks the 20-day high" is a decidable event on the bar it happens.
    """
    upper = df["high"].shift(1).rolling(length, min_periods=length).max()
    lower = df["low"].shift(1).rolling(length, min_periods=length).min()
    return pd.DataFrame({"upper": upper, "lower": lower, "mid": (upper + lower) / 2.0})


# --------------------------------------------------------------------------
# trend strength / direction
# --------------------------------------------------------------------------

def adx(df: pd.DataFrame, length: int = 14) -> pd.DataFrame:
    """Average directional index with +DI / -DI (Wilder).

    ADX measures trend *strength* without direction: readings above ~20-25 say a
    trend is worth following, below ~20 say the tape is chopping and trend
    signals will whipsaw.
    """
    up_move = df["high"].diff()
    down_move = -df["low"].diff()

    plus_dm = pd.Series(
        np.where((up_move > down_move) & (up_move > 0), up_move, 0.0), index=df.index
    )
    minus_dm = pd.Series(
        np.where((down_move > up_move) & (down_move > 0), down_move, 0.0), index=df.index
    )

    atr_ = rma(true_range(df), length).replace(0.0, np.nan)
    plus_di = 100.0 * rma(plus_dm, length) / atr_
    minus_di = 100.0 * rma(minus_dm, length) / atr_

    di_sum = (plus_di + minus_di).replace(0.0, np.nan)
    dx = 100.0 * (plus_di - minus_di).abs() / di_sum
    return pd.DataFrame({"adx": rma(dx, length), "plus_di": plus_di, "minus_di": minus_di})


def supertrend(df: pd.DataFrame, length: int = 10, mult: float = 3.0) -> pd.DataFrame:
    """Supertrend — an ATR band that ratchets in the direction of the trend.

    Returns the active stop line and a ``dir`` column (+1 uptrend, -1 downtrend).
    The recursion is inherently sequential, so this is an explicit loop.
    """
    hl2 = (df["high"] + df["low"]) / 2.0
    band = mult * atr(df, length)
    upper_basic = hl2 + band
    lower_basic = hl2 - band

    close = df["close"].to_numpy(dtype=float)
    ub = upper_basic.to_numpy(dtype=float)
    lb = lower_basic.to_numpy(dtype=float)
    n = len(df)

    final_ub = np.full(n, np.nan)
    final_lb = np.full(n, np.nan)
    direction = np.full(n, np.nan)

    start = int(np.argmax(~np.isnan(ub))) if (~np.isnan(ub)).any() else n
    if start < n:
        final_ub[start] = ub[start]
        final_lb[start] = lb[start]
        direction[start] = 1.0

    for i in range(start + 1, n):
        # Bands only tighten toward price; they reset when price closes through.
        final_ub[i] = ub[i] if (ub[i] < final_ub[i - 1] or close[i - 1] > final_ub[i - 1]) else final_ub[i - 1]
        final_lb[i] = lb[i] if (lb[i] > final_lb[i - 1] or close[i - 1] < final_lb[i - 1]) else final_lb[i - 1]

        if close[i] > final_ub[i - 1]:
            direction[i] = 1.0
        elif close[i] < final_lb[i - 1]:
            direction[i] = -1.0
        else:
            direction[i] = direction[i - 1]

    line = np.where(direction == 1.0, final_lb, final_ub)
    return pd.DataFrame(
        {"supertrend": line, "dir": direction, "upper": final_ub, "lower": final_lb},
        index=df.index,
    )


# --------------------------------------------------------------------------
# volume / flow
# --------------------------------------------------------------------------

def obv(df: pd.DataFrame) -> pd.Series:
    """On-balance volume — cumulative signed volume."""
    sign = np.sign(df["close"].diff()).fillna(0.0)
    return (sign * df["volume"]).cumsum()


def mfi(df: pd.DataFrame, length: int = 14) -> pd.Series:
    """Money flow index — a volume-weighted RSI on typical price."""
    typical = (df["high"] + df["low"] + df["close"]) / 3.0
    raw_flow = typical * df["volume"]
    direction = np.sign(typical.diff())

    pos = raw_flow.where(direction > 0, 0.0).rolling(length, min_periods=length).sum()
    neg = raw_flow.where(direction < 0, 0.0).rolling(length, min_periods=length).sum()

    ratio = pos / neg.replace(0.0, np.nan)
    out = 100.0 - (100.0 / (1.0 + ratio))
    return out.where(neg != 0.0, 100.0).where(pos.notna())


def chaikin_money_flow(df: pd.DataFrame, length: int = 20) -> pd.Series:
    """Chaikin money flow — where each bar closed within its range, volume-weighted."""
    span = (df["high"] - df["low"]).replace(0.0, np.nan)
    mf_multiplier = ((df["close"] - df["low"]) - (df["high"] - df["close"])) / span
    mf_volume = (mf_multiplier * df["volume"]).fillna(0.0)

    vol_sum = df["volume"].rolling(length, min_periods=length).sum().replace(0.0, np.nan)
    return mf_volume.rolling(length, min_periods=length).sum() / vol_sum


# --------------------------------------------------------------------------
# drawdown / positioning helpers
# --------------------------------------------------------------------------

def drawdown(close: pd.Series) -> pd.Series:
    """Percentage drawdown from the running peak (negative or zero)."""
    return 100.0 * (close / close.cummax() - 1.0)


def distance_from_high(close: pd.Series, length: int = 252) -> pd.Series:
    """Percent below the rolling ``length``-bar closing high (negative or zero).

    Near zero means the name is at highs — the condition trend systems want and
    mean-reversion systems avoid.
    """
    high = close.rolling(length, min_periods=1).max()
    return 100.0 * (close / high - 1.0)


def rolling_percentile(s: pd.Series, length: int = 252) -> pd.Series:
    """Percentile rank (0-1) of the latest value within its trailing window.

    Turns any raw indicator into a self-normalizing "is this high or low *for
    this instrument*" reading, which is what makes cross-sectional comparison and
    regime thresholds meaningful.
    """
    def _rank(w: np.ndarray) -> float:
        return float((w <= w[-1]).sum() - 1) / float(len(w) - 1)

    return s.rolling(length, min_periods=max(20, length // 4)).apply(_rank, raw=True)


def zscore(s: pd.Series, length: int = 252) -> pd.Series:
    """Rolling z-score."""
    mean = s.rolling(length, min_periods=max(20, length // 4)).mean()
    sd = s.rolling(length, min_periods=max(20, length // 4)).std(ddof=1)
    return (s - mean) / sd.replace(0.0, np.nan)
