"""Five exchanges, one way of describing an instrument.

Swing trading across the US, London, Hong Kong, Singapore and Tokyo means three
things differ per listing, and each one can break an order if ignored:

**Board lots.** Tokyo and Singapore trade in lots of 100 shares; Hong Kong's
lot is set per stock (100, 500, 1,000, 2,000...). An order for 37 shares of a
Tokyo stock is rejected, so position sizes round *down* to whole lots, and a
stock whose single lot costs more than a slot cannot be bought in one slot.

**Tick sizes.** Every price band has a minimum increment (HK's spread table,
Tokyo's yen steps). A stop at 3.6765 is rejected; it must be 3.68 or 3.67. The
live broker reads IBKR's own market rules for this; the tables below are the
fallback and are deliberately coarse, because a multiple of a coarse tick is
always a valid price and a finer guess might not be.

**Price units.** London quotes in pence while the account holds pounds. IBKR's
``priceMagnifier`` says how many quote units make one currency unit (100 for
pence), so ``value = qty × price / magnifier``. Getting this wrong by 100× in
the safe direction merely blocks the trade as too expensive; the order sanity
check in the guardian catches the other direction.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

__all__ = [
    "Market",
    "MARKETS",
    "Instrument",
    "parse_symbol",
    "round_to_tick",
    "fallback_increments",
    "detect_inverse",
]


@dataclass(frozen=True)
class Market:
    code: str
    name: str
    exchange: str
    currency: str
    default_lot: int
    #: Quote units per currency unit (100 for pence).
    default_magnifier: float
    #: Trading days from trade to settlement. In a cash account, sale proceeds
    #: cannot be spent until then.
    settlement_days: int


MARKETS: dict[str, Market] = {
    "US": Market("US", "United States", "SMART", "USD", 1, 1.0, 1),
    "LSE": Market("LSE", "London", "LSE", "GBP", 1, 100.0, 2),
    "SEHK": Market("SEHK", "Hong Kong", "SEHK", "HKD", 0, 1.0, 2),   # lot is per stock
    "SGX": Market("SGX", "Singapore", "SGX", "SGD", 100, 1.0, 2),
    "TSEJ": Market("TSEJ", "Tokyo", "TSEJ", "JPY", 100, 1.0, 2),
}

_ALIASES = {
    "US": "US", "USA": "US", "NYSE": "US", "NASDAQ": "US", "ARCA": "US", "SMART": "US",
    "LSE": "LSE", "LN": "LSE", "UK": "LSE", "L": "LSE", "LON": "LSE",
    "SEHK": "SEHK", "HK": "SEHK", "HKEX": "SEHK",
    "SGX": "SGX", "SG": "SGX", "SI": "SGX", "SES": "SGX",
    "TSEJ": "TSEJ", "JP": "TSEJ", "T": "TSEJ", "TSE": "TSEJ", "TYO": "TSEJ",
}


def parse_symbol(text: str) -> tuple[str, str]:
    """``"700:HK"`` → ``("700", "SEHK")``; a bare ticker is a US listing.

    Accepts ``SYMBOL:MARKET`` or ``SYMBOL.MARKET``. Hong Kong codes drop their
    leading zeros (``0700`` is ``700`` at IBKR).
    """
    raw = text.strip().upper()
    if not raw:
        raise ValueError("empty symbol")
    sym, mkt = raw, "US"
    for sep in (":", "."):
        if sep in raw:
            head, _, tail = raw.rpartition(sep)
            if tail in _ALIASES:
                sym, mkt = head, _ALIASES[tail]
                break
    if not sym:
        raise ValueError(f"no ticker in {text!r}")
    if mkt == "SEHK":
        sym = sym.lstrip("0") or "0"
    return sym, mkt


@dataclass(frozen=True)
class Instrument:
    """A tradeable listing, resolved far enough to size and price an order."""

    symbol: str
    market: str
    con_id: int
    currency: str
    lot_size: int
    price_magnifier: float = 1.0
    #: IBKR market rule: ((low_edge, increment), ...), ascending by low_edge.
    increments: tuple = field(default_factory=tuple)
    description: str = ""
    inverse: bool = False
    leverage: int | None = None

    @property
    def key(self) -> str:
        return f"{self.symbol}:{self.market}"

    def value(self, qty: float, price: float) -> float:
        """Value in the listing currency (pounds, not pence)."""
        return qty * price / self.price_magnifier

    def tick(self, price: float) -> float:
        incs = self.increments or fallback_increments(self.market)
        return _increment_at(price, incs)

    def round(self, price: float, mode: str) -> float:
        return round_to_tick(price, self.increments or fallback_increments(self.market), mode)

    def decimals(self, price: float) -> int:
        s = f"{self.tick(price):.6f}".rstrip("0").rstrip(".")
        return len(s.split(".")[1]) if "." in s else 0

    def fmt(self, price: float) -> str:
        """A price with exactly the decimals its tick size allows."""
        return f"{price:,.{self.decimals(price)}f}"


def _increment_at(price: float, increments) -> float:
    inc = increments[0][1]
    for low, step in increments:
        if price >= low:
            inc = step
        else:
            break
    return inc


def round_to_tick(price: float, increments, mode: str = "nearest") -> float:
    """Snap a price to the exchange's grid.

    ``mode`` is ``"up"``, ``"down"`` or ``"nearest"``. Rounding uses the band the
    price sits in; a price rounded up across a band edge is re-snapped to the
    coarser band so the result is always valid.
    """
    if price <= 0:
        raise ValueError(f"price must be positive, got {price}")
    inc = _increment_at(price, increments)
    q = price / inc
    # Tolerate float noise: 95.00000000001 is 95.00, not 95.01.
    if abs(q - round(q)) < 1e-6:
        n = round(q)
    elif mode == "up":
        n = math.ceil(q)
    elif mode == "down":
        n = math.floor(q)
    elif mode == "nearest":
        n = round(q)
    else:
        raise ValueError(f"mode must be up, down or nearest, got {mode!r}")
    out = n * inc
    inc2 = _increment_at(out, increments)
    if inc2 != inc:
        q2 = out / inc2
        n2 = math.ceil(q2 - 1e-6) if mode == "up" else math.floor(q2 + 1e-6) if mode == "down" else round(q2)
        out = n2 * inc2
    decimals = max(0, -int(math.floor(math.log10(min(inc, inc2)))) + 1)
    return round(out, decimals)


# Fallback grids. Coarse on purpose: see the module docstring.
_FALLBACK = {
    "US": ((0.0, 0.0001), (1.0, 0.01)),
    # Pence. Whole pence is a multiple of every finer LSE tick.
    "LSE": ((0.0, 0.25), (10.0, 1.0), (5000.0, 5.0)),
    # The SEHK spread table, which is the actual rule.
    "SEHK": (
        (0.0, 0.001), (0.25, 0.005), (0.5, 0.01), (10.0, 0.02), (20.0, 0.05),
        (100.0, 0.1), (200.0, 0.2), (500.0, 0.5), (1000.0, 1.0), (2000.0, 2.0), (5000.0, 5.0),
    ),
    "SGX": ((0.0, 0.001), (0.2, 0.005), (1.0, 0.01)),
    # Standard TSE steps; TOPIX 100 names allow finer, which these are multiples of.
    "TSEJ": ((0.0, 1.0), (3000.0, 5.0), (30000.0, 10.0), (50000.0, 50.0), (300000.0, 100.0)),
}


def fallback_increments(market: str) -> tuple:
    return _FALLBACK.get(market, ((0.0, 0.01),))


_INVERSE_WORDS = ("INVERSE", "SHORT", "BEAR", "ULTRASHORT")


def detect_inverse(description: str) -> tuple[bool, int | None]:
    """Guess from the fund name whether a listing is an inverse ETF and its leverage.

    A hint for the dashboard, not a classification anyone relies on: it adds a
    note to the trade check. ``(False, None)`` when nothing suggests inverse.
    """
    d = f" {description.upper()} "
    inverse = any(w in d for w in _INVERSE_WORDS) or "-1X" in d or "-2X" in d or "-3X" in d
    if not inverse:
        return False, None
    lev = 1
    if "ULTRAPRO" in d or "3X" in d:
        lev = 3
    elif "ULTRASHORT" in d or "2X" in d or " ULTRA " in d:
        lev = 2
    return True, lev
