"""The pre-trade check: every rule a new buy (or an add) must pass, with the reason for each.

Levels:

* ``crit`` blocks the order (breaker on, bucket full, stock locked, sector full...).
* ``warn`` allows it but says what is off (swing guide, earnings, last try).
* ``good`` is a rule that passed; ``info`` is context that changes nothing.

The dashboard refuses to send a blocked order. A buy placed directly in TWS or
the IBKR app cannot be blocked from here, so the guardian applies the same
rules after the fill and raises an alert instead.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

from .breakers import BreakerState
from .buckets import Holding, swing_share_after
from .markets import MARKETS, Instrument
from .rules import TradingRules
from .sizing import EntryPlan
from .strikes import StrikeStatus
from .weather import WeatherReading

__all__ = ["Check", "GateContext", "GateResult", "check_entry"]


@dataclass
class Check:
    level: str
    title: str
    detail: str


@dataclass
class GateContext:
    """What the book looks like, for the rules that depend on it."""

    holdings: list[Holding]
    held_keys: set[str]
    working_buy_keys: set[str]
    strike: StrikeStatus
    suggested_bucket: str
    bucket_confirmed: bool
    cash_usd: float | None = None
    currency_cash: float | None = None
    fx_live: bool = True
    weather: WeatherReading | None = None
    breaker: BreakerState | None = None
    #: {"date": "2026-10-28", "days": 5} for the next earnings, if known.
    earnings: dict | None = None
    sector: str = ""
    #: Open positions per sector.
    sector_counts: dict = field(default_factory=dict)
    #: Anchored VWAPs: {"anchors": [Anchor dicts], "pullback": Anchor dict or None}.
    avwap: dict | None = None
    #: Adding to an open winner rather than opening a position.
    is_add: bool = False
    #: Why an add isn't allowed, or None.
    add_problem: str | None = None


@dataclass
class GateResult:
    verdict: str                     # allowed | warning | blocked
    plan: EntryPlan
    checks: list[Check] = field(default_factory=list)
    suggested_bucket: str = ""
    bucket_confirmed: bool = False

    @property
    def allowed(self) -> bool:
        return self.verdict != "blocked"

    def as_dict(self) -> dict:
        return {
            "verdict": self.verdict,
            "plan": self.plan.as_dict(),
            "checks": [asdict(c) for c in self.checks],
            "suggested_bucket": self.suggested_bucket,
            "bucket_confirmed": self.bucket_confirmed,
        }


def _money(x: float) -> str:
    return f"${x:,.0f}"


def _decimals(x: float) -> int:
    return 0 if x >= 1000 else 2 if x >= 1 else 4


def check_entry(plan: EntryPlan, inst: Instrument, *, rules: TradingRules, ctx: GateContext) -> GateResult:
    checks: list[Check] = []
    add = checks.append
    bucket = rules.bucket(plan.bucket)
    market = MARKETS[inst.market].name

    # circuit breakers
    br = ctx.breaker
    if br is not None:
        if br.paused:
            add(Check("crit", "New buys are paused", " ".join(br.reasons)))
        if br.entries_today >= rules.max_entries_per_day:
            add(Check("crit", "Daily buy limit reached",
                      f"{br.entries_today} buys sent today (limit {rules.max_entries_per_day})."))

    # market weather
    w = ctx.weather
    if w is None or w.label is None:
        add(Check("info", f"No market weather for {market}", (w.note if w else "") or "Sizing is not adjusted."))
    elif w.multiplier <= 0:
        add(Check("crit", f"Market weather: {w.label} in {market}", "New buys in this market are paused."))
    elif w.multiplier < 1:
        add(Check("warn", f"Market weather: {w.label} in {market}",
                  f"Sized for {w.multiplier:.0%} of the fixed loss ({_money(plan.risk_budget_usd)})."))
    else:
        add(Check("good", f"Market weather: {w.label} in {market}", "Full size"))

    # sizing
    if plan.problem:
        add(Check("crit", "Can't size this trade", plan.problem))

    if ctx.is_add:
        if ctx.add_problem:
            add(Check("crit", "Can't add to this position", ctx.add_problem))
        else:
            add(Check("good", "Adding to a winner",
                      f"Its stop is at or above entry. After the add the whole position's stop moves up to "
                      f"about {plan.stop_pct:.1%} under the add price."))
        if inst.key in ctx.working_buy_keys:
            add(Check("crit", "A buy order is already working", f"Cancel or wait for the open {inst.symbol} buy first."))
    else:
        used = len(ctx.holdings)
        if used >= rules.max_slots:
            add(Check("crit", "No free position", f"All {rules.max_slots} positions are in use. Close one first."))
        else:
            add(Check("good", "Free position", f"{used} of {rules.max_slots} in use"))

        in_bucket = sum(1 for h in ctx.holdings if h.bucket == bucket.id)
        if in_bucket >= bucket.cap:
            add(Check("crit", f"{bucket.name} bucket is full",
                      f"{in_bucket} of {bucket.cap} used. Close a {bucket.name} position first."))
        else:
            last = ". This takes the last one." if in_bucket + 1 == bucket.cap else ""
            add(Check("good", f"{bucket.name} bucket has room", f"{in_bucket} of {bucket.cap} used{last}"))

        st = ctx.strike
        if st.locked:
            when = f"Unlocks {st.unlocks_on:%d %b}." if st.unlocks_on else "Locked until you unlock it."
            add(Check("crit", "Locked by the two-strike rule",
                      f"Stopped out {rules.max_strikes} times in a row. {when}"))
        elif st.strikes >= rules.max_strikes - 1 and st.strikes > 0:
            add(Check("warn", "Last try on this stock",
                      f"Stopped out {st.strikes} time(s) in a row. Another stop-out locks it."))
        else:
            add(Check("good", "No recent stop-outs", f"{rules.max_strikes} tries available"))

        if inst.key in ctx.working_buy_keys:
            add(Check("crit", "A buy order is already working", f"Cancel or wait for the open {inst.symbol} buy first."))
        if inst.key in ctx.held_keys and rules.one_position_per_stock:
            add(Check("crit", f"Already holding {inst.symbol}",
                      "One position per stock. To add to a winner, use Add on its row."))
        elif inst.key not in ctx.working_buy_keys:
            add(Check("good", "Not already held", "One position per stock at a time"))

        sector = ctx.sector or ""
        if sector and sector.lower() != "unknown":
            n = ctx.sector_counts.get(sector, 0)
            if n >= rules.max_per_sector:
                add(Check("crit", f"Sector full: {sector}",
                          f"{n} positions already in {sector} (limit {rules.max_per_sector}). They tend to move "
                          f"together, so this would be one bigger bet, not a new one."))
            else:
                add(Check("good", f"Sector: {sector}", f"{n} of {rules.max_per_sector} used"))

    # book-wide caps
    if plan.qty > 0:
        invested = sum(h.cost_usd for h in ctx.holdings)
        if invested + plan.cost_usd > rules.max_invested_usd:
            add(Check("crit", "Over the capital limit",
                      f"{_money(invested)} invested + {_money(plan.cost_usd)} would pass the "
                      f"{_money(rules.max_invested_usd)} limit."))
        open_risk = sum(h.risk_usd for h in ctx.holdings)
        if open_risk + plan.max_loss_usd > rules.max_open_risk_usd:
            add(Check("crit", "Too much open risk",
                      f"Every stop hitting at once would lose {_money(open_risk + plan.max_loss_usd)}, over the "
                      f"{_money(rules.max_open_risk_usd)} limit."))
        else:
            add(Check("good", "Open risk within limit",
                      f"{_money(open_risk + plan.max_loss_usd)} if every stop hit (limit {_money(rules.max_open_risk_usd)})"))

        new = Holding(key=inst.key, bucket=bucket.id, value_usd=plan.cost_usd, cost_usd=plan.cost_usd,
                      pl_usd=0.0, atr=plan.atr)
        before, after = swing_share_after(ctx.holdings, new, "volatile")
        move = f"Volatile share of daily swing: {before:.0%} → {after:.0%}"
        if after > rules.swing_guide and after >= before - 1e-9:
            add(Check("warn", "Above the swing guide", f"{move}, over the {rules.swing_guide:.0%} guide."))
        elif after > rules.swing_guide:
            add(Check("info", "Brings Volatile's share down", f"{move}; still over the {rules.swing_guide:.0%} guide."))
        else:
            add(Check("good", "Within the swing guide", move))

    # anchored VWAP: where buyers since an event are, relative to this price
    av = ctx.avwap or {}
    anchors = av.get("anchors") or []
    if anchors:
        fmt = lambda x: f"{x:,.{_decimals(x)}f}"   # noqa: E731
        above = [a for a in anchors if a["distance"] < 0]
        pb = av.get("pullback")
        if above:
            names = " and ".join(f"{a['label'].split(' ')[0].lower()} VWAP ({fmt(a['avwap'])})" for a in above[:2])
            add(Check("warn", f"Below its {names}",
                      "Buyers since then are losing money and tend to sell into rallies near that price."))
        supports = [a for a in anchors if a["distance"] > 0 and not (a["kind"] == "gap" and (a.get("gap") or 0) < 0)]
        if not pb and supports:
            near = min(supports, key=lambda a: a["distance"])
            add(Check("info", f"Extended: {near['distance']:.1%} above its {near['label']} VWAP",
                      f"No support level within three average days' range. A buy here has further to fall "
                      f"to {fmt(near['avwap'])} if it pulls back."))
        if pb:
            level, dist = pb["avwap"], pb["distance"]
            if plan.limit <= level * 1.002:
                add(Check("good", f"Buying at the {pb['label']} VWAP",
                          f"A limit at {fmt(plan.limit)} waits for the stock to come back to what buyers "
                          f"since {pb['label'].split(' ', 1)[-1]} paid."))
            else:
                extended = plan.atr and dist > 2 * plan.atr
                add(Check("info", f"Pullback level: {pb['label']} VWAP {fmt(level)}",
                          (f"The price is {dist:.1%} above it, more than two average days' range: extended. "
                           if extended else f"The price is {dist:.1%} above it. ")
                          + "A limit there enters nearer support; the stop moves down with it."))

    # earnings
    e = ctx.earnings
    if e is not None and e.get("days") is not None and e["days"] <= rules.earnings_warn_days:
        when = "today" if e["days"] == 0 else f"in {e['days']} day{'s' if e['days'] != 1 else ''}"
        add(Check("crit" if rules.earnings_block else "warn", f"Earnings {when} ({e['date']})",
                  "A stop can't protect against an earnings gap: the stock can open far below it."))

    # cash account
    if ctx.cash_usd is not None and plan.qty > 0:
        if ctx.cash_usd < plan.cost_usd:
            add(Check("crit", "Not enough settled cash",
                      f"{_money(ctx.cash_usd)} available, this buy needs {_money(plan.cost_usd)}. "
                      f"In a cash account, sale proceeds are usable only after settlement."))
        else:
            add(Check("good", "Enough settled cash", f"{_money(ctx.cash_usd)} available"))
    if (ctx.currency_cash is not None and plan.qty > 0 and plan.currency != rules.base_currency
            and ctx.currency_cash < plan.cost_local):
        add(Check("warn", f"Not enough {plan.currency} in the account",
                  f"You hold {plan.currency} {ctx.currency_cash:,.0f}; this buy needs {plan.currency} "
                  f"{plan.cost_local:,.0f}. A cash account can't borrow currency, so convert first "
                  f"or IBKR may reject the order."))

    # context
    if plan.sized_by == "rounded down" and plan.qty > 0:
        add(Check("info", "Rounded down",
                  f"One more lot would have lost more than {rules.max_round_up_overshoot:.0%} over the fixed loss; "
                  f"this size loses {_money(plan.max_loss_usd)}."))
    elif plan.sized_by == "max position" and plan.qty > 0:
        add(Check("info", "Capped at the position limit",
                  f"{_money(rules.max_position_usd)} maximum, so a stop-out loses {_money(plan.max_loss_usd)} "
                  f"rather than {_money(plan.risk_budget_usd)}."))
    if plan.atr is None:
        add(Check("warn", "No daily-range history",
                  f"Stop set at the {rules.max_stop_pct:.0%} cap and type defaulted to Volatile."))
    if not ctx.bucket_confirmed and not ctx.is_add:
        add(Check("info", "Stock type not confirmed yet",
                  f"Suggested as {rules.bucket(ctx.suggested_bucket).name} from its daily range. "
                  f"Buying confirms it."))
    if inst.inverse:
        if (inst.leverage or 1) > 1:
            add(Check("warn", f"Leveraged inverse ETF (−{inst.leverage}×)",
                      "It resets every day, so over several weeks it drifts away from "
                      f"−{inst.leverage}× the index, especially in choppy markets."))
        else:
            add(Check("info", "Inverse ETF", "Gains when the market falls. Resets daily."))
    if not ctx.fx_live and plan.currency != rules.base_currency:
        add(Check("info", "Approximate exchange rate",
                  f"Sized with the fallback {plan.currency} rate; the live rate was unavailable."))

    levels = {c.level for c in checks}
    verdict = "blocked" if "crit" in levels else "warning" if "warn" in levels else "allowed"
    return GateResult(verdict=verdict, plan=plan, checks=checks,
                      suggested_bucket=ctx.suggested_bucket, bucket_confirmed=ctx.bucket_confirmed)
