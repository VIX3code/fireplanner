"""Backtesting: daily-bar engine plus performance statistics."""

from .allocation import AllocationBacktest, run_allocation_backtest
from .engine import BacktestConfig, BacktestResult, run_backtest
from .metrics import best_days_analysis, buy_and_hold_stats, compute_stats, drawdown_series

__all__ = [
    "AllocationBacktest",
    "run_allocation_backtest",
    "BacktestConfig",
    "BacktestResult",
    "run_backtest",
    "best_days_analysis",
    "buy_and_hold_stats",
    "compute_stats",
    "drawdown_series",
]
