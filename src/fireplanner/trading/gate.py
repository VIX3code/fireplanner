"""The pre-trade check: every rule a new buy must pass, with the reason for each.

Levels:

* ``crit`` blocks the order (bucket full, stock locked, already held...).
* ``warn`` allows it but says what is off (swing guide, last try).
* ``good`` is a rule that passed; ``info`` is context that changes nothing.

The dashboard refuses to send a blocked order. A buy placed directly in TWS or
the IBKR app cannot be blocked from here, so the guardian applies the same
checks after the fill and raises an alert instead.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

from .buckets import Holding, swing_share_after
from .markets import Instrument
from .rules import TradingRules
from .sizing import EntryPlan
from .strikes import StrikeStatus

__all__ = ["Check", "GateResult", "check_entry"]


@dataclass
class Check:
    level: str
    title: str
    detail: str


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


def check_entry(
    plan: EntryPlan,
    inst: Instrument,
    *,
    rules: TradingRules,
    holdings: list[Holding],
    held_keys: set[str],
    working_buy_keys: set[str],
    strike: StrikeStatus,
    suggested_bucket: str,
    bucket_confirmed: bool,
    cash_usd: float | None = None,
    currency_cash: float | None = None,
    fx_live: bool = True,
) -> GateResult:
    checks: list[Check] = []
    bucket = rules.bucket(plan.bucket)
    add = checks.append

    # sizing
    if plan.problem:
        add(Check("crit", "Can't size this trade", plan.problem))

    # book and bucket room
    used = len(holdings)
    if used >= rules.max_slots:
        add(Check("crit", "No free slot", f"All {rules.max_slots} slots are in use. Close a position first."))
    else:
        add(Check("good", "Free slot", f"{used} of {rules.max_slots} slots in use"))

    in_bucket = sum(1 for h in holdings if h.bucket == bucket.id)
    if in_bucket >= bucket.cap:
        add(Check("crit", f"{bucket.name} bucket is full",
                  f"{in_bucket} of {bucket.cap} used. Close a {bucket.name} position first."))
    else:
        last = ". This takes the last slot." if in_bucket + 1 == bucket.cap else ""
        add(Check("good", f"{bucket.name} bucket has room", f"{in_bucket} of {bucket.cap} used{last}"))

    # two strikes
    if strike.locked:
        add(Check("crit", "Locked by the two-strike rule",
                  f"Stopped out {rules.max_strikes} times in a row. Unlocks {strike.unlocks_on:%d %b}."))
    elif strike.strikes >= rules.max_strikes - 1 and strike.strikes > 0:
        add(Check("warn", "Last try on this stock",
                  f"Stopped out {strike.strikes} time(s) in a row. Another stop-out locks it for "
                  f"{rules.lockout_days} trading days."))
    else:
        add(Check("good", "No recent stop-outs", f"{rules.max_strikes} tries available"))

    # one position per stock
    if inst.key in working_buy_keys:
        add(Check("crit", "A buy order is already working", f"Cancel or wait for the open {inst.symbol} buy first."))
    if inst.key in held_keys and rules.one_position_per_stock:
        add(Check("crit", f"Already holding {inst.symbol}", "One position per stock at a time."))
    elif inst.key not in working_buy_keys:
        add(Check("good", "Not already held", "One position per stock at a time"))

    # swing guide
    if plan.qty > 0:
        new = Holding(key=inst.key, bucket=bucket.id, value_usd=plan.cost_usd, cost_usd=plan.cost_usd,
                      pl_usd=0.0, atr=plan.atr)
        before, after = swing_share_after(holdings, new, "volatile")
        move = f"Volatile share of daily swing: {before:.0%} → {after:.0%}"
        if after > rules.swing_guide:
            add(Check("warn", "Above the swing guide", f"{move}, over the {rules.swing_guide:.0%} guide."))
        else:
            add(Check("good", "Within the swing guide", move))

    # cash account
    if cash_usd is not None and plan.qty > 0:
        if cash_usd < plan.cost_usd:
            add(Check("crit", "Not enough settled cash",
                      f"{_money(cash_usd)} available, this buy needs {_money(plan.cost_usd)}. "
                      f"In a cash account, sale proceeds are usable only after settlement."))
        else:
            add(Check("good", "Enough settled cash", f"{_money(cash_usd)} available"))
    if (currency_cash is not None and plan.qty > 0 and plan.currency != rules.base_currency
            and currency_cash < plan.cost_local):
        add(Check("warn", f"Not enough {plan.currency} in the account",
                  f"You hold {plan.currency} {currency_cash:,.0f}; this buy needs {plan.currency} "
                  f"{plan.cost_local:,.0f}. A cash account can't borrow currency, so convert first "
                  f"or IBKR may reject the order."))

    # context
    if plan.atr is None:
        add(Check("warn", "No daily-range history",
                  f"Stop set at the {rules.max_stop_pct:.0%} cap and type defaulted to Volatile."))
    if not bucket_confirmed:
        add(Check("info", "Stock type not confirmed yet",
                  f"Suggested as {rules.bucket(suggested_bucket).name} from its daily range. "
                  f"Confirm it on the watchlist."))
    if inst.inverse:
        if (inst.leverage or 1) > 1:
            add(Check("warn", f"Leveraged inverse ETF (−{inst.leverage}×)",
                      "It resets every day, so over several weeks it drifts away from "
                      f"−{inst.leverage}× the index, especially in choppy markets."))
        else:
            add(Check("info", "Inverse ETF", "Gains when the market falls. Resets daily."))
    if not fx_live and plan.currency != rules.base_currency:
        add(Check("info", "Approximate exchange rate",
                  f"Sized with the fallback {plan.currency} rate; the live rate was unavailable."))

    levels = {c.level for c in checks}
    verdict = "blocked" if "crit" in levels else "warning" if "warn" in levels else "allowed"
    return GateResult(verdict=verdict, plan=plan, checks=checks,
                      suggested_bucket=suggested_bucket, bucket_confirmed=bucket_confirmed)
