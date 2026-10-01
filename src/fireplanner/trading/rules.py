"""Every trading rule in one place, so the code and the dashboard agree.

The rules are the user's, written down as numbers:

* **A fixed loss per trade.** Every position is sized so that its stop costs
  the same ``risk_per_trade_usd`` (before gaps), whatever the stock. A tight
  stop means a bigger position, capped at ``max_position_usd``; the whole book
  is capped at ``max_invested_usd`` and ``max_slots`` positions.
* **Stop = the tighter of 5% or 2.5 × the average daily range.**
* **Three stock types**, each with its own target and cap on open positions:
  Steady (+10%), Core (+15%), Volatile (+20%). Suggested from the daily range,
  confirmed by the user.
* **At the target, sell half and trail the rest.** The stop moves to entry at
  +5%, then trails 3 ATRs under the high. It never moves down.
* **Two strikes per stock.** After a stop-out you may re-enter once; a second in
  a row locks the stock until you unlock it (or for ``lockout_days``).
* **One add to a winner**, once its stop is at entry or better, sized for the
  same fixed loss; the combined stop moves up so the whole position still risks
  about one fixed loss.
* **Guards on the book**: a time stop, market weather, sector and total-risk
  caps, and circuit breakers that pause new buys.

Every fraction here is a fraction (0.05), never a percentage (5). ATR arrives
from the indicator layer as a percentage and is divided by 100 at the boundary.
"""

from __future__ import annotations

from dataclasses import dataclass, field, fields, replace
from pathlib import Path

__all__ = ["Bucket", "TradingRules", "DEFAULT_BUCKETS", "BUCKET_IDS", "ADJUSTABLE", "load_rules",
           "load_settings", "with_overrides"]

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
    Bucket("steady", "Steady", 0.020, 0.10, 10),
    Bucket("core", "Core", 0.035, 0.15, 10),
    Bucket("volatile", "Volatile", None, 0.20, 7),
)

#: Market-weather label -> multiplier on the fixed loss for a new trade.
DEFAULT_WEATHER_SIZING = {"Risk-On": 1.0, "Constructive": 1.0, "Neutral": 1.0, "Defensive": 0.5, "Risk-Off": 0.0}


