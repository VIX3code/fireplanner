"""What the trading layer needs from a broker, and nothing more.

Two implementations: `SimBroker` (in-memory, for tests, demos and rehearsal)
and `IBBroker` (TWS / IB Gateway through ``ib_async``). Everything above this
line — sync, guardian, gate, dashboard — only sees these types, so the whole
system can be exercised end to end without an IBKR login.

Prices are always in **quote units** (pence for London), the units orders are
priced in. Adapters convert anything the broker reports otherwise.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

from .markets import Instrument

__all__ = ["BrokerPosition", "BrokerOrder", "BrokerFill", "OrderSpec", "CashView", "Broker", "PacingDeferred",
           "order_ref", "parse_ref"]


class PacingDeferred(Exception):
    """A historical-data request was held back to stay inside IBKR's pacing limit. Try again later."""


def order_ref(role: str, trade_id: int | str) -> str:
    """Tag carried on every order this system places, visible in TWS."""
    return f"fp:{role}:{trade_id}"


def parse_ref(ref: str | None) -> tuple[str | None, int | None]:
    if not ref or not ref.startswith("fp:"):
        return None, None
    parts = ref.split(":")
    if len(parts) != 3:
        return None, None
    try:
        return parts[1], int(parts[2])
    except ValueError:
        return parts[1], None


@dataclass(frozen=True)
class BrokerPosition:
    con_id: int
    symbol: str
    market: str
    currency: str
    qty: float
    #: Average cost per share, in quote units.
    avg_cost: float


@dataclass(frozen=True)
class BrokerOrder:
    order_id: int
    con_id: int
    action: str                 # BUY | SELL
    order_type: str             # STP | LMT | STP LMT | TRAIL | MKT
    qty: float
    stop_price: float | None = None
    limit_price: float | None = None
    tif: str = "GTC"
    oca_group: str = ""
    order_ref: str = ""
    status: str = "Submitted"
    filled: float = 0.0

    @property
    def role(self) -> str | None:
        return parse_ref(self.order_ref)[0]

    @property
    def trade_id(self) -> int | None:
        return parse_ref(self.order_ref)[1]

    @property
    def managed(self) -> bool:
        return self.role is not None

    @property
    def remaining(self) -> float:
        return self.qty - self.filled

    @property
    def is_protective_stop(self) -> bool:
        return self.action == "SELL" and self.order_type in {"STP", "STP LMT", "TRAIL", "TRAIL LIMIT"}


@dataclass(frozen=True)
class BrokerFill:
    exec_id: str
    con_id: int
    side: str                   # BUY | SELL
    qty: float
    price: float
    time: datetime
    order_ref: str = ""


@dataclass(frozen=True)
class OrderSpec:
    con_id: int
    action: str
    order_type: str
    qty: int
    stop_price: float | None = None
    limit_price: float | None = None
    tif: str = "GTC"
    oca_group: str = ""
    #: 1 = cancel the rest of the group when one fills. Deterministic, and the
    #: guardian re-places whatever the remaining position still needs.
    oca_type: int = 1
    order_ref: str = ""
    outside_rth: bool = False


@dataclass
class CashView:
    #: Cash that can be spent now, in the base currency. None if unknown.
    available_usd: float | None = None
    #: Cash balance per currency (can be negative only in a margin account).
    by_currency: dict = field(default_factory=dict)


class Broker(Protocol):
    #: "sim", "paper" or "live".
    mode: str

    def positions(self) -> list[BrokerPosition]: ...
    def open_orders(self) -> list[BrokerOrder]: ...
    def fills(self) -> list[BrokerFill]: ...
    def quotes(self, instruments: list[Instrument]) -> dict[int, float]: ...
    def instrument(self, symbol: str, market: str, lot_size: int | None = None) -> Instrument: ...
    def instrument_for(self, position: BrokerPosition) -> Instrument: ...
    def daily_atr(self, inst: Instrument) -> float | None: ...
    def bars(self, symbol_text: str, days: int = 520): ...
    def usd_per_unit(self, currency: str) -> tuple[float, bool]: ...
    def cash(self) -> CashView: ...
    def place(self, spec: OrderSpec) -> int: ...
    def modify(self, order: BrokerOrder, *, qty: int | None = None, stop_price: float | None = None,
               limit_price: float | None = None) -> None: ...
    def cancel(self, order: BrokerOrder) -> None: ...
    def preview_cost_usd(self, spec: OrderSpec) -> float | None: ...
    def wait(self, seconds: float) -> None: ...
    def now(self) -> datetime: ...
    def pop_dirty(self) -> bool: ...
