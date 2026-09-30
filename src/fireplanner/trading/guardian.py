"""The guardian: no position without a stop, and stops only ever move up.

`plan_protection` is a pure function. Given the journal's open trades and what
the broker reports (positions, working orders, last prices), it returns the
order changes that bring every position in line with the rules:

1. **Every position has one GTC stop for its full size.** A position without
   one gets it immediately. Duplicates are cancelled.
2. **Until the first exit, half the position has a GTC target**, in the same
   One-Cancels-All group as the stop. Whichever fills first cancels the other;
   the next cycle re-places a stop for whatever is left.
3. **The stop ratchets.** At +5% from entry it moves to the entry price, then
   trails 3 ATRs under the highest price since entry. It never moves down, and
   a stop you tightened by hand in TWS is adopted rather than undone.
4. **Leftovers are cleaned up.** A stop or target tagged by this system whose
   position is gone is cancelled, so it can never sell a later position.

Two refusals keep it from doing harm:

* It never places or moves a sell stop to or above the last price. That order
  would fire at once and sell the position; a stock already below its stop gets
  a protective stop under the market instead, and a critical alert.
* Orders it did not place are never modified. A manual stop covering the whole
  position is respected and reported, not duplicated.

The service runs this every cycle and on every fill, so a missed event, a
restart, or an order cancelled by hand is corrected within one cycle.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .broker import BrokerOrder, BrokerPosition, OrderSpec, order_ref
from .journal import Trade
from .markets import Instrument
from .rules import TradingRules

__all__ = ["Action", "ratchet", "plan_protection"]


@dataclass
class Action:
    kind: str                     # place | modify | cancel | note
    key: str
    trade_id: int | None
    reason: str
    level: str = "info"           # info | warn | crit
    spec: OrderSpec | None = None
    order: BrokerOrder | None = None
    changes: dict = field(default_factory=dict)
    #: Journal fields to write once the broker call succeeds.
    trade_update: dict = field(default_factory=dict)


def ratchet(trade: Trade, inst: Instrument, rules: TradingRules) -> tuple[float, str]:
    """The stop the rules call for now, and its state. Never below the recorded stop."""
    stop, state = trade.stop, trade.stop_state
    if trade.high_water >= trade.entry * (1 + rules.breakeven_at):
        breakeven = inst.round(trade.entry, "up")
        if breakeven > stop:
            stop, state = breakeven, "breakeven"
        elif state == "initial":
            state = "breakeven"
        if trade.atr:
            trail = inst.round(trade.high_water * (1 - rules.atr_trail_mult * trade.atr), "down")
            if trail > stop:
                stop, state = trail, "trailing"
    return stop, state


def _fmt(inst: Instrument, px: float) -> str:
    return inst.fmt(px)


def plan_protection(
    trades: list[Trade],
    positions: dict[int, BrokerPosition],
    orders: list[BrokerOrder],
    quotes: dict[int, float],
    instruments: dict[int, Instrument],
    rules: TradingRules,
) -> list[Action]:
    actions: list[Action] = []
    open_by_con = {t.con_id: t for t in trades if t.status == "open"}

    for trade in open_by_con.values():
        pos = positions.get(trade.con_id)
        inst = instruments.get(trade.con_id)
        if pos is None or pos.qty <= 0 or inst is None:
            continue
        actions += _protect(trade, pos, inst, orders, quotes.get(trade.con_id), rules)

    # Orphans: our stop/target orders whose position or trade is gone.
    for o in orders:
        if o.role not in {"stop", "target"}:
            continue
        t = open_by_con.get(o.con_id)
        pos = positions.get(o.con_id)
        if t is None or pos is None or pos.qty <= 0 or o.trade_id != t.id:
            actions.append(Action("cancel", key=t.key if t else str(o.con_id), trade_id=o.trade_id, order=o,
                                  reason=f"Cancelled a leftover {o.role} order: its position is closed."))
    return actions


def _protect(trade: Trade, pos: BrokerPosition, inst: Instrument, orders: list[BrokerOrder],
             last: float | None, rules: TradingRules) -> list[Action]:
    out: list[Action] = []
    qty = int(pos.qty)
    half_tick = inst.tick(trade.entry) / 2
    mine = [o for o in orders if o.con_id == trade.con_id and o.trade_id == trade.id]
    stops = [o for o in mine if o.role == "stop"]
    targets = [o for o in mine if o.role == "target"]
    manual = [o for o in orders if o.con_id == trade.con_id and not o.managed and o.is_protective_stop]

    # Until the first exit there is a target; after it the remainder only trails.
    want_target = not trade.has_exits and trade.target_qty > 0
    tq = min(trade.target_qty, qty)
    new_stop, state = ratchet(trade, inst, rules)

    def stop_spec(px: float, group: str) -> OrderSpec:
        return OrderSpec(con_id=trade.con_id, action="SELL", order_type="STP", qty=qty, stop_price=px,
                         tif="GTC", oca_group=group, order_ref=order_ref("stop", trade.id),
                         outside_rth=rules.outside_rth)

    def target_spec(group: str) -> OrderSpec:
        return OrderSpec(con_id=trade.con_id, action="SELL", order_type="LMT", qty=tq, limit_price=trade.target,
                         tif="GTC", oca_group=group, order_ref=order_ref("target", trade.id))

    # ---- no stop of ours ------------------------------------------------
    if not stops:
        if manual and sum(o.remaining for o in manual) >= qty:
            out.append(Action("note", trade.key, trade.id, level="info",
                              reason=f"{trade.symbol} has a stop you placed yourself; the guardian leaves it alone."))
            return out

        px, state_out, level = new_stop, state, "info"
        if trade.oca_rev == 0:
            reason = f"Placed GTC stop {_fmt(inst, px)} on {qty} {trade.symbol}"
        elif trade.has_exits:
            # Expected: the target's fill cancelled its One-Cancels-All stop.
            reason = f"{trade.symbol}: placed a GTC stop {_fmt(inst, px)} on the remaining {qty} shares"
        else:
            reason = f"{trade.symbol} had no stop. Placed GTC stop {_fmt(inst, px)} on {qty} shares"
            level = "crit"
        if last is not None and px >= last:
            fallback = inst.round(last * (1 - rules.stop_pct(trade.atr)), "down")
            reason = (f"{trade.symbol} is already below its stop (stop {_fmt(inst, px)}, last {_fmt(inst, last)}). "
                      f"Placed a protective stop at {_fmt(inst, fallback)} instead. Decide whether to sell now.")
            px, level = fallback, "crit"
        if last is not None and px < last * 0.3:
            out.append(Action("note", trade.key, trade.id, level="crit",
                              reason=f"Did not place a stop on {trade.symbol}: {_fmt(inst, px)} is implausibly far "
                                     f"below the last price {_fmt(inst, last)}. Check the price units."))
            return out

        rev = trade.oca_rev + 1
        group = f"fp-{trade.id}-{rev}"
        for t in targets:
            out.append(Action("cancel", trade.key, trade.id, order=t, reason="Replacing the target with a fresh stop/target pair."))
        out.append(Action("place", trade.key, trade.id, spec=stop_spec(px, group), level=level, reason=reason,
                          trade_update={"stop": px, "stop_state": state_out, "oca_rev": rev}))
        if want_target:
            out.append(Action("place", trade.key, trade.id, spec=target_spec(group),
                              reason=f"Placed GTC target {_fmt(inst, trade.target)} on {tq} {trade.symbol}"))
        return out

    # ---- our stop exists ---------------------------------------------------
    primary, extras = stops[0], stops[1:]
    for o in extras:
        out.append(Action("cancel", trade.key, trade.id, order=o, level="warn",
                          reason=f"Cancelled a duplicate stop on {trade.symbol}."))

    changes: dict = {}
    update: dict = {}
    if int(round(primary.remaining)) != qty:
        changes["qty"] = qty
    current = primary.stop_price if primary.stop_price is not None else trade.stop
    if current > trade.stop + half_tick:
        # Tightened by hand in TWS: adopt it, never undo it.
        update["stop"] = current
    if new_stop > current + half_tick:
        # Once trailing, move in steps of a quarter ATR, not every tick: fewer
        # order amendments, and no alert for every new high.
        step = 0.25 * trade.atr * trade.entry if trade.atr and trade.stop_state == "trailing" else 0.0
        if last is not None and new_stop < last and new_stop - current >= step:
            changes["stop_price"] = new_stop
            update.update(stop=new_stop, stop_state=state)
    elif state != trade.stop_state:
        update["stop_state"] = state

    if changes:
        what = []
        if "stop_price" in changes:
            label = {"breakeven": "to the entry price", "trailing": "up (trailing)"}.get(state, "up")
            what.append(f"moved stop {label}: {_fmt(inst, current)} → {_fmt(inst, changes['stop_price'])}")
        if "qty" in changes:
            what.append(f"resized stop to {qty} shares")
        out.append(Action("modify", trade.key, trade.id, order=primary, changes=changes, trade_update=update,
                          reason=f"{trade.symbol}: " + "; ".join(what)))
    elif update:
        out.append(Action("note", trade.key, trade.id, trade_update=update, level="info",
                          reason=f"{trade.symbol}: stop state is now {update.get('stop_state', trade.stop_state)}"
                          if "stop" not in update else
                          f"{trade.symbol}: adopted your tighter stop at {_fmt(inst, update['stop'])}"))

    # targets
    if want_target:
        if not targets:
            group = primary.oca_group or f"fp-{trade.id}-{trade.oca_rev}"
            out.append(Action("place", trade.key, trade.id, spec=target_spec(group),
                              reason=f"Placed GTC target {_fmt(inst, trade.target)} on {tq} {trade.symbol}"))
        else:
            t0 = targets[0]
            for o in targets[1:]:
                out.append(Action("cancel", trade.key, trade.id, order=o, level="warn",
                                  reason=f"Cancelled a duplicate target on {trade.symbol}."))
            tchanges = {}
            if int(round(t0.remaining)) != tq:
                tchanges["qty"] = tq
            if t0.limit_price is not None and abs(t0.limit_price - trade.target) > half_tick:
                tchanges["limit_price"] = trade.target
            if tchanges:
                out.append(Action("modify", trade.key, trade.id, order=t0, changes=tchanges,
                                  reason=f"{trade.symbol}: target updated to {_fmt(inst, trade.target)} on {tq} shares"))
    else:
        for o in targets:
            out.append(Action("cancel", trade.key, trade.id, order=o,
                              reason=f"{trade.symbol}: half already sold, so the rest trails without a target."))
    return out
