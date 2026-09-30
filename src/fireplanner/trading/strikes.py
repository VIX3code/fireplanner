"""The two-strike rule: after a stop-out you may re-enter once; two in a row locks the stock.

A *strike* is any exit at a loss worse than ``rules.strike_loss_pct`` (half a
percent by default), so a scratch at the entry price after the stop moved up
is not held against the stock. A winning or breakeven exit resets the count.

When the count reaches ``rules.max_strikes`` the stock is locked for
``rules.lockout_days`` trading days from that exit, and starts again with a
clean count once the lock ends. Trading days are weekdays; exchange holidays
are not modelled, which errs slightly toward unlocking a day early.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

import numpy as np

from .journal import Journal, Trade
from .rules import TradingRules

__all__ = ["StrikeStatus", "strike_status", "strike_board", "add_trading_days"]


def add_trading_days(d: date, n: int) -> date:
    return np.busday_offset(np.datetime64(d, "D"), n, roll="forward").astype(date)


@dataclass
class StrikeStatus:
    key: str
    strikes: int
    tries_left: int
    locked: bool
    #: Last day of the lock (inclusive); None when not locked.
    locked_until: date | None
    last_exit: date | None
    last_result: float | None

    @property
    def unlocks_on(self) -> date | None:
        return add_trading_days(self.locked_until + timedelta(days=1), 0) if self.locked_until else None

    def as_dict(self) -> dict:
        return {
            "key": self.key, "strikes": self.strikes, "tries_left": self.tries_left,
            "locked": self.locked,
            "locked_until": self.locked_until.isoformat() if self.locked_until else None,
            "unlocks_on": self.unlocks_on.isoformat() if self.unlocks_on else None,
            "last_exit": self.last_exit.isoformat() if self.last_exit else None,
            "last_result": self.last_result,
        }


def strike_status(key: str, closed: list[Trade], today: date, rules: TradingRules) -> StrikeStatus:
    """Replay a stock's closed trades, oldest first, to get its count and lock."""
    count, lock_until = 0, None
    last_exit, last_result = None, None
    for t in sorted(closed, key=lambda t: (t.closed_at or "", t.id)):
        d = t.closed_date
        if lock_until is not None and d is not None and d > lock_until:
            count, lock_until = 0, None
        count = count + 1 if t.strike else 0
        if count >= rules.max_strikes and d is not None:
            lock_until = add_trading_days(d, rules.lockout_days)
        last_exit, last_result = d, t.realized_pct
    if lock_until is not None and today > lock_until:
        count, lock_until = 0, None
    locked = lock_until is not None
    return StrikeStatus(
        key=key, strikes=count, tries_left=0 if locked else max(0, rules.max_strikes - count),
        locked=locked, locked_until=lock_until, last_exit=last_exit, last_result=last_result,
    )


def strike_board(journal: Journal, today: date, rules: TradingRules) -> list[StrikeStatus]:
    """Every stock currently carrying a strike or a lock, most urgent first."""
    by_key: dict[str, list[Trade]] = {}
    for t in journal.closed_trades():
        by_key.setdefault(t.key, []).append(t)
    board = [strike_status(k, ts, today, rules) for k, ts in by_key.items()]
    board = [s for s in board if s.strikes > 0 or s.locked]
    return sorted(board, key=lambda s: (not s.locked, -s.strikes, s.key))
