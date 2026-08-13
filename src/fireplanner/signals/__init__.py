"""Signal generation: score an enriched frame, emit an action."""

from .model import (
    ACTIONS,
    ScoreWeights,
    Signal,
    SignalConfig,
    latest_signal,
    score_frame,
)

__all__ = ["ACTIONS", "ScoreWeights", "Signal", "SignalConfig", "latest_signal", "score_frame"]

from .allocation import (
    AllocationPolicy,
    AllocationState,
    latest_decision,
    target_allocation,
    trigger_levels,
)

__all__ += [
    "AllocationPolicy",
    "AllocationState",
    "latest_decision",
    "target_allocation",
    "trigger_levels",
]
