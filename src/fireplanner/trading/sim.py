"""An in-memory broker that behaves enough like IBKR to rehearse the whole loop.

Orders rest until `set_price` crosses them: a buy limit fills at or below its
price, a sell limit at or above, a sell stop triggers at or below its stop and
fills at the price given, which is how a gap through a stop behaves. One-
Cancels-All groups cancel their siblings on a fill. Like a cash account, it
never sells more than is held.

Used by the tests, by ``fireplanner trade demo``, and for a dry run of the
guardian against a scripted price path before it ever touches a paper account.
"""

from __future__ import annotations

import itertools
import time
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from .broker import BrokerFill, BrokerOrder, BrokerPosition, CashView, OrderSpec
from .markets import Instrument

__all__ = ["SimBroker"]


class SimBroker:
    mode = "sim"

    def __init__(self, cash_usd: float = 100_000.0, fx: dict | None = None,
                 clock: datetime | None = None):
        self._fx = {"USD": 1.0, "GBP": 1.27, "HKD": 0.128, "SGD": 0.77, "JPY": 0.0067, **(fx or {})}
        self._inst: dict[int, Instrument] = {}
        self._by_key: dict[str, Instrument] = {}
        self._price: dict[int, float] = {}
        self._atr: dict[int, float | None] = {}
        self._pos: dict[int, list] = {}          # con_id -> [qty, avg_cost]
        self._orders: dict[int, BrokerOrder] = {}
        self._fills: list[BrokerFill] = []
        self._ids = itertools.count(1)
        self._exec_ids = itertools.count(1)
        self.cash_usd = cash_usd
        self.clock = clock or datetime(2026, 9, 30, 14, 0, tzinfo=timezone.utc)
        self.dirty = False
        self.rejected: list[str] = []
        self._bars: dict = {}

    # -- setup -----------------------------------------------------------
    def add(self, inst: Instrument, price: float, atr: float | None = None) -> Instrument:
        self._inst[inst.con_id] = inst
        self._by_key[inst.key] = inst
        self._price[inst.con_id] = price
        self._atr[inst.con_id] = atr
        return inst

    def hold(self, con_id: int, qty: int, avg_cost: float) -> None:
        """Seed a position as if it had been bought earlier (no fill recorded)."""
        self._pos[con_id] = [qty, avg_cost]
        inst = self._inst[con_id]
        self.cash_usd -= inst.value(qty, avg_cost) * self._fx[inst.currency]

    def now(self) -> datetime:
        return self.clock

    def tick(self, minutes: int = 1) -> None:
        self.clock += timedelta(minutes=minutes)

    # -- Broker protocol ---------------------------------------------------
    def positions(self) -> list[BrokerPosition]:
        out = []
        for cid, (qty, avg) in self._pos.items():
            if qty == 0:
                continue
            i = self._inst[cid]
            out.append(BrokerPosition(cid, i.symbol, i.market, i.currency, qty, avg))
        return out

    def open_orders(self) -> list[BrokerOrder]:
        return [o for o in self._orders.values() if o.status == "Submitted"]

    def fills(self) -> list[BrokerFill]:
        return list(self._fills)

    def quotes(self, instruments: list[Instrument]) -> dict[int, float]:
        return {i.con_id: self._price[i.con_id] for i in instruments if i.con_id in self._price}

    def instrument(self, symbol: str, market: str, lot_size: int | None = None) -> Instrument:
        inst = self._by_key.get(f"{symbol}:{market}")
        if inst is None:
            raise LookupError(f"no such listing: {symbol}:{market}")
        if lot_size and inst.lot_size != lot_size:
            inst = replace(inst, lot_size=lot_size)
            self._inst[inst.con_id] = inst
            self._by_key[inst.key] = inst
        return inst

    def instrument_for(self, position: BrokerPosition) -> Instrument:
        return self._inst[position.con_id]

    def daily_atr(self, inst: Instrument) -> float | None:
        return self._atr.get(inst.con_id)

    def set_bars(self, symbol_text: str, frame) -> None:
        """Daily bars served by `bars`, keyed by ``"SPY"``, ``"2800:HK"`` and so on."""
        self._bars[symbol_text.upper()] = frame

    def bars(self, symbol_text: str, days: int = 520):
        frame = self._bars.get(symbol_text.upper())
        if frame is None:
            raise LookupError(f"no bars for {symbol_text}")
        return frame.tail(days)

    def usd_per_unit(self, currency: str) -> tuple[float, bool]:
        return self._fx[currency], True

    def cash(self) -> CashView:
        return CashView(available_usd=self.cash_usd, by_currency={})

    def place(self, spec: OrderSpec) -> int:
        if spec.con_id not in self._inst:
            raise LookupError(f"unknown con_id {spec.con_id}")
        if spec.qty <= 0:
            raise ValueError("quantity must be positive")
        self.tick()                               # time passes between orders, as it does live
        lot = max(1, self._inst[spec.con_id].lot_size)
        if spec.qty % lot:
            raise ValueError(f"{spec.qty} is not a multiple of the {lot}-share lot")
        oid = next(self._ids)
        self._orders[oid] = BrokerOrder(
            order_id=oid, con_id=spec.con_id, action=spec.action, order_type=spec.order_type,
            qty=spec.qty, stop_price=spec.stop_price, limit_price=spec.limit_price, tif=spec.tif,
            oca_group=spec.oca_group, order_ref=spec.order_ref,
        )
        self._match(spec.con_id)
        return oid

    def preview_cost_usd(self, spec: OrderSpec) -> float | None:
        inst = self._inst[spec.con_id]
        px = spec.limit_price or self._price[spec.con_id]
        return inst.value(spec.qty, px) * self._fx[inst.currency]

    def modify(self, order: BrokerOrder, *, qty=None, stop_price=None, limit_price=None) -> None:
        cur = self._orders[order.order_id]
        if cur.status != "Submitted":
            raise RuntimeError(f"order {order.order_id} is {cur.status}")
        changes = {}
        if qty is not None:
            changes["qty"] = cur.filled + qty
        if stop_price is not None:
            changes["stop_price"] = stop_price
        if limit_price is not None:
            changes["limit_price"] = limit_price
        self._orders[order.order_id] = replace(cur, **changes)
        self._match(order.con_id)

    def cancel(self, order: BrokerOrder) -> None:
        cur = self._orders.get(order.order_id)
        if cur and cur.status == "Submitted":
            self._orders[order.order_id] = replace(cur, status="Cancelled")

    def wait(self, seconds: float) -> None:
        # Real (short) sleep so a running loop doesn't spin; simulated time only
        # moves with prices and orders.
        time.sleep(min(seconds, 0.05))

    def status(self) -> dict:
        return {"broker": "Simulator", "connected": True, "account": "SIM", "account_type": "sim"}

    def orders_allowed(self) -> tuple[bool | None, str]:
        return True, "Simulated orders are always accepted."

    def pop_dirty(self) -> bool:
        d, self.dirty = self.dirty, False
        return d

    # -- market ------------------------------------------------------------
    def set_price(self, con_id: int, price: float) -> None:
        self._price[con_id] = price
        self.tick()
        self._match(con_id)

    def _match(self, con_id: int) -> None:
        px = self._price[con_id]
        for oid in sorted(self._orders):
            o = self._orders[oid]
            if o.con_id != con_id or o.status != "Submitted":
                continue
            hit = (
                (o.action == "BUY" and o.order_type == "LMT" and px <= o.limit_price)
                or (o.action == "SELL" and o.order_type == "LMT" and px >= o.limit_price)
                or (o.action == "SELL" and o.order_type == "STP" and px <= o.stop_price)
                or o.order_type == "MKT"
            )
            if hit:
                fill_px = o.limit_price if o.order_type == "LMT" and o.action == "BUY" else px
                if o.order_type == "LMT" and o.action == "SELL":
                    fill_px = max(px, o.limit_price)
                self._fill(o, fill_px)

    def _fill(self, o: BrokerOrder, price: float) -> None:
        inst = self._inst[o.con_id]
        qty_held, avg = self._pos.get(o.con_id, [0, 0.0])
        qty = o.remaining
        if o.action == "SELL":
            qty = min(qty, qty_held)
            if qty <= 0:
                self._orders[o.order_id] = replace(o, status="Cancelled")
                self.rejected.append(f"{o.order_ref or o.order_id}: nothing to sell")
                return
            self._pos[o.con_id] = [qty_held - qty, avg if qty_held - qty else 0.0]
            self.cash_usd += inst.value(qty, price) * self._fx[inst.currency]
        else:
            new = qty_held + qty
            self._pos[o.con_id] = [new, (qty_held * avg + qty * price) / new]
            self.cash_usd -= inst.value(qty, price) * self._fx[inst.currency]
        self._orders[o.order_id] = replace(o, filled=o.filled + qty, status="Filled")
        self._fills.append(BrokerFill(exec_id=f"sim{next(self._exec_ids)}", con_id=o.con_id, side=o.action,
                                      qty=qty, price=price, time=self.clock, order_ref=o.order_ref))
        self.dirty = True
        if o.oca_group:
            for other in list(self._orders.values()):
                if other.order_id != o.order_id and other.oca_group == o.oca_group and other.status == "Submitted":
                    self._orders[other.order_id] = replace(other, status="Cancelled")
