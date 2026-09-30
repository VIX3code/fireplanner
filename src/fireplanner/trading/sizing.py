"""Turn "buy this stock" into a whole-lot order with its stop and target.

One slot is ``rules.slot_usd`` in the base currency. It is converted to the
listing currency, divided by the limit price, and rounded **down** to whole
board lots, so a position never exceeds its slot. Prices are snapped to the
exchange's tick grid in the direction that keeps the promise:

* the stop rounds **up**, toward the price, so a stop-out never loses more
  than the stop percentage;
* the target rounds **down**, toward the price, so it stays reachable.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass

from .markets import Instrument
from .rules import TradingRules

__all__ = ["EntryPlan", "plan_entry", "initial_stop", "target_price", "target_qty", "stop_basis"]


@dataclass
class EntryPlan:
    key: str
    symbol: str
    market: str
    currency: str
    bucket: str
    last: float
    limit: float
    qty: int
    lot_size: int
    cost_local: float
    cost_usd: float
    usd_per_unit: float
    stop: float
    stop_pct: float
    stop_basis: str
    target: float
    target_pct: float
    target_qty: int
    max_loss_usd: float
    target_gain_usd: float
    atr: float | None
    #: Why the order cannot be placed, or None.
    problem: str | None = None

    def as_dict(self) -> dict:
        return asdict(self)


def stop_basis(atr: float | None, rules: TradingRules) -> str:
    if atr is None or atr != atr or atr <= 0:
        return f"{rules.max_stop_pct:.0%} cap (no ATR yet)"
    if rules.atr_stop_mult * atr < rules.max_stop_pct:
        return f"{rules.atr_stop_mult:g} × ATR ({atr:.1%} a day)"
    return f"{rules.max_stop_pct:.0%} cap"


def initial_stop(entry: float, atr: float | None, rules: TradingRules, inst: Instrument) -> float:
    """The first stop for a position bought at ``entry``."""
    raw = entry * (1 - rules.stop_pct(atr))
    stop = inst.round(raw, "up")
    if stop >= entry:  # a coarse tick swallowed the whole distance
        stop = inst.round(entry - inst.tick(entry), "down")
    return stop


def target_price(entry: float, bucket_target: float, inst: Instrument) -> float:
    return inst.round(entry * (1 + bucket_target), "down")


def target_qty(qty: int, lot_size: int, rules: TradingRules) -> int:
    """How much to sell at the target: half in whole lots, or all of a single lot."""
    if not rules.take_half_at_target:
        return qty
    lot = max(1, lot_size)
    half = math.floor(qty / 2 / lot) * lot
    return half if half > 0 else qty


def plan_entry(
    inst: Instrument,
    last: float,
    atr: float | None,
    bucket_id: str,
    rules: TradingRules,
    usd_per_unit: float,
    limit: float | None = None,
) -> EntryPlan:
    """Size a new position. Never raises for a business reason: sets ``problem``."""
    bucket = rules.bucket(bucket_id)
    if limit is None:
        limit = inst.round(last * (1 + rules.entry_limit_buffer), "up")
    lot = inst.lot_size
    problem = None

    if lot <= 0:
        qty = 0
        problem = (f"The board lot for {inst.key} is unknown. Hong Kong lots differ per stock: "
                   f"add it to the watchlist with its lot size.")
    else:
        slot_local = rules.slot_usd / usd_per_unit
        per_share = inst.value(1, limit)
        qty = math.floor(slot_local / per_share / lot) * lot if per_share > 0 else 0
        if qty == 0:
            one_lot = inst.value(lot, limit) * usd_per_unit
            problem = (f"One lot ({lot} shares) costs about ${one_lot:,.0f}, "
                       f"more than the ${rules.slot_usd:,.0f} slot.")

    stop = initial_stop(limit, atr, rules, inst)
    target = target_price(limit, bucket.target, inst)
    cost_local = inst.value(qty, limit)
    return EntryPlan(
        key=inst.key, symbol=inst.symbol, market=inst.market, currency=inst.currency,
        bucket=bucket.id, last=last, limit=limit, qty=qty, lot_size=lot,
        cost_local=cost_local, cost_usd=cost_local * usd_per_unit, usd_per_unit=usd_per_unit,
        stop=stop, stop_pct=1 - stop / limit, stop_basis=stop_basis(atr, rules),
        target=target, target_pct=target / limit - 1,
        target_qty=target_qty(qty, lot, rules) if qty else 0,
        max_loss_usd=inst.value(qty, limit - stop) * usd_per_unit,
        target_gain_usd=inst.value(qty, target - limit) * usd_per_unit,
        atr=atr, problem=problem,
    )
