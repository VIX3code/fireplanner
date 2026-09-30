"""Keep the journal in step with what the broker says you hold.

Reconciliation, not event handling: each cycle compares the journal's open
trades with the broker's positions and records whatever changed. Fill events
only make the next cycle happen sooner. That way a restart, a dropped
connection or a buy placed in the IBKR app on your phone all end in the same
place: every position has a trade, every trade has its exits.

* A position with no open trade becomes a new trade. Its stop and target are
  computed from the fill price, the stock type, and the average daily range.
* A position that shrank records an exit (target, stop, trail or manual).
* A trade whose position is gone is closed, and the two-strike rule is applied.
* A position that grew was added to outside the dashboard: noted and re-based.

Buys made outside the dashboard are checked against the same rules after the
fact. They cannot be blocked, but they are flagged.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

from .broker import BrokerFill, BrokerPosition, parse_ref
from .buckets import Holding
from .journal import Journal, Trade, now_utc
from .markets import Instrument
from .rules import TradingRules
from .sizing import initial_stop, target_price, target_qty
from .strikes import strike_for

__all__ = ["SyncEvent", "sync_trades"]


@dataclass
class SyncEvent:
    level: str
    kind: str
    key: str
    message: str


def _avg_fill(fills: list[BrokerFill], con_id: int, side: str, after: datetime | None) -> tuple[float, float] | None:
    fs = [f for f in fills if f.con_id == con_id and f.side == side and (after is None or f.time > after)]
    qty = sum(f.qty for f in fs)
    if qty <= 0:
        return None
    return sum(f.qty * f.price for f in fs) / qty, qty


_ROLE_KIND = {"exit": "exit", "timestop": "time-stop", "target": "target"}


def _exit_kind(trade: Trade, price: float, inst: Instrument, roles: set | None = None) -> str:
    """Name an exit by the order that filled it, or by its price when fills are gone."""
    for role in ("timestop", "exit", "target"):
        if roles and role in roles:
            return _ROLE_KIND[role]
    if roles and "stop" in roles:
        return {"breakeven": "breakeven", "trailing": "trail"}.get(trade.stop_state, "stop")
    tick = inst.tick(price)
    if price >= trade.target - tick:
        return "target"
    if price <= trade.stop + 2 * tick:
        return {"breakeven": "breakeven", "trailing": "trail"}.get(trade.stop_state, "stop")
    return "manual"


def _money(x: float) -> str:
    sign = "−" if x < 0 else "+"
    return f"{sign}${abs(x):,.0f}"


def sync_trades(
    journal: Journal,
    positions: list[BrokerPosition],
    fills: list[BrokerFill],
    quotes: dict[int, float],
    instruments: dict[int, Instrument],
    atr: dict[int, float | None],
    fx: dict[str, float],
    rules: TradingRules,
    today: date,
    now: datetime | None = None,
) -> list[SyncEvent]:
    now = now or now_utc()
    events: list[SyncEvent] = []
    by_con = {p.con_id: p for p in positions if p.qty > 0}
    confirmed = journal.confirmed_buckets()
    sectors = journal.sectors()

    for p in positions:
        if p.qty < 0:
            events.append(SyncEvent("crit", "short", f"{p.symbol}:{p.market}",
                                    f"{p.symbol} shows a short position of {p.qty:g}. A cash account should not "
                                    f"allow this: check the account in TWS."))

    # trades whose position shrank or vanished
    for t in journal.open_trades():
        inst = instruments.get(t.con_id)
        pos = by_con.get(t.con_id)
        held = int(pos.qty) if pos else 0
        if held >= t.qty or inst is None:
            continue
        sold = t.qty - held
        last_exit = journal.exits(t.id)
        after = datetime.fromisoformat(last_exit[-1].at) if last_exit else t.opened
        got = _avg_fill(fills, t.con_id, "SELL", after)
        price = got[0] if got else quotes.get(t.con_id, t.stop)
        roles = {parse_ref(f.order_ref)[0] for f in fills
                 if f.con_id == t.con_id and f.side == "SELL" and (after is None or f.time > after)}
        kind = _exit_kind(t, price, inst, roles)
        t = journal.add_exit(t.id, sold, price, kind, at=now)
        usd = fx.get(t.currency, 1.0)
        pl = inst.value(sold, price - t.entry) * usd
        if held > 0:
            events.append(SyncEvent("info", f"exit-{kind}", t.key,
                                    f"{t.symbol}: sold {sold} at {inst.fmt(price)} ({kind}), {_money(pl)}. "
                                    f"{held} left, trailing."))
            continue
        t = journal.close_trade(t.id, magnifier=inst.price_magnifier, usd_per_unit=usd,
                                strike_loss_pct=rules.strike_loss_pct, at=now)
        st = strike_for(journal, t.key, today, rules)
        result = f"{t.realized_pct:+.1%}, {_money(t.realized_usd)}"
        if t.strike:
            if st.locked:
                until = f"until {st.locked_until:%d %b}" if st.locked_until else "until you unlock it"
                tail = f"Strike {rules.max_strikes} of {rules.max_strikes}: locked {until}."
            else:
                tail = f"Strike {st.strikes} of {rules.max_strikes}: {st.tries_left} try left."
            events.append(SyncEvent("warn", "stopped-out", t.key, f"{t.symbol} closed ({kind}) at {inst.fmt(price)}, {result}. {tail}"))
        else:
            events.append(SyncEvent("info", "closed", t.key, f"{t.symbol} closed ({kind}) at {inst.fmt(price)}, {result}."))

    # positions with no trade, or that grew
    for con_id, p in by_con.items():
        inst = instruments.get(con_id)
        if inst is None:
            continue
        t = journal.open_trade_for(con_id)
        a = atr.get(con_id)
        if t is None:
            # Only buys since this stock's previous trade closed belong to this one.
            prior = [c for c in journal.closed_trades(inst.key) if c.closed_at]
            since = datetime.fromisoformat(prior[-1].closed_at) if prior else None
            got = _avg_fill(fills, con_id, "BUY", since)
            entry = got[0] if got else p.avg_cost
            bucket = confirmed.get(inst.key) or rules.suggest_bucket(a)
            qty = int(p.qty)
            stop = initial_stop(entry, a, rules, inst)
            target = target_price(entry, rules.bucket(bucket).target, inst)
            usd = fx.get(inst.currency, 1.0)
            pending = journal.pop_pending(inst.key) or {}
            sector = sectors.get(inst.key) or inst.sector or ""
            t = journal.open_trade(key=inst.key, symbol=inst.symbol, market=inst.market, con_id=con_id,
                                   currency=inst.currency, bucket=bucket, entry=entry, qty=qty, atr=a,
                                   stop=stop, target=target, target_qty=target_qty(qty, inst.lot_size, rules),
                                   opened_at=now, setup=pending.get("setup", ""), note=pending.get("note", ""),
                                   initial_risk_usd=inst.value(qty, entry - stop) * usd, sector=sector)
            events.append(SyncEvent("info", "opened", inst.key,
                                    f"New position: {qty} {inst.symbol} at {inst.fmt(entry)} "
                                    f"({rules.bucket(bucket).name}"
                                    f"{'' if inst.key in confirmed else ', suggested'}). "
                                    f"Stop {inst.fmt(stop)} ({stop / entry - 1:.1%}), target {inst.fmt(target)} "
                                    f"(+{rules.bucket(bucket).target:.0%}). "
                                    f"Cost ${inst.value(qty, entry) * usd:,.0f}."))
            events += _after_the_fact(journal, t, rules, today)
        elif int(p.qty) > t.qty:
            added = int(p.qty) - t.qty
            new_qty = int(p.qty)
            usd = fx.get(inst.currency, 1.0)
            planned = [f for f in fills if f.con_id == con_id and f.side == "BUY" and f.time > t.opened
                       and parse_ref(f.order_ref)[0] == "add"]
            tq = target_qty(new_qty, inst.lot_size, rules) if not t.has_exits else t.target_qty
            if planned:
                add_px = sum(f.qty * f.price for f in planned) / sum(f.qty for f in planned)
                entry = (t.qty * t.entry + added * add_px) / new_qty
                add_stop = initial_stop(add_px, a, rules, inst)
                stop = max(t.stop, add_stop)
                journal.update_trade(
                    t.id, qty=new_qty, initial_qty=t.initial_qty + added, entry=entry, adds=t.adds + 1,
                    stop=stop, stop_state=t.stop_state if stop >= entry else "initial", target_qty=tq,
                    initial_risk_usd=(t.initial_risk_usd or 0.0) + inst.value(added, add_px - add_stop) * usd)
                events.append(SyncEvent("info", "added", inst.key,
                                        f"Added {added} {inst.symbol} at {inst.fmt(add_px)} (now {new_qty}). "
                                        f"Stop raised to {inst.fmt(stop)}, so the whole position still risks about "
                                        f"one fixed loss."))
            else:
                entry = p.avg_cost if p.avg_cost > 0 else t.entry
                journal.update_trade(t.id, qty=new_qty, initial_qty=t.initial_qty + added, entry=entry,
                                     adds=t.adds + 1, target_qty=tq)
                events.append(SyncEvent("warn", "added", inst.key,
                                        f"{added} {inst.symbol} added outside the dashboard (now {new_qty}). The stop "
                                        f"stays at {inst.fmt(t.stop)}, so this position now risks more than one fixed "
                                        f"loss. Adds from the dashboard move the stop up for you."))

    # high- and low-water marks (best and worst price since entry)
    for t in journal.open_trades():
        last = quotes.get(t.con_id)
        if last is None:
            continue
        changes = {}
        if last > t.high_water:
            changes["high_water"] = last
        if t.low_water is None or last < t.low_water:
            changes["low_water"] = last
        if changes:
            journal.update_trade(t.id, **changes)
    return events


def _after_the_fact(journal: Journal, trade: Trade, rules: TradingRules, today: date) -> list[SyncEvent]:
    """Rules a buy placed outside the dashboard may have broken."""
    out = []
    others = [t for t in journal.open_trades() if t.id != trade.id]
    b = rules.bucket(trade.bucket)
    if len(others) + 1 > rules.max_slots:
        out.append(SyncEvent("warn", "rule", trade.key,
                             f"{trade.symbol} takes the book to {len(others) + 1} positions, over the "
                             f"{rules.max_slots}-slot limit."))
    n = sum(1 for t in others if t.bucket == b.id) + 1
    if n > b.cap:
        out.append(SyncEvent("warn", "rule", trade.key,
                             f"{trade.symbol} puts {n} positions in {b.name}, over its cap of {b.cap}."))
    if trade.sector:
        same = sum(1 for t in others if t.sector == trade.sector) + 1
        if same > rules.max_per_sector:
            out.append(SyncEvent("warn", "rule", trade.key,
                                 f"{trade.symbol} makes {same} positions in {trade.sector}, over the limit of "
                                 f"{rules.max_per_sector}."))
    st = strike_for(journal, trade.key, today, rules)
    if st.locked:
        out.append(SyncEvent("warn", "rule", trade.key,
                             f"{trade.symbol} was bought while locked by the two-strike rule."))
    return out


def holdings_from(journal: Journal, positions: dict[int, BrokerPosition], quotes: dict[int, float],
                  instruments: dict[int, Instrument], fx: dict[str, float]) -> list[Holding]:
    """Open trades as `Holding` rows for the bucket mix and the gate."""
    out = []
    for t in journal.open_trades():
        inst = instruments.get(t.con_id)
        pos = positions.get(t.con_id)
        if inst is None or pos is None or pos.qty <= 0:
            continue
        usd = fx.get(t.currency, 1.0)
        last = quotes.get(t.con_id, t.entry)
        qty = int(pos.qty)
        value = inst.value(qty, last) * usd
        cost = inst.value(qty, t.entry) * usd
        risk = max(0.0, inst.value(qty, t.entry - t.stop) * usd)
        out.append(Holding(key=t.key, bucket=t.bucket, value_usd=value, cost_usd=cost,
                           pl_usd=value - cost, atr=t.atr, risk_usd=risk))
    return out
