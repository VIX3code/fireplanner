"""Every trading rule in one place, so the code and the dashboard agree.

The rules are the user's, written down as numbers:

* **$5,000 per trade, at most 20 open.** A slot is a fixed amount in USD,
  converted to the stock's own currency at the day's rate.
* **Stop = the tighter of 5% or 2.5 × the average daily range.** A calm stock
  gets a tighter stop than 5%; a volatile one is capped at 5%. Either way a
  stop-out costs at most about $250 before gaps.
* **Three stock types, each with its own target and cap on open positions.**
  Steady (+10%), Core (+15%), Volatile (+20%). The type is suggested from the
  average daily range and confirmed by the user.
* **At the target, sell half and trail the rest.** The stop moves to the entry
  price once the stock has been 5% above entry, then trails 3 ATRs under the
  highest price since entry. It never moves down.
* **Two strikes per stock.** After a stop-out you may re-enter once. A second
  stop-out in a row locks the stock for 10 trading days.

Every fraction here is a fraction (0.05), never a percentage (5). ATR arrives
from the indicator layer as a percentage and is divided by 100 at the boundary.
"""

from __future__ import annotations

from dataclasses import dataclass, field, fields, replace
from pathlib import Path

__all__ = ["Bucket", "TradingRules", "DEFAULT_BUCKETS", "BUCKET_IDS", "load_rules", "load_settings"]

BUCKET_IDS = ("steady", "core", "volatile")


@dataclass(frozen=True)
class Bucket:
    """One stock type: how it is recognised, what it aims for, how many may be open."""

    id: str
    name: str
    #: Upper bound on the average daily range (fraction of price). None = no bound.
    max_atr: float | None
    #: Profit target as a fraction of entry.
    target: float
    #: Maximum open positions of this type.
    cap: int


DEFAULT_BUCKETS: tuple[Bucket, ...] = (
    Bucket("steady", "Steady", 0.020, 0.10, 20),
    Bucket("core", "Core", 0.035, 0.15, 10),
    Bucket("volatile", "Volatile", None, 0.20, 5),
)


@dataclass(frozen=True)
class TradingRules:
    """The account's trading policy. Defaults are the agreed starting rules."""

    #: Size of one position, in the base currency.
    slot_usd: float = 5000.0
    #: Maximum open positions across every bucket.
    max_slots: int = 20
    #: Widest allowed initial stop, as a fraction below entry.
    max_stop_pct: float = 0.05
    #: Volatility stop distance in ATRs; the stop is the tighter of this and max_stop_pct.
    atr_stop_mult: float = 2.5
    #: Move the stop to the entry price once the high since entry is this far up.
    breakeven_at: float = 0.05
    #: Trail this many ATRs below the high since entry, once at breakeven.
    atr_trail_mult: float = 3.0
    #: Sell half at the target and trail the rest (False: sell everything).
    take_half_at_target: bool = True
    #: Consecutive losing exits allowed before a stock is locked.
    max_strikes: int = 2
    #: Trading days a stock stays locked after the last allowed strike.
    lockout_days: int = 10
    #: An exit worse than this loss counts as a strike. Keeps a scratch at
    #: breakeven (commissions only) from counting as a stop-out.
    strike_loss_pct: float = 0.005
    #: Warn when Volatile would drive more than this share of the daily swing.
    swing_guide: float = 0.50
    #: Block a second position in a stock that is already held.
    one_position_per_stock: bool = True
    #: A dashboard buy is a limit order this far above the last price.
    entry_limit_buffer: float = 0.005
    #: Let a stop trigger outside regular trading hours.
    outside_rth: bool = False
    #: Currency every total is reported in.
    base_currency: str = "USD"
    #: Used only when the broker cannot supply a live rate. USD per 1 unit.
    fx_fallback: dict = field(default_factory=lambda: {
        "USD": 1.0, "GBP": 1.27, "HKD": 0.128, "SGD": 0.77, "JPY": 0.0067,
    })
    buckets: tuple[Bucket, ...] = DEFAULT_BUCKETS

    # -- lookups ---------------------------------------------------------
    def bucket(self, bucket_id: str) -> Bucket:
        for b in self.buckets:
            if b.id == bucket_id:
                return b
        raise KeyError(f"unknown stock type {bucket_id!r}; expected one of {[b.id for b in self.buckets]}")

    def suggest_bucket(self, atr: float | None) -> str:
        """The stock type an average daily range (fraction of price) points to.

        With no ATR (a new listing, no history yet) the answer is the most
        cautious type, so a missing number can never make a stock look calm.
        """
        if atr is None or atr != atr:  # NaN
            return self.buckets[-1].id
        for b in self.buckets:
            if b.max_atr is None or atr < b.max_atr:
                return b.id
        return self.buckets[-1].id

    def stop_pct(self, atr: float | None) -> float:
        """Initial stop distance: the tighter of the cap and the ATR multiple."""
        if atr is None or atr != atr or atr <= 0:
            return self.max_stop_pct
        return min(self.max_stop_pct, self.atr_stop_mult * atr)

    def as_dict(self) -> dict:
        return {
            "slot_usd": self.slot_usd,
            "max_slots": self.max_slots,
            "max_stop_pct": self.max_stop_pct,
            "atr_stop_mult": self.atr_stop_mult,
            "breakeven_at": self.breakeven_at,
            "atr_trail_mult": self.atr_trail_mult,
            "take_half_at_target": self.take_half_at_target,
            "max_strikes": self.max_strikes,
            "lockout_days": self.lockout_days,
            "swing_guide": self.swing_guide,
            "base_currency": self.base_currency,
            "buckets": [
                {"id": b.id, "name": b.name, "max_atr": b.max_atr, "target": b.target, "cap": b.cap}
                for b in self.buckets
            ],
        }


