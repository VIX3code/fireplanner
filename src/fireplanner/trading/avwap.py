"""Anchored VWAP: the average price everyone paid since a day that mattered.

VWAP anchored to an event is the volume-weighted average price of every share
traded since that session: in effect, the cost basis of everyone who bought
because of it. Buyers defend it, so a pullback to it is a common swing entry,
and a price below it means those buyers are losing money and tend to sell into
rallies.

Three anchors, from daily bars:

* **Earnings.** The report date, if known and in the past; otherwise the most
  recent **gap day**, a session opening at least 4% away from the prior close
  on at least twice the 50-day average volume, which is usually the earnings
  reaction.
* **Breakout.** The first close above the prior 50 sessions' high after at
  least 10 sessions without one: the start of the latest leg up.
* **Swing low.** The lowest low of the last 60 sessions.

The trade check lists each level and its distance from the price. The nearest
one below the price, within reach, is offered as a **pullback limit**, so a
buy can wait for the stock to come back to support instead of chasing it.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date

import pandas as pd

__all__ = ["Anchor", "avwap_since", "find_gap_day", "find_breakout_day", "find_swing_low",
           "anchored_vwaps", "pullback_level"]


@dataclass
class Anchor:
    kind: str            # earnings | gap | breakout | swing_low
    label: str           # what the dashboard calls it
    date: str
    avwap: float
    #: last / avwap - 1: positive when the price is above the level.
    distance: float
    #: Up or down gap on the anchor day, when it was one.
    gap: float | None = None

    def as_dict(self) -> dict:
        return asdict(self)


def avwap_since(bars: pd.DataFrame, start) -> float | None:
    """Volume-weighted typical price from ``start`` (a bar date) to the last bar, inclusive."""
    seg = bars.loc[bars.index >= pd.Timestamp(start)]
    if seg.empty:
        return None
    tp = (seg["high"] + seg["low"] + seg["close"]) / 3.0
    vol = seg["volume"].clip(lower=0)
    if float(vol.sum()) <= 0:
        return float(tp.mean())
    return float((tp * vol).sum() / vol.sum())


def find_gap_day(bars: pd.DataFrame, lookback: int = 90, min_gap: float = 0.04, vol_mult: float = 2.0):
    """The most recent session that gapped ``min_gap`` on ``vol_mult`` × average volume."""
    if len(bars) < 60:
        return None, None
    prev_close = bars["close"].shift(1)
    gap = bars["open"] / prev_close - 1
    avg_vol = bars["volume"].rolling(50, min_periods=20).mean().shift(1)
    hit = (gap.abs() >= min_gap) & (bars["volume"] >= vol_mult * avg_vol)
    recent = hit.iloc[-lookback:]
    days = recent[recent].index
    if len(days) == 0:
        return None, None
    day = days[-1]
    return day, float(gap.loc[day])


def find_breakout_day(bars: pd.DataFrame, lookback: int = 60, window: int = 50, quiet: int = 10):
    """The first day of the latest breakout leg: a close above the prior ``window`` sessions' high
    with no such close in the ``quiet`` sessions before it.

    In a steady uptrend every day is a new high, so "the latest breakout" would
    always be yesterday; anchoring to the start of the leg keeps the level meaningful.
    """
    if len(bars) < window + quiet:
        return None
    prior_high = bars["high"].rolling(window).max().shift(1)
    brk = (bars["close"] > prior_high).to_numpy()
    start = max(quiet, len(bars) - lookback)
    for i in range(len(bars) - 1, start - 1, -1):
        if brk[i] and not brk[i - quiet:i].any():
            return bars.index[i]
    return None


def find_swing_low(bars: pd.DataFrame, lookback: int = 60):
    """The session with the lowest low of the last ``lookback``, if it isn't one of the last two."""
    if len(bars) < 10:
        return None
    recent = bars.iloc[-lookback:]
    day = recent["low"].idxmin()
    if day in bars.index[-2:]:
        return None
    return day


def _fmt_day(ts) -> str:
    return pd.Timestamp(ts).strftime("%d %b").lstrip("0")


def anchored_vwaps(bars: pd.DataFrame, last: float | None = None, earnings_day: date | None = None) -> list[Anchor]:
    """Every anchor found in ``bars`` (daily OHLCV, oldest first), nearest-first by date."""
    if bars is None or bars.empty:
        return []
    bars = bars.sort_index()
    last = float(bars["close"].iloc[-1]) if last is None else last
    found: list[tuple[str, str, object, float | None]] = []

    event = None
    if earnings_day is not None and bars.index[0].date() <= earnings_day <= bars.index[-1].date():
        on_or_after = bars.index[bars.index >= pd.Timestamp(earnings_day)]
        if len(on_or_after):
            # A report after the close moves the next session; take the bigger gap of the two.
            cands = on_or_after[:2]
            gaps = (bars["open"] / bars["close"].shift(1) - 1).reindex(cands).abs()
            event = gaps.idxmax() if gaps.notna().any() else cands[0]
            found.append(("earnings", f"Earnings {_fmt_day(event)}", event, None))
    if event is None:
        day, g = find_gap_day(bars)
        if day is not None:
            word = "Gap up" if g > 0 else "Gap down"
            found.append(("gap", f"{word} {_fmt_day(day)}", day, g))
    b = find_breakout_day(bars)
    if b is not None:
        found.append(("breakout", f"Breakout {_fmt_day(b)}", b, None))
    s = find_swing_low(bars)
    if s is not None:
        found.append(("swing_low", f"Swing low {_fmt_day(s)}", s, None))

    out, seen = [], set()
    for kind, label, day, g in found:
        if day in seen:
            continue
        seen.add(day)
        v = avwap_since(bars, day)
        if v:
            out.append(Anchor(kind=kind, label=label, date=str(pd.Timestamp(day).date()), avwap=v,
                              distance=last / v - 1, gap=g))
    return sorted(out, key=lambda a: a.date, reverse=True)


def pullback_level(anchors: list[Anchor], last: float, atr: float | None, max_atrs: float = 3.0) -> Anchor | None:
    """The nearest anchored VWAP below the price, if a pullback to it is within ``max_atrs`` daily ranges."""
    reach = max_atrs * (atr or 0.03)
    below = [a for a in anchors if a.avwap < last and (last / a.avwap - 1) <= reach
             and not (a.kind == "gap" and (a.gap or 0) < 0)]
    return max(below, key=lambda a: a.avwap) if below else None
