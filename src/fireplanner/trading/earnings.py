"""Earnings dates beside every stock, because a stop can't protect against the gap.

IBKR's API has no free earnings calendar (its Wall Street Horizon feed is a
paid subscription), so dates come from two places:

* **You.** Set a date on the dashboard or with ``fireplanner trade earnings``.
  A date you set is never overwritten while it is still in the future.
* **Financial Modeling Prep**, if ``FMP_API_KEY`` is set (free tier: 250 calls
  a day). Each stock is looked up at most once a day, a few per cycle, so a
  large watchlist fills in over the first hour. International symbols use
  FMP's suffixes (``.L``, ``.HK``, ``.SI``, ``.T``).

The dashboard flags a date inside ``rules.earnings_warn_days``; the trade check
warns on it (or blocks, with ``earnings_block: true``).
"""

from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta
from typing import Callable

from .journal import Journal, now_utc

__all__ = ["fmp_symbol", "parse_fmp", "EarningsCalendar"]

FMP_URL = "https://financialmodelingprep.com/stable/earnings"


def fmp_symbol(symbol: str, market: str) -> str:
    if market == "US":
        return symbol.replace(".", "-")
    if market == "SEHK":
        return f"{int(symbol):04d}.HK" if symbol.isdigit() else f"{symbol}.HK"
    return f"{symbol}.{ {'LSE': 'L', 'SGX': 'SI', 'TSEJ': 'T'}.get(market, market) }"


def parse_fmp(rows, today: date) -> date | None:
    """The first earnings date on or after ``today`` in an FMP response."""
    upcoming = []
    for row in rows if isinstance(rows, list) else []:
        raw = row.get("date") if isinstance(row, dict) else None
        try:
            d = date.fromisoformat(str(raw)[:10])
        except (TypeError, ValueError):
            continue
        if d >= today:
            upcoming.append(d)
    return min(upcoming) if upcoming else None


def _http_json(url: str, timeout: float = 10.0):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read().decode())


class EarningsCalendar:
    def __init__(self, journal: Journal, api_key: str | None = None,
                 fetch: Callable[[str], object] | None = None, per_cycle: int = 5):
        self.journal = journal
        self.api_key = api_key if api_key is not None else os.environ.get("FMP_API_KEY", "")
        self.fetch = fetch or _http_json
        self.per_cycle = per_cycle
        self.errors: list[str] = []

    @property
    def online(self) -> bool:
        return bool(self.api_key)

    def refresh(self, keys: list[str], today: date) -> int:
        """Look up a few stale stocks. Returns how many were fetched."""
        if not self.online:
            return 0
        known = self.journal.earnings()
        stale_after = now_utc() - timedelta(days=1)
        todo = []
        for key in keys:
            k = known.get(key)
            if k and k["source"] == "manual" and k["date"] and k["date"] >= today:
                continue
            if k and k["updated_at"] and k["updated_at"] > stale_after:
                continue
            todo.append(key)
        n = 0
        for key in todo[: self.per_cycle]:
            symbol, market = key.split(":")
            url = f"{FMP_URL}?{urllib.parse.urlencode({'symbol': fmp_symbol(symbol, market), 'apikey': self.api_key})}"
            try:
                self.journal.set_earnings(key, parse_fmp(self.fetch(url), today), source="fmp")
                n += 1
            except Exception as exc:
                self.errors = (self.errors + [f"{key}: {exc}"])[-5:]
        return n

    def upcoming(self, today: date) -> dict[str, dict]:
        """``key -> {"date", "days", "source"}`` for every future date known."""
        out = {}
        for key, k in self.journal.earnings().items():
            if k["date"] and k["date"] >= today:
                out[key] = {"date": k["date"].isoformat(), "days": (k["date"] - today).days, "source": k["source"]}
        return out


def parse_date(text: str | None) -> date | None:
    if not text:
        return None
    return datetime.strptime(text.strip()[:10], "%Y-%m-%d").date()
