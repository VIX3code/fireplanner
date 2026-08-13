"""Backtesting: daily-bar engine plus performance statistics."""

from .engine import BacktestConfig, BacktestResult, run_backtest
from .metrics import buy_and_hold_stats, compute_stats, drawdown_series

__all__ = [
    "BacktestConfig",
    "BacktestResult",
    "run_backtest",
    "buy_and_hold_stats",
    "compute_stats",
    "drawdown_series",
]
