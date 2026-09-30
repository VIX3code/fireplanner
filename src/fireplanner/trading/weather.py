"""Market weather: the regime gate from the analysis side, per market, applied to new buys.

Most swing stop-outs happen when the whole market turns, not the stock. So each
new buy's fixed loss is scaled by its own market's weather:

=============  ==========  =================================
Label          Default     Meaning for a new buy
=============  ==========  =================================
Risk-On        × 1.0       full fixed loss
Constructive   × 1.0
Neutral        × 1.0
Defensive      × 0.5       half the fixed loss (half the size)
Risk-Off       × 0         blocked
=============  ==========  =================================

The US reading is the full model (SPY trend, VIX, equal-weight breadth, VIX
term structure). The other markets read their index proxy's trend and
drawdown only; the model renormalises its weights when inputs are missing.
Open positions are never touched by the weather: their stops do that job.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date
from typing import Callable

import pandas as pd

from ..indicators.regime import compute_regime, latest_regime
from .markets import parse_symbol
from .rules import TradingRules

__all__ = ["WeatherReading", "read_weather", "MarketWeather"]


@dataclass
class WeatherReading:
    market: str
    proxy: str
    label: str | None
    score: float | None
    multiplier: float
    as_of: str | None
    components: dict
    note: str = ""

    def as_dict(self) -> dict:
        return asdict(self)


def read_weather(market: str, proxy: str, closes: dict[str, pd.Series], rules: TradingRules) -> WeatherReading:
    """One market's reading from its closes (``index`` plus, for the US, vix/breadth/vix3m)."""
    index = closes.get("index")
    if index is None or len(index.dropna()) < 210:
        return WeatherReading(market, proxy, None, None, 1.0, None, {},
                              note="Not enough history for a reading; sizing is not adjusted.")
    reg = compute_regime(index, vix=closes.get("vix"), equal_weight=closes.get("breadth"), vix3m=closes.get("vix3m"))
    r = latest_regime(reg)
    return WeatherReading(market=market, proxy=proxy, label=r.label, score=float(r.score),
                          multiplier=rules.weather_multiplier(r.label), as_of=str(r.date.date()),
                          components={k: (None if v is None else round(float(v), 3)) for k, v in r.components.items()})


class MarketWeather:
    """Reads each market once a day through ``bars(symbol_text) -> DataFrame``."""

    US_EXTRAS = {"vix": "VIX", "breadth": "RSP", "vix3m": "VIX3M"}

    def __init__(self, rules: TradingRules, proxies: dict[str, str],
                 bars: Callable[[str], pd.DataFrame]):
        self.rules, self.proxies, self.bars = rules, dict(proxies), bars
        self._day: date | None = None
        self.readings: dict[str, WeatherReading] = {}

    def refresh(self, today: date, markets: set[str] | None = None, force: bool = False) -> dict[str, WeatherReading]:
        if self._day == today and not force and self.readings:
            return self.readings
        out = {}
        for market, proxy in self.proxies.items():
            if markets is not None and market not in markets:
                continue
            closes: dict[str, pd.Series] = {}
            note = ""
            try:
                closes["index"] = self.bars(proxy)["close"]
                if market == "US":
                    for k, sym in self.US_EXTRAS.items():
                        try:
                            closes[k] = self.bars(sym)["close"]
                        except Exception:
                            note = "Partial reading: some of VIX / RSP / VIX3M unavailable."
                reading = read_weather(market, proxy, closes, self.rules)
                if note and not reading.note:
                    reading.note = note
            except Exception as exc:
                reading = WeatherReading(market, proxy, None, None, 1.0, None, {},
                                         note=f"No reading ({exc}); sizing is not adjusted.")
            out[market] = reading
        self.readings, self._day = out, today
        return out

    def rescale(self, rules: TradingRules) -> None:
        """Re-apply multipliers after the rules change, without re-reading prices."""
        self.rules = rules
        for r in self.readings.values():
            r.multiplier = rules.weather_multiplier(r.label)

    def for_market(self, market: str) -> WeatherReading | None:
        return self.readings.get(market)


def proxy_key(text: str) -> str:
    sym, mkt = parse_symbol(text)
    return f"{sym}:{mkt}"
