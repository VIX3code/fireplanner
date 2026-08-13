"""Indicator correctness, checked against independent brute-force computation.

These tests are the reason the rest of the stack can be trusted: if RSI is wrong
by a rounding convention, every score and every backtest downstream is wrong in a
way no amount of pretty charting will reveal.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from fireplanner import indicators as ind
from fireplanner.data import SnapshotProvider


@pytest.fixture(scope="module")
def spy() -> pd.DataFrame:
    return SnapshotProvider("data/snapshots").history("SPY")


@pytest.fixture(scope="module")
def enriched(spy) -> pd.DataFrame:
    return ind.enrich(spy)


def test_snapshot_loads_with_expected_shape(spy):
    assert len(spy) > 1000
    assert list(spy.columns) == ["open", "high", "low", "close", "volume"]
    assert spy.index.is_monotonic_increasing
    assert not spy.index.has_duplicates
    # high/low must actually bound open/close
    assert (spy["high"] >= spy[["open", "close"]].max(axis=1) - 1e-6).all()
    assert (spy["low"] <= spy[["open", "close"]].min(axis=1) + 1e-6).all()


def test_sma_matches_plain_mean(spy):
    got = ind.sma(spy["close"], 50).iloc[-1]
    assert got == pytest.approx(spy["close"].tail(50).mean())


def test_rsi_matches_wilder_recursion(spy):
    """Recompute RSI with an explicit loop — no pandas smoothing helpers."""
    close = spy["close"]
    delta = close.diff()
    gain, loss = delta.clip(lower=0), (-delta).clip(lower=0)

    avg_gain = gain.iloc[1:15].mean()
    avg_loss = loss.iloc[1:15].mean()
    for i in range(15, len(close)):
        avg_gain = (avg_gain * 13 + gain.iloc[i]) / 14
        avg_loss = (avg_loss * 13 + loss.iloc[i]) / 14

    expected = 100 - 100 / (1 + avg_gain / avg_loss)
    assert ind.rsi(close, 14).iloc[-1] == pytest.approx(expected, abs=1e-9)


def test_atr_matches_wilder_recursion(spy):
    tr = ind.true_range(spy)
    acc = tr.iloc[1:15].mean()
    for i in range(15, len(tr)):
        acc = (acc * 13 + tr.iloc[i]) / 14
    assert ind.atr(spy, 14).iloc[-1] == pytest.approx(acc, abs=1e-9)


def test_true_range_covers_gaps():
    df = pd.DataFrame(
        {"open": [10, 20], "high": [11, 21], "low": [9, 19], "close": [10, 20], "volume": [1, 1]},
        index=pd.to_datetime(["2024-01-02", "2024-01-03"]),
    )
    # Gap from 10 up to a 19-21 range: TR is 21-10 = 11, not the 2-point bar range.
    assert ind.true_range(df).iloc[1] == pytest.approx(11.0)


def test_bollinger_bands_and_pct_b(spy):
    bb = ind.bollinger(spy["close"], 20, 2.0)
    window = spy["close"].tail(20)
    mean, sd = window.mean(), window.std(ddof=0)
    assert bb["upper"].iloc[-1] == pytest.approx(mean + 2 * sd)
    assert bb["lower"].iloc[-1] == pytest.approx(mean - 2 * sd)
    # %B is 0 at the lower band and 1 at the upper by construction.
    span = bb["upper"].iloc[-1] - bb["lower"].iloc[-1]
    expected = (spy["close"].iloc[-1] - bb["lower"].iloc[-1]) / span
    assert bb["pct_b"].iloc[-1] == pytest.approx(expected)


def test_rsi_bounds_and_adx_non_negative(enriched):
    rsi = enriched["rsi14"].dropna()
    assert rsi.between(0, 100).all()
    adx = enriched["adx"].dropna()
    assert (adx >= 0).all() and (adx <= 100).all()


def test_donchian_excludes_current_bar(spy):
    """The channel must be decidable on the bar that breaks it."""
    dc = ind.donchian(spy, 20)
    i = -1
    prior_high = spy["high"].iloc[i - 20 : i].max()
    assert dc["upper"].iloc[i] == pytest.approx(prior_high)


def test_supertrend_direction_is_signed(enriched):
    d = enriched["supertrend_dir"].dropna()
    assert set(np.unique(d)) <= {-1.0, 1.0}


def test_indicators_do_not_look_ahead(spy):
    """Truncating the series must not change any already-computed value.

    This is the single most important property in the library. If it fails, every
    backtest is fiction.
    """
    full = ind.enrich(spy)
    truncated = ind.enrich(spy.iloc[:-30])

    cols = ["sma50", "sma200", "rsi14", "atr14", "adx", "macd_hist", "bb_pct_b", "supertrend_dir", "obv"]
    shared = truncated.index[-120:]
    for col in cols:
        a = full.loc[shared, col]
        b = truncated.loc[shared, col]
        both = a.notna() & b.notna()
        assert both.sum() > 50, f"{col}: too few overlapping values to judge"
        assert np.allclose(a[both], b[both], rtol=1e-9, atol=1e-9), f"{col} changed when future bars were added"


def test_warmup_is_nan_not_backfilled(enriched):
    assert enriched["sma200"].iloc[:199].isna().all()
    assert enriched["sma50"].iloc[:49].isna().all()


def test_rolling_percentile_is_bounded(enriched):
    p = enriched["natr_pctile"].dropna()
    assert p.between(0, 1).all()


def test_drawdown_is_non_positive(enriched):
    assert (enriched["drawdown"].dropna() <= 1e-9).all()
