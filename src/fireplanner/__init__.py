"""FirePlanner — a swing/position trading stack for IBKR.

Quick start
-----------
::

    from fireplanner.data import get_provider
    from fireplanner import indicators as ind
    from fireplanner.signals import score_frame, latest_signal

    p = get_provider("snapshot")          # or "gateway" once TWS is running
    bars = p.history("SPY", lookback_days=1260)
    enriched = ind.enrich(bars)
    signal = latest_signal("SPY", score_frame(enriched))
"""

__version__ = "0.1.0"

from . import indicators  # noqa: F401

__all__ = ["indicators", "__version__"]
