"""Turn "buy this stock" into a whole-lot order with its stop and target.

**The loss is fixed; the size follows.** The stop is set first (the tighter of
5% or 2.5 × the daily range), then the share count is whatever makes a
stop-out cost ``risk_per_trade_usd``. A calm stock with a 3% stop gets a bigger
position than a volatile one with a 5% stop; both lose the same if stopped.

Share and lot counts round **up** by default, so the position is never smaller
than the rule asks. Rounding up a large lot (Tokyo, Hong Kong) can overshoot a
lot, so when it would add more than ``max_round_up_overshoot`` to the loss the
count rounds down instead. Every position is then capped at
``max_position_usd``.

Prices snap to the exchange's tick grid in the direction that keeps the
promise: the stop rounds **up** (toward the price), the target **down**.
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
    #: The fixed loss this trade was sized for (after any weather cut).
    risk_budget_usd: float
    #: What a stop-out actually costs after rounding and caps.
    max_loss_usd: float
    target_gain_usd: float
    atr: float | None
    #: What set the size: "fixed loss", "rounded down", or "max position".
    sized_by: str = "fixed loss"
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
    risk_usd: float | None = None,
) -> EntryPlan:
    """Size a new position (or an add). Never raises for a business reason: sets ``problem``."""
    bucket = rules.bucket(bucket_id)
    budget = rules.risk_per_trade_usd if risk_usd is None else risk_usd
    if limit is None:
        limit = inst.round(last * (1 + rules.entry_limit_buffer), "up")
    stop = initial_stop(limit, atr, rules, inst)
    target = target_price(limit, bucket.target, inst)
    lot = inst.lot_size
    risk_per_share = inst.value(1, limit - stop) * usd_per_unit
    per_share = inst.value(1, limit) * usd_per_unit
    qty, sized_by, problem = 0, "fixed loss", None

    if lot <= 0:
        problem = (f"The board lot for {inst.key} is unknown. Hong Kong lots differ per stock: "
                   f"add it to the watchlist with its lot size.")
    elif budget <= 0:
        problem = "Market weather is Risk-Off: new buys are paused."
    elif risk_per_share <= 0:
        problem = "The stop is not below the entry price."
    else:
        lots = budget / risk_per_share / lot
        down, up = math.floor(lots + 1e-9) * lot, math.ceil(lots - 1e-9) * lot
        qty = down
        if rules.round_up:
            if up * risk_per_share <= budget * (1 + rules.max_round_up_overshoot):
                qty = up
            elif down > 0:
                sized_by = "rounded down"
        cap = math.floor(rules.max_position_usd / per_share / lot + 1e-9) * lot
        if qty > cap:
            qty, sized_by = cap, "max position"
        if qty == 0:
            one_lot_risk = lot * risk_per_share
            if cap == 0:
                problem = (f"One lot ({lot} shares) costs about ${lot * per_share:,.0f}, more than the "
                           f"${rules.max_position_usd:,.0f} position cap.")
            else:
                problem = (f"One lot ({lot} shares) would lose about ${one_lot_risk:,.0f} at the stop, more than "
                           f"{rules.max_round_up_overshoot:.0%} over the ${budget:,.0f} fixed loss.")

    cost_local = inst.value(qty, limit)
    return EntryPlan(
        key=inst.key, symbol=inst.symbol, market=inst.market, currency=inst.currency,
        bucket=bucket.id, last=last, limit=limit, qty=qty, lot_size=lot,
        cost_local=cost_local, cost_usd=cost_local * usd_per_unit, usd_per_unit=usd_per_unit,
        stop=stop, stop_pct=1 - stop / limit, stop_basis=stop_basis(atr, rules),
        target=target, target_pct=target / limit - 1,
        target_qty=target_qty(qty, lot, rules) if qty else 0,
        risk_budget_usd=budget, max_loss_usd=qty * risk_per_share,
        target_gain_usd=inst.value(qty, target - limit) * usd_per_unit,
        atr=atr, sized_by=sized_by, problem=problem,
    )
