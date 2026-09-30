"""How the open book splits across Steady, Core and Volatile.

Two lenses, because they disagree and the disagreement is the point:

* **Share of money** — market value per bucket. What a statement shows.
* **Share of daily swing** — value × average daily range per bucket. Roughly
  what a normal day can move each bucket. A Volatile slot moves about three
  times as much as a Steady one, so a book that looks balanced by money can
  still get half of its day-to-day movement from its Volatile names.

The caps block on position counts; the swing guide only warns.
"""

from __future__ import annotations

from dataclasses import dataclass

from .rules import TradingRules

__all__ = ["Holding", "BucketView", "bucket_mix", "swing_share_after"]


@dataclass(frozen=True)
class Holding:
    key: str
    bucket: str
    value_usd: float
    cost_usd: float
    pl_usd: float
    #: Average daily range as a fraction of price; None when unknown.
    atr: float | None
    #: What a stop-out would cost from here, in USD (0 once the stop is at entry or above).
    risk_usd: float = 0.0

    @property
    def swing_usd(self) -> float:
        return self.value_usd * (self.atr or 0.0)


@dataclass
class BucketView:
    id: str
    name: str
    target: float
    cap: int
    n: int
    value_usd: float
    cost_usd: float
    pl_usd: float
    swing_usd: float
    money_share: float
    swing_share: float
    #: New positions this bucket can still take, limited by its cap and the book.
    room: int

    def as_dict(self) -> dict:
        return dict(self.__dict__)


def bucket_mix(holdings: list[Holding], rules: TradingRules) -> list[BucketView]:
    total_value = sum(h.value_usd for h in holdings)
    total_swing = sum(h.swing_usd for h in holdings)
    free = max(0, rules.max_slots - len(holdings))
    out = []
    for b in rules.buckets:
        hs = [h for h in holdings if h.bucket == b.id]
        value = sum(h.value_usd for h in hs)
        swing = sum(h.swing_usd for h in hs)
        out.append(BucketView(
            id=b.id, name=b.name, target=b.target, cap=b.cap, n=len(hs),
            value_usd=value, cost_usd=sum(h.cost_usd for h in hs), pl_usd=sum(h.pl_usd for h in hs),
            swing_usd=swing,
            money_share=value / total_value if total_value else 0.0,
            swing_share=swing / total_swing if total_swing else 0.0,
            room=max(0, min(b.cap - len(hs), free)),
        ))
    return out


def swing_share_after(holdings: list[Holding], add: Holding | None, bucket_id: str = "volatile") -> tuple[float, float]:
    """``bucket_id``'s share of daily swing now, and after adding ``add``."""
    def share(hs):
        total = sum(h.swing_usd for h in hs)
        return sum(h.swing_usd for h in hs if h.bucket == bucket_id) / total if total else 0.0

    before = share(holdings)
    after = share(holdings + [add]) if add is not None else before
    return before, after
