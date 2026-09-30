"""The two-strike rule: after a stop-out you may re-enter once; two in a row locks the stock.

A *strike* is any exit at a loss worse than ``rules.strike_loss_pct`` (half a
percent by default), so a scratch at the entry price after the stop moved up
is not held against the stock. A winning or breakeven exit resets the count.

When the count reaches ``rules.max_strikes`` the stock is locked. With
``rules.lockout_days`` unset (the default) it stays locked **until you unlock
it** on the dashboard or with ``fireplanner trade unlock``; with a number of
days it unlocks by itself after that many trading days. Either way the count
starts clean afterwards. Trading days are weekdays; exchange holidays are not
modelled.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta

import numpy as np

from .journal import Journal, Trade
from .rules import TradingRules

__all__ = ["StrikeStatus", "strike_status", "strike_for", "strike_board", "add_trading_days"]


def add_trading_days(d: date, n: int) -> date:
    return np.busday_offset(np.datetime64(d, "D"), n, roll="forward").astype(date)


@dataclass
class StrikeStatus:
    key: str
    strikes: int
    tries_left: int
    locked: bool
    #: Last day of a timed lock (inclusive); None when unlocked or locked until you unlock it.
    locked_until: date | None
    last_exit: date | None
    last_result: float | None
    #: True when the lock lasts until you unlock it yourself.
    manual: bool = False

    @property
    def unlocks_on(self) -> date | None:
        return add_trading_days(self.locked_until + timedelta(days=1), 0) if self.locked_until else None

    def as_dict(self) -> dict:
        return {
            "key": self.key, "strikes": self.strikes, "tries_left": self.tries_left,
            "locked": self.locked, "manual": self.manual,
            "locked_until": self.locked_until.isoformat() if self.locked_until else None,
            "unlocks_on": self.unlocks_on.isoformat() if self.unlocks_on else None,
            "last_exit": self.last_exit.isoformat() if self.last_exit else None,
            "last_result": self.last_result,
        }


def strike_status(key: str, closed: list[Trade], today: date, rules: TradingRules,
                  unlocked_at: datetime | None = None) -> StrikeStatus:
    """Replay a stock's closed trades, oldest first, to get its count and lock.

    ``unlocked_at`` is the last time you unlocked the stock by hand; it clears
    any lock that began before it.
    """
    timed = rules.lockout_days is not None
    count, locked, locked_at, lock_until = 0, False, None, None
    last_exit, last_result = None, None

    def released(before: datetime | None, day: date | None) -> bool:
        if unlocked_at is not None and locked_at is not None and unlocked_at >= locked_at and (
                before is None or before > unlocked_at):
            return True
        return timed and lock_until is not None and day is not None and day > lock_until

    for t in sorted(closed, key=lambda t: (t.closed_at or "", t.id)):
        closed_at = datetime.fromisoformat(t.closed_at) if t.closed_at else None
        d = t.closed_date
        if locked and released(closed_at, d):
            count, locked, locked_at, lock_until = 0, False, None, None
        count = count + 1 if t.strike else 0
        if count >= rules.max_strikes and not locked:
            locked, locked_at = True, closed_at
            lock_until = add_trading_days(d, rules.lockout_days) if timed and d else None
        last_exit, last_result = d, t.realized_pct
    if locked and released(None, today):
        count, locked, locked_at, lock_until = 0, False, None, None
    return StrikeStatus(
        key=key, strikes=count, tries_left=0 if locked else max(0, rules.max_strikes - count),
        locked=locked, locked_until=lock_until if locked else None, last_exit=last_exit,
        last_result=last_result, manual=locked and not timed,
    )


def strike_for(journal: Journal, key: str, today: date, rules: TradingRules) -> StrikeStatus:
    return strike_status(key, journal.closed_trades(key), today, rules, journal.last_unlock(key))


def strike_board(journal: Journal, today: date, rules: TradingRules) -> list[StrikeStatus]:
    """Every stock currently carrying a strike or a lock, most urgent first."""
    by_key: dict[str, list[Trade]] = {}
    for t in journal.closed_trades():
        by_key.setdefault(t.key, []).append(t)
    board = [strike_status(k, ts, today, rules, journal.last_unlock(k)) for k, ts in by_key.items()]
    board = [s for s in board if s.strikes > 0 or s.locked]
    return sorted(board, key=lambda s: (not s.locked, -s.strikes, s.key))
