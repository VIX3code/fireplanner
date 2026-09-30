"""The journal, graded: what each stock type and each setup actually pays.

Results are in **R**, units of the risk taken on the trade: a full stop-out is
-1R, a trade that made twice its risk is +2R. With a fixed loss per trade, R
and dollars tell the same story, but R keeps working if the fixed loss
changes, and it makes the question plain: *does this kind of trade make more
when it wins than it loses when it doesn't?*

``expectancy`` is the average R per trade. Above zero is an edge; with 30 or
more trades in a group it starts to mean something.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from .journal import Trade

__all__ = ["trade_r", "group_stats", "journal_stats"]


def trade_r(t: Trade) -> float | None:
    if t.r_multiple is not None:
        return t.r_multiple
    if t.realized_pct is None or t.entry <= 0:
        return None
    risk = 1 - t.initial_stop / t.entry
    return t.realized_pct / risk if risk > 0 else None


def group_stats(trades: list[Trade]) -> dict:
    rs = [r for r in (trade_r(t) for t in trades) if r is not None]
    wins = [r for r in rs if r > 0]
    losses = [r for r in rs if r <= 0]
    return {
        "trades": len(trades),
        "wins": len(wins),
        "win_rate": len(wins) / len(rs) if rs else None,
        "avg_win_r": sum(wins) / len(wins) if wins else None,
        "avg_loss_r": sum(losses) / len(losses) if losses else None,
        "expectancy_r": sum(rs) / len(rs) if rs else None,
        "total_usd": sum(t.realized_usd or 0.0 for t in trades),
        "avg_days": sum(t.days_held for t in trades) / len(trades) if trades else None,
        "best_r": max(rs) if rs else None,
        "worst_r": min(rs) if rs else None,
    }


def journal_stats(closed: list[Trade], now: datetime) -> dict:
    by_bucket: dict[str, list[Trade]] = {}
    by_setup: dict[str, list[Trade]] = {}
    for t in closed:
        by_bucket.setdefault(t.bucket, []).append(t)
        by_setup.setdefault(t.setup or "Untagged", []).append(t)
    week = [t for t in closed if t.closed_at and datetime.fromisoformat(t.closed_at) >= now - timedelta(days=7)]
    month = [t for t in closed if t.closed_at and datetime.fromisoformat(t.closed_at) >= now - timedelta(days=30)]
    return {
        "all": group_stats(closed),
        "week": group_stats(week),
        "month": group_stats(month),
        "by_bucket": {k: group_stats(v) for k, v in by_bucket.items()},
        "by_setup": {k: group_stats(v) for k, v in sorted(by_setup.items())},
    }