def load_rules(path: str | Path | None = "config/config.yaml") -> TradingRules:
    """Read the ``trading:`` section of the config file over the defaults.

    Unknown keys are an error rather than silently ignored: a typo in a risk
    limit (``max_slot: 5``) must not quietly leave the default of 20 in force.
    """
    rules = TradingRules()
    if path is None or not Path(path).exists():
        return rules

    import yaml

    raw = (yaml.safe_load(Path(path).read_text()) or {}).get("trading") or {}
    known = {f.name for f in fields(TradingRules)}
    raw = dict(raw)
    # Operational settings live in the same section but are not rules.
    for key in ("enabled", "allow_live", "journal", "dashboard", "guardian", "watchlist", "lot_sizes"):
        raw.pop(key, None)

    unknown = set(raw) - known
    if unknown:
        raise ValueError(f"unknown trading setting(s) in {path}: {sorted(unknown)}")

    buckets = raw.pop("buckets", None)
    if buckets is not None:
        built = []
        for bid in BUCKET_IDS:
            spec = buckets.get(bid)
            if spec is None:
                raise ValueError(f"trading.buckets must define {bid!r}")
            default = next(b for b in DEFAULT_BUCKETS if b.id == bid)
            built.append(Bucket(
                id=bid,
                name=spec.get("name", default.name),
                max_atr=spec.get("max_atr", default.max_atr),
                target=float(spec.get("target", default.target)),
                cap=int(spec.get("cap", default.cap)),
            ))
        raw["buckets"] = tuple(built)

    if "fx_fallback" in raw:
        raw["fx_fallback"] = {**rules.fx_fallback, **raw["fx_fallback"]}
    return replace(rules, **raw)


#: Operational settings: how the service runs, as opposed to what the rules are.
DEFAULT_SETTINGS = {
    "enabled": False,          # False = dry run: orders are logged, never sent
    "allow_live": False,       # refuse live ports (4001 / 7496) until True
    "journal": "data/trading/journal.sqlite",
    "guardian": {"client_id": 23, "interval_seconds": 30},
    "dashboard": {"host": "127.0.0.1", "port": 8765},
    "watchlist": [],
    "lot_sizes": {},
}


def load_settings(path: str | Path | None = "config/config.yaml") -> dict:
    """The operational part of the ``trading:`` section, over the defaults."""
    out = {k: (dict(v) if isinstance(v, dict) else list(v) if isinstance(v, list) else v)
           for k, v in DEFAULT_SETTINGS.items()}
    if path is None or not Path(path).exists():
        return out
    import yaml

    raw = (yaml.safe_load(Path(path).read_text()) or {}).get("trading") or {}
    for key in DEFAULT_SETTINGS:
        if key in raw and raw[key] is not None:
            if isinstance(out[key], dict) and isinstance(raw[key], dict):
                out[key].update(raw[key])
            else:
                out[key] = raw[key]
    return out
