"""A pass-through cache so repeated scans do not re-hit the broker.

IBKR historical-data requests are pacing-limited (roughly 60 requests per
10 minutes, and identical requests inside 15 seconds are rejected outright). A
scan across a few hundred names will trip that instantly without a cache.

Bars for completed sessions never change, so caching them is safe. The cache
keys on ``symbol + bar_size`` and refreshes when the newest cached bar is older
than ``max_age_days`` trading days.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

from .base import BarProvider, Contract, normalize_bars

__all__ = ["CachedProvider"]


class CachedProvider:
    """Wraps any `BarProvider` with an on-disk parquet (or CSV) cache."""

    def __init__(
        self,
        inner: BarProvider,
        root: str | Path = ".cache/bars",
        max_age_days: int = 1,
        throttle_seconds: float = 0.0,
    ):
        self.inner = inner
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.max_age_days = max_age_days
        self.throttle_seconds = throttle_seconds
        self._last_call = 0.0

    # -- plumbing --------------------------------------------------------
    def _path(self, symbol: str, bar_size: str) -> Path:
        slug = bar_size.replace(" ", "")
        return self.root / f"{symbol.upper()}__{slug}.parquet"

    def _read(self, path: Path) -> pd.DataFrame | None:
        if not path.exists():
            return None
        try:
            return normalize_bars(pd.read_parquet(path))
        except Exception:
            # A corrupt or pyarrow-less cache entry should never break a scan.
            try:
                return normalize_bars(pd.read_csv(path.with_suffix(".csv"), parse_dates=["date"]))
            except Exception:
                return None

    def _write(self, path: Path, df: pd.DataFrame) -> None:
        try:
            df.to_parquet(path)
        except Exception:
            # No pyarrow installed — fall back to CSV rather than failing.
            df.to_csv(path.with_suffix(".csv"))

    def _is_fresh(self, df: pd.DataFrame) -> bool:
        if df is None or df.empty:
            return False
        newest = df.index[-1]
        if newest.tzinfo is not None:
            newest = newest.tz_localize(None)
        age = datetime.now(timezone.utc).replace(tzinfo=None) - newest.to_pydatetime()
        # Allow for weekends and holidays on top of the configured tolerance.
        return age <= timedelta(days=self.max_age_days + 3)

    def _throttle(self) -> None:
        if self.throttle_seconds <= 0:
            return
        wait = self.throttle_seconds - (time.monotonic() - self._last_call)
        if wait > 0:
            time.sleep(wait)
        self._last_call = time.monotonic()

    # -- BarProvider -----------------------------------------------------
    def resolve(self, symbol: str) -> Contract:
        return self.inner.resolve(symbol)

    def history(self, symbol: str, lookback_days: int = 1260, bar_size: str = "1 day") -> pd.DataFrame:
        path = self._path(symbol, bar_size)
        cached = self._read(path)

        if cached is not None and self._is_fresh(cached) and len(cached) >= lookback_days:
            return cached.tail(lookback_days)

        self._throttle()
        try:
            fresh = self.inner.history(symbol, lookback_days=lookback_days, bar_size=bar_size)
        except Exception:
            # Serving slightly stale bars beats failing a whole scan on one name.
            if cached is not None:
                return cached.tail(lookback_days)
            raise

        if cached is not None:
            fresh = normalize_bars(pd.concat([cached, fresh]))

        self._write(path, fresh)
        return fresh.tail(lookback_days)

    def clear(self, symbol: str | None = None) -> int:
        """Delete cache entries; returns how many files were removed."""
        pattern = f"{symbol.upper()}__*" if symbol else "*"
        removed = 0
        for p in self.root.glob(pattern):
            p.unlink()
            removed += 1
        return removed
