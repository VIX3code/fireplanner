"""Market breadth: how many of a market's big stocks are above their 20, 50 and 200-day averages.

An index can hold up on a handful of giants while most stocks fall. Breadth
shows that: in a healthy market most members sit above their averages; when
the index is near a high but under 40% of its members are above their 50-day,
the advance is narrow and new buys fail more often.

Each market has a basket of large, liquid constituents (about 20 to 30 names,
configurable under ``trading.breadth.baskets``). Their daily bars are fetched
a few per cycle, to stay inside IBKR's pacing limit of about 60 historical
requests per 10 minutes, and reused for the rest of the day. The reading is
used once at least 60% of a basket has data.

The breadth score is ``0.2 × %above-20d + 0.4 × %above-50d + 0.4 × %above-200d``,
from 0 to 1. The market weather blends it with the regime score
(``rules.breadth_weight``, 25% by default), so broad participation lifts a
market's weather and a narrow one pulls it down, a notch at most.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date
from typing import Callable

import pandas as pd

from .broker import PacingDeferred

__all__ = ["DEFAULT_BASKETS", "BreadthReading", "breadth_of", "MarketBreadth"]

#: Large, liquid constituents per market. Tickers as IBKR lists them.
DEFAULT_BASKETS: dict[str, list[str]] = {
    "US": ["AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "AVGO", "TSLA", "JPM", "LLY",
           "V", "XOM", "UNH", "MA", "COST", "HD", "PG", "JNJ", "WMT", "NFLX",
           "ABBV", "BAC", "CRM", "ORCL", "KO", "CVX", "MRK", "PEP", "AMD", "CSCO"],
    "LSE": ["AZN", "SHEL", "HSBA", "ULVR", "RIO", "GSK", "REL", "DGE", "BATS", "LSEG",
            "GLEN", "BARC", "LLOY", "AAL", "VOD", "TSCO", "NWG", "PRU", "CPG", "EXPN"],
    "SEHK": ["700", "9988", "939", "1299", "5", "941", "3690", "1398", "388", "2318",
             "1810", "9618", "883", "3988", "2", "1", "16", "11", "27", "1211"],
    "SGX": ["D05", "O39", "U11", "Z74", "C6L", "BN4", "S63", "C38U", "A17U", "G13",
            "F34", "Y92", "S68", "V03", "U96", "N2IU", "M44U", "C09"],
    "TSEJ": ["7203", "6758", "8306", "9984", "6861", "8035", "9983", "6501", "9432", "8316",
             "4063", "7974", "6098", "8058", "8001", "4502", "6367", "7267", "6902", "9433"],
}

_WINDOWS = (20, 50, 200)
_WEIGHTS = {20: 0.2, 50: 0.4, 200: 0.4}


@dataclass
class BreadthReading:
    market: str
    pct20: float | None
    pct50: float | None
    pct200: float | None
    #: 0..1, or None when too little of the basket has data.
    score: float | None
    counted: int
    basket: int
    as_of: str | None = None

    @property
    def coverage(self) -> float:
        return self.counted / self.basket if self.basket else 0.0

    def as_dict(self) -> dict:
        d = asdict(self)
        d["coverage"] = self.coverage
        return d


def breadth_of(closes: dict[str, pd.Series]) -> tuple[dict[int, float | None], int, str | None]:
    """Share of series whose last close is above each moving average.

    A series counts toward a window only if it has that many bars, so a recent
    listing doesn't distort the 200-day figure.
    """
    above = {w: 0 for w in _WINDOWS}
    have = {w: 0 for w in _WINDOWS}
    last_day = None
    for series in closes.values():
        s = series.dropna()
        if s.empty:
            continue
        last = float(s.iloc[-1])
        day = s.index[-1]
        last_day = day if last_day is None or day > last_day else last_day
        for w in _WINDOWS:
            if len(s) >= w:
                have[w] += 1
                if last > float(s.iloc[-w:].mean()):
                    above[w] += 1
    pct = {w: (above[w] / have[w] if have[w] else None) for w in _WINDOWS}
    counted = sum(1 for s in closes.values() if len(s.dropna()) >= 50)
    return pct, counted, (str(pd.Timestamp(last_day).date()) if last_day is not None else None)


class MarketBreadth:
    """Fetches each basket a few stocks per cycle and keeps the day's closes."""

    def __init__(self, baskets: dict[str, list[str]], bars: Callable[[str], pd.DataFrame],
                 per_cycle: int = 8, min_coverage: float = 0.6):
        self.baskets = {m: list(dict.fromkeys(str(s).upper() for s in syms)) for m, syms in baskets.items()}
        self.bars, self.per_cycle, self.min_coverage = bars, per_cycle, min_coverage
        self._day: date | None = None
        self._closes: dict[str, dict[str, pd.Series]] = {m: {} for m in self.baskets}
        self._failed: dict[str, set[str]] = {m: set() for m in self.baskets}

    @staticmethod
    def _text(symbol: str, market: str) -> str:
        return symbol if market == "US" else f"{symbol}:{market}"

    def pending(self) -> int:
        return sum(len(syms) - len(self._closes[m]) - len(self._failed[m]) for m, syms in self.baskets.items())

    def step(self, today: date) -> int:
        """Fetch up to ``per_cycle`` stocks not yet read today. Returns how many were fetched."""
        if self._day != today:
            self._day = today
            self._closes = {m: {} for m in self.baskets}
            self._failed = {m: set() for m in self.baskets}
        n = 0
        for market, syms in self.baskets.items():
            for sym in syms:
                if n >= self.per_cycle:
                    return n
                if sym in self._closes[market] or sym in self._failed[market]:
                    continue
                try:
                    self._closes[market][sym] = self.bars(self._text(sym, market))["close"]
                except PacingDeferred:
                    return n                      # out of request budget: carry on next cycle
                except Exception:
                    self._failed[market].add(sym)
                n += 1
        return n

    def reading(self, market: str) -> BreadthReading | None:
        if market not in self.baskets:
            return None
        pct, counted, as_of = breadth_of(self._closes[market])
        basket = len(self.baskets[market])
        score = None
        if basket and counted / basket >= self.min_coverage and all(pct[w] is not None for w in _WINDOWS):
            score = sum(_WEIGHTS[w] * pct[w] for w in _WINDOWS)
        return BreadthReading(market=market, pct20=pct[20], pct50=pct[50], pct200=pct[200], score=score,
                              counted=counted, basket=basket, as_of=as_of)
