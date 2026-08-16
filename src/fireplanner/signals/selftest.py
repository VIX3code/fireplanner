"""Grade yesterday's signal against today's close, every day.

What is being graded
--------------------
The model does not predict direction — it sets an allocation. To grade it
directionally you first have to say what counts as a directional call, and the
choice matters more than the arithmetic that follows.

The rule here: the **midpoint of the allocation ladder** is neutral. A target
above it is the model leaning risk-on, below it leaning risk-off, and exactly on
it is no call at all. A day is then a hit when the lean and the session's move
agree. This is gradeable on most days, symmetric, and says out loud what
"leaning" means — unlike a rule based on target *changes*, which the cooldown
makes rare enough (about eight a year) that a one-week window would contain
none.

The no-lookahead rule
---------------------
The signal graded against session *t* is the one standing at the close of *t-1*
— the last one you could have acted on. Grading ``target[t]`` against ``ret[t]``
instead would score the model on a target computed from the very close it is
being tested against, which is not a test. That single ``shift(1)`` is the whole
difference between a scorecard and a flattering fiction, so it has its own test.

What the numbers say
--------------------
Over five years this comes out near a coin flip: about 48% against a market that
rises 54% of days, with hit-day and miss-day moves the same size (0.81% vs
0.83%), so there is no hidden asymmetry rescuing it. That is not a defect being
reported — it is the correct result for a model that sizes exposure across
regimes and never claimed to call sessions. Its documented edge is risk-adjusted
(Sharpe 1.18 against 0.98 for buy-and-hold), which a daily direction count
cannot see. The baseline is carried alongside every figure here so the number
cannot be read as skill when it is not.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

__all__ = ["grade_daily", "scorecard", "self_test", "WINDOWS"]

#: Trading sessions per reporting window. Calendar weeks and months vary; these
#: are the session counts a reader means by "the past week" and "the past month".
WINDOWS = [("1 week", 5), ("1 month", 21)]

HIT, MISS, NONE = "hit", "miss", "—"


def _lean(level: pd.Series, neutral: float) -> pd.Series:
    """+1 leaning risk-on, -1 risk-off, 0 no call."""
    return np.sign(level - neutral).fillna(0.0)


def grade_daily(
    allocation: pd.DataFrame,
    ladder_steps,
    neutral: float | None = None,
) -> pd.DataFrame:
    """One row per session: what was standing, what happened, whether it agreed.

    ``allocation`` needs ``close``, ``target`` and ``raw_target``. The returned
    frame is indexed by session date and carries the levels *as they stood at the
    previous close*, which is what makes it a test rather than a description.
    """
    need = {"close", "target", "raw_target"}
    missing = need - set(allocation.columns)
    if missing:
        raise ValueError(f"allocation frame is missing {sorted(missing)}")

    a = allocation.dropna(subset=["target"]).copy()
    if neutral is None:
        steps = list(ladder_steps)
        neutral = (min(steps) + max(steps)) / 2.0

    out = pd.DataFrame(index=a.index)
    out["close"] = a["close"]
    out["ret"] = a["close"].pct_change()

    for name, col in (("committed", "target"), ("raw", "raw_target")):
        # shift(1): the level you were holding when this session's move happened.
        level = a[col].shift(1)
        lean = _lean(level, neutral)
        agree = np.sign(out["ret"]) == lean
        gradeable = lean.ne(0) & out["ret"].notna() & out["ret"].ne(0)
        out[f"{name}_level"] = level
        out[f"{name}_lean"] = lean
        out[f"{name}_verdict"] = np.where(gradeable, np.where(agree, HIT, MISS), NONE)

    # The benchmark that makes the rest legible: never leave the market. It has
    # no view, so anything that cannot beat it has no view worth paying for.
    out["baseline_verdict"] = np.where(
        out["ret"].notna() & out["ret"].ne(0),
        np.where(out["ret"] > 0, HIT, MISS),
        NONE,
    )
    out.attrs["neutral"] = neutral
    return out


def _tally(graded: pd.DataFrame, prefix: str) -> dict:
    verdict = graded[f"{prefix}_verdict"]
    hits = verdict.eq(HIT)
    misses = verdict.eq(MISS)
    n = int(hits.sum() + misses.sum())
    ret = graded["ret"]
    return {
        "graded": n,
        "sessions": int(len(graded)),
        "hits": int(hits.sum()),
        "misses": int(misses.sum()),
        # None rather than 0.0 when nothing was gradeable: a window with no calls
        # in it has no hit rate, and printing 0% would read as "always wrong".
        "hit_rate": (hits.sum() / n) if n else None,
        "avg_hit_move": float(ret[hits].abs().mean()) if hits.any() else None,
        "avg_miss_move": float(ret[misses].abs().mean()) if misses.any() else None,
    }


def scorecard(graded: pd.DataFrame, windows=WINDOWS) -> list[dict]:
    """Summarise the graded frame over each trailing window."""
    rows = []
    for label, sessions in windows:
        win = graded.tail(sessions)
        rows.append({
            "label": label,
            "sessions": int(len(win)),
            "from": win.index[0].date().isoformat() if len(win) else None,
            "to": win.index[-1].date().isoformat() if len(win) else None,
            "committed": _tally(win, "committed"),
            "raw": _tally(win, "raw"),
            "baseline": _tally(win, "baseline"),
            "up_days": int((win["ret"] > 0).sum()),
        })
    return rows


@dataclass
class SelfTest:
    """Everything the page needs to show how the signal has been doing."""

    daily: pd.DataFrame
    windows: list = field(default_factory=list)
    neutral: float = 0.7
    #: Long-run context, so a short window is never read in isolation.
    lifetime: dict = field(default_factory=dict)

    @property
    def latest(self) -> dict | None:
        """The most recent graded session — "did yesterday's call work"."""
        if self.daily.empty:
            return None
        row = self.daily.iloc[-1]
        return {
            "date": self.daily.index[-1].date().isoformat(),
            "ret": None if pd.isna(row["ret"]) else float(row["ret"]),
            "close": float(row["close"]),
            "committed_level": None if pd.isna(row["committed_level"]) else float(row["committed_level"]),
            "committed_verdict": str(row["committed_verdict"]),
            "raw_level": None if pd.isna(row["raw_level"]) else float(row["raw_level"]),
            "raw_verdict": str(row["raw_verdict"]),
        }


def self_test(allocation: pd.DataFrame, ladder_steps, windows=WINDOWS) -> SelfTest:
    """Grade the signal daily and summarise it over the reporting windows."""
    daily = grade_daily(allocation, ladder_steps)
    return SelfTest(
        daily=daily,
        windows=scorecard(daily, windows),
        neutral=daily.attrs["neutral"],
        lifetime={
            "sessions": int(len(daily)),
            "from": daily.index[0].date().isoformat() if len(daily) else None,
            "committed": _tally(daily, "committed"),
            "raw": _tally(daily, "raw"),
            "baseline": _tally(daily, "baseline"),
        },
    )
