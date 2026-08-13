"""Data access: one contract, three IBKR transports, plus an on-disk cache."""

from __future__ import annotations

from .base import (
    Bar,
    BarProvider,
    Contract,
    OHLCV_COLUMNS,
    drop_incomplete_last_bar,
    normalize_bars,
)
from .cache import CachedProvider
from .providers import GatewayProvider, SnapshotProvider, WebApiProvider, get_provider

__all__ = [
    "Bar",
    "BarProvider",
    "Contract",
    "OHLCV_COLUMNS",
    "drop_incomplete_last_bar",
    "normalize_bars",
    "CachedProvider",
    "GatewayProvider",
    "SnapshotProvider",
    "WebApiProvider",
    "get_provider",
]