@dataclass(frozen=True)
class TradingRules:
    """The account's trading policy. Defaults are the agreed starting rules."""

    # -- sizing --------------------------------------------------------------
    #: What a stop-out costs, in the base currency, on every trade (before gaps).
    risk_per_trade_usd: float = 250.0
    #: No single position larger than this, however tight its stop.
    max_position_usd: float = 10_000.0
    #: Total the open book may hold at cost.
    max_invested_usd: float = 100_000.0
    #: Maximum open positions across every bucket.
    max_slots: int = 20
    #: Round the share / lot count up (True) or down (False).
    round_up: bool = True
    #: ...but round down instead if rounding up would add more than this to the fixed loss.
    max_round_up_overshoot: float = 0.20

    # -- stops and targets ---------------------------------------------------------
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
    #: Let a stop trigger outside regular trading hours.
    outside_rth: bool = False

    # -- two strikes -----------------------------------------------------------
    #: Consecutive losing exits allowed before a stock is locked.
    max_strikes: int = 2
    #: Trading days a lock lasts. None: until you unlock it yourself.
    lockout_days: int | None = None
    #: An exit worse than this loss counts as a strike (a scratch at entry does not).
    strike_loss_pct: float = 0.005

    # -- adding to a winner -------------------------------------------------------
    allow_add: bool = True
    #: Adds allowed per position ("a second $5k").
    max_adds: int = 1

    # -- time stop -------------------------------------------------------------
    #: Flag a position that has gone this many weeks without reaching time_stop_min_gain.
    time_stop_weeks: float = 4.0
    time_stop_min_gain: float = 0.05
    #: "alert" (flag it, you sell from the dashboard) or "sell" (sell it automatically).
    time_stop_action: str = "alert"

    # -- earnings ---------------------------------------------------------------
    #: Warn when earnings fall within this many calendar days.
    earnings_warn_days: int = 7
    #: Block new buys inside that window (False: warn only).
    earnings_block: bool = False

    # -- market weather -------------------------------------------------------------
    #: Weather label -> multiplier on the fixed loss. 0 blocks new buys.
    weather_sizing: dict = field(default_factory=lambda: dict(DEFAULT_WEATHER_SIZING))
    #: Share of the weather score that comes from breadth (the rest is the regime model).
    breadth_weight: float = 0.25

    # -- concentration --------------------------------------------------------------
    #: Most open positions in one sector (IBKR's industry category).
    max_per_sector: int = 4
    #: Most the whole book may lose if every stop hits at once.
    max_open_risk_usd: float = 5_000.0
    #: Warn when Volatile would drive more than this share of the daily swing.
    swing_guide: float = 0.50
    #: Block a second position in a stock already held (adds go through max_adds).
    one_position_per_stock: bool = True

    # -- circuit breakers -------------------------------------------------------------
    #: Realized losses that pause new buys for the rest of the day / week.
    daily_loss_limit_usd: float = 750.0
    weekly_loss_limit_usd: float = 1_500.0
    #: Stop-outs in a row, across all stocks, that pause new buys until you resume.
    max_consecutive_losses: int = 3
    #: Buy orders the dashboard may send in one day.
    max_entries_per_day: int = 10

    # -- plumbing ------------------------------------------------------------------
    #: A dashboard buy is a limit order this far above the last price.
    entry_limit_buffer: float = 0.005
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

    def weather_multiplier(self, label: str | None) -> float:
        if not label:
            return 1.0
        return float(self.weather_sizing.get(label, 1.0))

    def as_dict(self) -> dict:
        d = {f.name: getattr(self, f.name) for f in fields(self) if f.name not in ("buckets", "fx_fallback")}
        d["buckets"] = [
            {"id": b.id, "name": b.name, "max_atr": b.max_atr, "target": b.target, "cap": b.cap}
            for b in self.buckets
        ]
        return d


#: Rules the dashboard may change at run time, with their allowed range.
ADJUSTABLE = {
    "time_stop_weeks": (1.0, 26.0),
    "time_stop_min_gain": (0.0, 0.5),
    "earnings_warn_days": (0, 60),
    "risk_per_trade_usd": (25.0, 5_000.0),
}


def with_overrides(rules: TradingRules, overrides: dict) -> TradingRules:
    """Apply dashboard-set values, clamped to their allowed range."""
    clean = {}
    for key, value in overrides.items():
        if key not in ADJUSTABLE:
            continue
        lo, hi = ADJUSTABLE[key]
        v = type(lo)(value)
        clean[key] = min(hi, max(lo, v))
    return replace(rules, **clean) if clean else rules


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
    for key in DEFAULT_SETTINGS:
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
    if "weather_sizing" in raw:
        raw["weather_sizing"] = {**rules.weather_sizing, **raw["weather_sizing"]}
    if raw.get("time_stop_action", "alert") not in ("alert", "sell"):
        raise ValueError("trading.time_stop_action must be 'alert' or 'sell'")
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
    # On the first run, positions already in the account are left alone (no stop, no target)
    # until you choose Manage on the dashboard. true: take them all over from the start.
    "manage_existing": False,
    # Index proxy per market for the market-weather check. US also uses VIX,
    # RSP (breadth) and VIX3M; the others use trend and drawdown of the proxy.
    "weather": {"US": "SPY", "LSE": "ISF:LN", "SEHK": "2800:HK", "SGX": "ES3:SG", "TSEJ": "1306:JP"},
    # Breadth baskets per market (defaults in trading/breadth.py) and how many
    # stocks' bars to fetch per cycle, to stay inside IBKR's pacing limit.
    "breadth": {"per_cycle": 8, "baskets": {}},
    # Earnings dates: IBKR has no free calendar, so dates come from you (dashboard
    # or `trade earnings`) and, if FMP_API_KEY is set, Financial Modeling Prep.
    "earnings": {"provider": "manual"},
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
