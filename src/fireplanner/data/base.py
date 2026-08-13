"""The data contract every IBKR transport implements.

Three transports ship with this package and all of them satisfy `BarProvider`:

===================  =========================================================
`SnapshotProvider`   CSV/JSON committed to the repo. No network. Used by tests
                     and by the dashboard when the gateway is not running.
`GatewayProvider`    TWS / IB Gateway socket API via ``ib_async``. Full history,
                     full order entry. This is the production path.
`WebApiProvider`     Client Portal Web API (REST). No desktop app required, but
                     the session must be kept alive by re-authenticating.
===================  =========================================================

Because the scanner, backtester, and dashboard only ever see this interface,
you can develop and backtest offline and flip to live data by changing one line.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

import pandas as pd

__all__ = [
    "Bar",
    "Contract",
    "BarProvider",
    "normalize_bars",
    "drop_incomplete_last_bar",
    "OHLCV_COLUMNS",
]

OHLCV_COLUMNS = ["open", "high", "low", "close", "volume"]


@dataclass(frozen=True)
class Contract:
    """An instrument resolved to an IBKR contract id."""

    symbol: str
    con_id: int
    sec_type: str = "STK"
    exchange: str = "SMART"
    currency: str = "USD"
    description: str = ""

    def as_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "con_id": self.con_id,
            "sec_type": self.sec_type,
            "exchange": self.exchange,
            "currency": self.currency,
            "description": self.description,
        }


@dataclass(frozen=True)
class Bar:
    """A single OHLCV bar."""

    date: pd.Timestamp
    open: float
    high: float
    low: float
    close: float
    volume: float


@runtime_checkable
class BarProvider(Protocol):
    """Minimal surface the rest of the system depends on."""

    def resolve(self, symbol: str) -> Contract:
        """Map a ticker to a Contract, raising LookupError if it cannot."""
        ...

    def history(self, symbol: str, lookback_days: int = 1260, bar_size: str = "1 day") -> pd.DataFrame:
        """Return an OHLCV frame indexed by date, oldest first."""
        ...


def normalize_bars(df: pd.DataFrame) -> pd.DataFrame:
    """Coerce any provider's output into the canonical frame.

    Guarantees a sorted, unique `DatetimeIndex` named ``date``, lowercase OHLCV
    float columns, and no duplicate sessions — the invariants every indicator in
    this package assumes.
    """
    out = df.copy()
    out.columns = [str(c).lower() for c in out.columns]

    if "date" in out.columns:
        out = out.set_index("date")
    out.index = pd.to_datetime(out.index)
    # Bars are daily; drop any intraday component so joins across providers line up.
    out.index = out.index.normalize()
    out.index.name = "date"

    for col in OHLCV_COLUMNS:
        if col not in out.columns:
            if col == "volume":
                out[col] = 0.0
            else:
                raise ValueError(f"bar frame is missing required column {col!r}")
        out[col] = pd.to_numeric(out[col], errors="coerce")

    out = out[OHLCV_COLUMNS]
    out = out[~out.index.duplicated(keep="last")].sort_index()
    out = out.dropna(subset=["close"])

    # Repair the OHLC invariant.
    #
    # IBKR's SMART-aggregated daily bars occasionally report a close a cent or two
    # outside the session's own high/low — the consolidated closing-auction print
    # lands outside the range built from the aggregated intraday feed. It shows up
    # on roughly 1% of SPY sessions (e.g. 2022-03-17: high 441.02, close 441.07).
    #
    # Left alone it silently corrupts anything reading the range: true range,
    # ATR-derived stops, %B, Donchian breaks. Widening the bar to contain its own
    # open and close is the conservative repair — it never narrows a range, and the
    # adjustment is bounded by the discrepancy itself.
    out["high"] = out[["high", "open", "close"]].max(axis=1)
    out["low"] = out[["low", "open", "close"]].min(axis=1)
    return out


def drop_incomplete_last_bar(
    df: pd.DataFrame,
    now: pd.Timestamp | None = None,
    tz: str = "America/New_York",
    close_hour: int = 16,
    close_minute: int = 0,
) -> pd.DataFrame:
    """Drop a final bar that belongs to a session still in progress.

    IBKR happily returns today's partially-formed daily bar while the market is
    open. Ingesting it is silently wrong for a system that only ever acts on
    completed sessions: the close is really "the price right now", the high and
    low are partial, and the volume is a fraction of the day's. Every indicator
    downstream — the moving averages, ATR, the score, the allocation target —
    then reflects a bar that does not exist yet, and can flip a signal that will
    read differently at 16:00.

    Observed live: SPY's 2026-08-13 bar mid-session carried 5.9M shares against a
    20-30M daily norm, and a "close" that was simply the last print.

    The rule is deliberately conservative: drop the last bar only when its date
    is the current exchange-local date *and* the session has not yet closed.
    Historical bars are never touched, and after the close nothing is dropped.
    """
    if df.empty:
        return df

    try:
        from zoneinfo import ZoneInfo
    except ImportError:  # pragma: no cover - Python < 3.9
        return df

    zone = ZoneInfo(tz)
    if now is None:
        now_local = pd.Timestamp.now("UTC").tz_convert(zone)
    else:
        stamp = pd.Timestamp(now)
        # A naive timestamp is taken as UTC; an aware one is converted.
        stamp = stamp.tz_localize("UTC") if stamp.tzinfo is None else stamp
        now_local = stamp.tz_convert(zone)

    last_date = pd.Timestamp(df.index[-1]).date()
    if last_date != now_local.date():
        return df

    session_close = now_local.replace(
        hour=close_hour, minute=close_minute, second=0, microsecond=0
    )
    if now_local >= session_close:
        return df

    return df.iloc[:-1]
