"""Circuit breakers: when to stop opening new trades.

Four things pause new buys. None of them touches an open position; stops and
targets keep working, and the guardian keeps protecting everything.

* **Daily loss limit.** Realized losses today beyond ``daily_loss_limit_usd``
  pause buys for the rest of the day.
* **Weekly loss limit.** The same over the ISO week.
* **Losses in a row.** ``max_consecutive_losses`` stop-outs in a row, across all
  stocks, pause buys until you press Resume. A losing streak that long is
  usually the market, not the stocks.
* **Your hand.** Pause, or the kill switch, which also cancels every buy order
  still working. Both last until you press Resume.

``max_entries_per_day`` caps dashboard buys, which also stops a runaway bug.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta

from .journal import Journal
from .rules import TradingRules

__all__ = ["BreakerState", "breaker_state"]


@dataclass
class BreakerState:
    paused: bool
    reasons: list[str] = field(default_factory=list)
    manual: bool = False
    killed: bool = False
    today_pl_usd: float = 0.0
    week_pl_usd: float = 0.0
    losses_in_a_row: int = 0
    entries_today: int = 0

    def as_dict(self) -> dict:
        return asdict(self)


def breaker_state(journal: Journal, rules: TradingRules, now: datetime) -> BreakerState:
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    week_start = day_start - timedelta(days=day_start.weekday())
    week = journal.closed_since(week_start)
    today_pl = sum(t.realized_usd or 0.0 for t in week if datetime.fromisoformat(t.closed_at) >= day_start)
    week_pl = sum(t.realized_usd or 0.0 for t in week)

    last = journal.last_breaker_event()
    resumed_at = datetime.fromisoformat(last["at"]) if last and last["kind"] == "resume" else None
    manual = bool(last and last["kind"] in ("pause", "kill"))
    killed = bool(last and last["kind"] == "kill")

    # Losses in a row, replayed since the last resume: once the streak hits the
    # limit, buys stay paused until you resume, even if a later exit is a win.
    streak, tripped = 0, False
    for t in journal.closed_trades():
        if resumed_at is not None and datetime.fromisoformat(t.closed_at) <= resumed_at:
            continue
        streak = streak + 1 if t.strike else 0
        if streak >= rules.max_consecutive_losses:
            tripped = True

    entries = sum(1 for e in journal.breaker_events_since(day_start) if e["kind"] == "entry")

    reasons = []
    if manual:
        reasons.append("Kill switch pressed: buys paused until you resume." if killed
                       else "Paused by you until you resume.")
    if -today_pl >= rules.daily_loss_limit_usd:
        reasons.append(f"Daily loss limit: ${-today_pl:,.0f} lost today (limit ${rules.daily_loss_limit_usd:,.0f}). "
                       f"Buys resume tomorrow.")
    if -week_pl >= rules.weekly_loss_limit_usd:
        reasons.append(f"Weekly loss limit: ${-week_pl:,.0f} lost this week (limit ${rules.weekly_loss_limit_usd:,.0f}). "
                       f"Buys resume next week.")
    if tripped:
        reasons.append(f"{rules.max_consecutive_losses} stop-outs in a row. Buys paused until you resume.")
    return BreakerState(paused=bool(reasons), reasons=reasons, manual=manual, killed=killed,
                        today_pl_usd=today_pl, week_pl_usd=week_pl, losses_in_a_row=streak,
                        entries_today=entries)
