"""Market weather: the regime gate from the analysis side, plus breadth, per market, applied to new buys.

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

The weather score is the regime score blended with **breadth** (the share of
the market's big stocks above their 20/50/200-day averages; see `breadth`):
``(1 - w) × regime + w × breadth`` with ``w = rules.breadth_weight``. Until a
market's breadth basket has enough data, the regime score is used alone.

The US regime is the full model (SPY trend, VIX, equal-weight breadth, VIX
term structure). The other markets read their index proxy's trend and
drawdown; the model renormalises its weights when inputs are missing. Open
positions are never touched by the weather: their stops do that job.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from datetime import date
from typing import Callable

import pandas as pd

from ..indicators.regime import REGIME_BANDS, compute_regime, latest_regime
from .breadth import MarketBreadth
from .broker import PacingDeferred
from .markets import parse_symbol
from .rules import TradingRules

__all__ = ["WeatherReading", "read_weather", "band_label", "MarketWeather"]


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
    #: The regime model alone, before breadth.
    regime_score: float | None = None
    regime_label: str | None = None
    #: The breadth reading blended in, if any (see `breadth.BreadthReading`).
    breadth: dict | None = None

    def as_dict(self) -> dict:
        return asdict(self)


def band_label(score: float) -> str:
    for threshold, label, _cap in REGIME_BANDS:
        if score >= threshold:
            return label
    return REGIME_BANDS[-1][1]


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
    """Reads each market's regime once a day, and blends in breadth as it arrives."""

    US_EXTRAS = {"vix": "VIX", "breadth": "RSP", "vix3m": "VIX3M"}

    def __init__(self, rules: TradingRules, proxies: dict[str, str],
                 bars: Callable[[str], pd.DataFrame], breadth: MarketBreadth | None = None):
        self.rules, self.proxies, self.bars, self.breadth = rules, dict(proxies), bars, breadth
        self._day: date | None = None
        self._regime: dict[str, WeatherReading] = {}
        self.readings: dict[str, WeatherReading] = {}

    def refresh(self, today: date, markets: set[str] | None = None, force: bool = False) -> dict[str, WeatherReading]:
        if self._day != today or force or not self._regime:
            self._regime = {}
            deferred = False
            for market, proxy in self.proxies.items():
                if markets is not None and market not in markets:
                    continue
                try:
                    self._regime[market] = self._read(market, proxy)
                except PacingDeferred:
                    deferred = True
                    self._regime[market] = WeatherReading(market, proxy, None, None, 1.0, None, {},
                                                          note="Waiting for price history (IBKR pacing).")
            self._day = None if deferred else today
        if self.breadth is not None:
            self.breadth.step(today)
        self.readings = {m: self._combine(m, r) for m, r in self._regime.items()}
        return self.readings

    def _read(self, market: str, proxy: str) -> WeatherReading:
        closes: dict[str, pd.Series] = {}
        note = ""
        try:
            closes["index"] = self.bars(proxy)["close"]
            if market == "US":
                for k, sym in self.US_EXTRAS.items():
                    try:
                        closes[k] = self.bars(sym)["close"]
                    except PacingDeferred:
                        raise
                    except Exception:
                        note = "Partial reading: some of VIX / RSP / VIX3M unavailable."
            reading = read_weather(market, proxy, closes, self.rules)
            if note and not reading.note:
                reading.note = note
            return reading
        except PacingDeferred:
            raise
        except Exception as exc:
            return WeatherReading(market, proxy, None, None, 1.0, None, {},
                                  note=f"No reading ({exc}); sizing is not adjusted.")

    def _combine(self, market: str, base: WeatherReading) -> WeatherReading:
        b = self.breadth.reading(market) if self.breadth is not None else None
        out = replace(base, regime_score=base.score, regime_label=base.label,
                      breadth=b.as_dict() if b is not None else None)
        if base.label is None or b is None or b.score is None:
            return out
        w = self.rules.breadth_weight
        score = (1 - w) * base.score + w * b.score
        label = band_label(score)
        return replace(out, score=score, label=label, multiplier=self.rules.weather_multiplier(label))

    def rescale(self, rules: TradingRules) -> None:
        """Re-apply rules (multipliers, breadth weight) without re-reading prices."""
        self.rules = rules
        self.readings = {m: self._combine(m, r) for m, r in self._regime.items()}

    def for_market(self, market: str) -> WeatherReading | None:
        return self.readings.get(market)


def proxy_key(text: str) -> str:
    sym, mkt = parse_symbol(text)
    return f"{sym}:{mkt}"
