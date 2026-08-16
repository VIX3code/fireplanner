"""Signals: score a name, then decide how much of the index to hold.

Two layers, deliberately separate:

``model``
    Scores an enriched frame 0-100 and emits a per-name action. Answers
    *is this worth owning*.
``allocation``
    Turns that score, plus the market regime, into a target allocation with
    hysteresis. Answers *how much, today* — the question that costs money.
``selftest``
    Grades what those two said yesterday against what the market actually did.
    Answers *has any of this been working* — read-only, and it never feeds back
    into the model.
"""

from .allocation import (
    AllocationPolicy,
    AllocationState,
    latest_decision,
    target_allocation,
    trigger_levels,
)
from .model import (
    ACTIONS,
    ScoreWeights,
    Signal,
    SignalConfig,
    latest_signal,
    score_frame,
)
from .selftest import WINDOWS, SelfTest, grade_daily, scorecard, self_test

__all__ = [
    # scoring
    "ACTIONS",
    "ScoreWeights",
    "Signal",
    "SignalConfig",
    "latest_signal",
    "score_frame",
    # allocation
    "AllocationPolicy",
    "AllocationState",
    "latest_decision",
    "target_allocation",
    "trigger_levels",
    # self-test
    "WINDOWS",
    "SelfTest",
    "grade_daily",
    "scorecard",
    "self_test",
]
