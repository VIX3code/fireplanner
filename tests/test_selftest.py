"""The daily self-test: does yesterday's signal survive contact with today's close.

The test that matters here is the lookahead one. A scorecard is only worth
having if it cannot cheat, and the cheat is a single missing ``shift(1)`` — grade
``target[t]`` against ``ret[t]`` and the model appears to call the market
perfectly, because that target was computed *from* that close. The fixture below
is built so a lookahead implementation scores 100% and the correct one scores 0%,
which makes the two impossible to confuse.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from fireplanner.dashboard import build_payload
from fireplanner.dashboard.build import add_decision
from fireplanner.data import SnapshotProvider
from fireplanner.signals.selftest import HIT, MISS, NONE, grade_daily, scorecard, self_test

LADDER = [0.4, 0.55, 0.7, 0.85, 1.0]        # midpoint 0.70


def frame(closes, targets, raws=None):
    idx = pd.bdate_range("2026-01-05", periods=len(closes))
    return pd.DataFrame(
        {"close": closes, "target": targets, "raw_target": raws if raws is not None else targets},
        index=idx,
    )


# ---------------------------------------------------------------- no lookahead

def test_it_grades_the_signal_you_could_have_acted_on():
    """The single most important property, and the easiest to get wrong.

    Every target here is set to match the *same* session's move: risk-on on up
    days, risk-off on down days. An implementation that grades ``target[t]``
    against ``ret[t]`` therefore scores a perfect 100%. The honest one grades the
    level standing at the previous close, which in this series is always the
    opposite of what the session did — so it must score exactly 0%.
    """
    closes = [100.0, 110.0, 100.0, 110.0, 100.0]
    #          -      up     down    up     down
    targets = [0.70, 1.00, 0.40, 1.00, 0.40]   # each matches its own day's move

    graded = grade_daily(frame(closes, targets), LADDER)
    card = scorecard(graded, [("all", 5)])[0]

    assert card["committed"]["hit_rate"] == 0.0, (
        "scored above zero — the grader is reading the target computed from the "
        "same close it is being tested against"
    )
    assert card["committed"]["graded"] == 3
    assert card["committed"]["misses"] == 3


def test_the_first_session_cannot_be_graded():
    """There is no previous close to have acted on, and no return to grade."""
    graded = grade_daily(frame([100.0, 101.0], [1.0, 1.0]), LADDER)
    assert graded["committed_verdict"].iloc[0] == NONE
    assert pd.isna(graded["ret"].iloc[0])


# ---------------------------------------------------------------- the rule

def test_above_the_midpoint_is_a_risk_on_call():
    graded = grade_daily(frame([100.0, 101.0], [0.85, 0.85]), LADDER)
    assert graded["committed_lean"].iloc[1] == 1.0
    assert graded["committed_verdict"].iloc[1] == HIT      # leaned in, market rose


def test_below_the_midpoint_is_a_risk_off_call():
    graded = grade_daily(frame([100.0, 99.0], [0.40, 0.40]), LADDER)
    assert graded["committed_lean"].iloc[1] == -1.0
    assert graded["committed_verdict"].iloc[1] == HIT      # leaned out, market fell


def test_leaning_out_of_a_rising_market_is_a_miss():
    graded = grade_daily(frame([100.0, 101.0], [0.40, 0.40]), LADDER)
    assert graded["committed_verdict"].iloc[1] == MISS


def test_sitting_exactly_on_the_midpoint_is_not_a_call():
    """No view is not a wrong view, and must not be counted as one."""
    graded = grade_daily(frame([100.0, 101.0], [0.70, 0.70]), LADDER)
    assert graded["committed_lean"].iloc[1] == 0.0
    assert graded["committed_verdict"].iloc[1] == NONE
    card = scorecard(graded, [("all", 2)])[0]
    assert card["committed"]["graded"] == 0
    assert card["committed"]["hit_rate"] is None


def test_an_unchanged_close_is_not_a_call():
    graded = grade_daily(frame([100.0, 100.0], [1.0, 1.0]), LADDER)
    assert graded["committed_verdict"].iloc[1] == NONE


def test_neutral_defaults_to_the_ladder_midpoint():
    graded = grade_daily(frame([100.0, 101.0], [0.7, 0.7]), LADDER)
    assert graded.attrs["neutral"] == pytest.approx(0.7)
    # ...and a different ladder moves it
    graded = grade_daily(frame([100.0, 101.0], [0.7, 0.7]), [0.0, 0.5, 1.0])
    assert graded.attrs["neutral"] == pytest.approx(0.5)


def test_an_explicit_neutral_overrides_the_ladder():
    graded = grade_daily(frame([100.0, 101.0], [0.60, 0.60]), LADDER, neutral=0.50)
    assert graded["committed_lean"].iloc[1] == 1.0        # 0.60 is above 0.50


# ---------------------------------------------------------------- both signals

def test_the_two_signals_are_graded_independently():
    """The point of showing both: what the damping costs, or saves."""
    graded = grade_daily(
        frame(closes=[100.0, 101.0], targets=[0.40, 0.40], raws=[1.00, 1.00]), LADDER
    )
    assert graded["committed_verdict"].iloc[1] == MISS     # committed stayed out
    assert graded["raw_verdict"].iloc[1] == HIT            # raw signal was in


# ---------------------------------------------------------------- baseline

def test_the_baseline_never_leaves_the_market():
    graded = grade_daily(frame([100.0, 101.0, 100.0], [0.4, 0.4, 0.4]), LADDER)
    assert list(graded["baseline_verdict"]) == [NONE, HIT, MISS]


def test_the_baseline_is_reported_in_every_window():
    """A hit rate with nothing to compare it against invites being read as skill."""
    p = add_decision(build_payload(SnapshotProvider("data/snapshots"), watchlist=["SPY"]),
                     reference_date=pd.Timestamp("2026-08-13"))
    st = self_test(p.allocation, p.ladder_steps)
    for window in st.windows:
        assert "baseline" in window and window["baseline"]["graded"] > 0


# ---------------------------------------------------------------- windows

def test_windows_cover_the_requested_number_of_sessions():
    p = add_decision(build_payload(SnapshotProvider("data/snapshots"), watchlist=["SPY"]),
                     reference_date=pd.Timestamp("2026-08-13"))
    st = self_test(p.allocation, p.ladder_steps)
    by_label = {w["label"]: w for w in st.windows}
    assert by_label["1 week"]["sessions"] == 5
    assert by_label["1 month"]["sessions"] == 21
    # the week is the tail of the month
    assert by_label["1 week"]["to"] == by_label["1 month"]["to"]


def test_a_short_history_does_not_crash_the_windows():
    graded = grade_daily(frame([100.0, 101.0, 102.0], [0.4, 0.4, 0.4]), LADDER)
    card = scorecard(graded, [("1 month", 21)])[0]
    assert card["sessions"] == 3          # not 21 — it reports what it has


# ---------------------------------------------------------------- read-only

def test_grading_does_not_touch_the_allocation_frame():
    """This is a scorecard. It must not be able to change the thing it grades."""
    p = add_decision(build_payload(SnapshotProvider("data/snapshots"), watchlist=["SPY"]),
                     reference_date=pd.Timestamp("2026-08-13"))
    before = p.allocation.copy(deep=True)
    self_test(p.allocation, p.ladder_steps)
    pd.testing.assert_frame_equal(p.allocation, before)


def test_a_frame_without_the_needed_columns_says_so():
    bad = pd.DataFrame({"close": [1.0, 2.0]}, index=pd.bdate_range("2026-01-05", periods=2))
    with pytest.raises(ValueError, match="raw_target"):
        grade_daily(bad, LADDER)


# ---------------------------------------------------------------- real data

def test_the_lifetime_result_is_near_a_coin_flip():
    """Documents the finding rather than asserting a target.

    If this ever drifts far from chance it is worth knowing about — in either
    direction. A directional edge appearing in a model that never had one is as
    much a reason to go looking as one disappearing.
    """
    p = add_decision(build_payload(SnapshotProvider("data/snapshots"), watchlist=["SPY"]),
                     reference_date=pd.Timestamp("2026-08-13"))
    st = self_test(p.allocation, p.ladder_steps)
    rate = st.lifetime["committed"]["hit_rate"]
    assert st.lifetime["committed"]["graded"] > 500
    assert 0.40 < rate < 0.60, f"daily direction hit rate moved to {rate:.1%}"
    # and the market's own up-day rate is the thing it has to be read against
    assert st.lifetime["baseline"]["hit_rate"] > rate


def test_hit_and_miss_days_are_similar_in_size():
    """The check that stops a near-50% hit rate hiding a real asymmetry.

    A model could be wrong often and still valuable if its hits were large and
    its misses small. Measured over five years they are the same size, so the
    coin-flip reading stands.
    """
    p = add_decision(build_payload(SnapshotProvider("data/snapshots"), watchlist=["SPY"]),
                     reference_date=pd.Timestamp("2026-08-13"))
    lt = self_test(p.allocation, p.ladder_steps).lifetime["committed"]
    assert lt["avg_hit_move"] == pytest.approx(lt["avg_miss_move"], abs=0.002)
