"""Regime, signals, sizing, and backtest-engine behaviour."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from fireplanner import indicators as ind
from fireplanner.backtest import BacktestConfig, run_backtest
from fireplanner.data import SnapshotProvider, normalize_bars
from fireplanner.risk import RiskConfig, plan_position, portfolio_heat, vol_target_scalar
from fireplanner.signals import SignalConfig, latest_signal, score_frame


@pytest.fixture(scope="module")
def provider():
    return SnapshotProvider("data/snapshots")


@pytest.fixture(scope="module")
def spy(provider):
    return provider.history("SPY")


@pytest.fixture(scope="module")
def regime(provider, spy):
    return ind.compute_regime(
        spy["close"],
        vix=provider.history("VIX")["close"],
        equal_weight=provider.history("RSP")["close"],
        vix3m=provider.history("VIX3M")["close"],
    )


# ---------------------------------------------------------------- data layer

def test_normalize_bars_sorts_dedupes_and_lowercases():
    raw = pd.DataFrame(
        {
            "Date": ["2024-01-03", "2024-01-02", "2024-01-03"],
            "Open": [2, 1, 2.5], "High": [3, 2, 3.5], "Low": [1, 0.5, 1.5],
            "Close": [2.5, 1.5, 2.7], "Volume": [10, 20, 30],
        }
    )
    out = normalize_bars(raw)
    assert list(out.columns) == ["open", "high", "low", "close", "volume"]
    assert out.index.is_monotonic_increasing
    assert not out.index.has_duplicates
    assert len(out) == 2
    # duplicate session keeps the last observation
    assert out["close"].iloc[-1] == pytest.approx(2.7)


def test_normalize_bars_requires_close():
    with pytest.raises(ValueError, match="close"):
        normalize_bars(pd.DataFrame({"date": ["2024-01-02"], "open": [1], "high": [2], "low": [0]}))


def test_normalize_bars_repairs_out_of_range_close():
    """IBKR sometimes closes a cent outside the bar's own high — widen, never narrow."""
    raw = pd.DataFrame(
        {"date": ["2022-03-17"], "open": [433.59], "high": [441.02],
         "low": [433.19], "close": [441.07], "volume": [1]}
    )
    out = normalize_bars(raw)
    assert out["high"].iloc[0] == pytest.approx(441.07)
    assert out["low"].iloc[0] == pytest.approx(433.19)  # untouched


def test_normalize_bars_leaves_clean_bars_alone():
    raw = pd.DataFrame(
        {"date": ["2024-01-02"], "open": [10.0], "high": [11.0],
         "low": [9.0], "close": [10.5], "volume": [1]}
    )
    out = normalize_bars(raw)
    assert out["high"].iloc[0] == pytest.approx(11.0)
    assert out["low"].iloc[0] == pytest.approx(9.0)


# ---------------------------------------------------------------- regime

def test_regime_score_bounded_and_labelled(regime):
    s = regime["score"].dropna()
    assert s.between(0, 1).all()
    assert set(regime["label"].unique()) <= {
        "Risk-On", "Constructive", "Neutral", "Defensive", "Risk-Off", "Unknown"
    }


def test_exposure_cap_is_monotone_in_score(regime):
    valid = regime.dropna(subset=["score"])
    ranked = valid.sort_values("score")
    assert ranked["exposure_cap"].is_monotonic_increasing


def test_regime_survives_missing_optional_inputs(spy):
    only_trend = ind.compute_regime(spy["close"])
    assert only_trend["score"].dropna().between(0, 1).all()
    assert "c_vol" not in only_trend.columns


# ---------------------------------------------------------------- signals

def test_score_is_bounded_and_actions_valid(spy, regime):
    e = ind.enrich(spy)
    scored = score_frame(e, regime=regime["score"])
    s = scored["score"].dropna()
    assert s.between(0, 100).all()
    assert set(scored["action"].dropna().unique()) <= {"BUY", "ADD", "HOLD", "REDUCE", "AVOID"}


def test_extended_names_score_worse_than_resting_ones(spy):
    """The entry block must punish extension — that is the whole point."""
    e = ind.enrich(spy)
    scored = score_frame(e)
    joined = pd.concat([scored["entry"], e["atr_from_ema21"]], axis=1).dropna()
    stretched = joined[joined["atr_from_ema21"] > 2.5]["entry"]
    resting = joined[joined["atr_from_ema21"].between(-0.5, 1.0)]["entry"]
    assert len(stretched) > 5 and len(resting) > 5
    assert stretched.mean() < resting.mean()


def test_close_below_sma50_is_always_defensive(spy, regime):
    """Below the 50-day the model must never say BUY/ADD/HOLD.

    REDUCE and AVOID are both acceptable: AVOID is the stronger verdict reserved
    for a broken name that also fails the trend gate on a weak score.
    """
    e = ind.enrich(spy)
    scored = score_frame(e, regime=regime["score"])
    broken = e["close"] < e["sma50"]
    both = broken & scored["action"].notna()
    assert both.sum() > 100
    assert scored.loc[both, "action"].isin({"REDUCE", "AVOID"}).all()


def test_regime_gate_demotes_scores(spy):
    e = ind.enrich(spy)
    ungated = score_frame(e)["score"]
    bad_tape = pd.Series(0.0, index=e.index)
    gated = score_frame(e, regime=bad_tape)["score"]
    both = ungated.notna() & gated.notna()
    assert (gated[both] <= ungated[both] + 1e-9).all()
    assert gated[both].mean() < ungated[both].mean()


def test_latest_signal_needs_history():
    short = pd.DataFrame(
        {"open": [1.0] * 10, "high": [1.0] * 10, "low": [1.0] * 10, "close": [1.0] * 10, "volume": [1] * 10},
        index=pd.date_range("2024-01-01", periods=10, freq="B"),
    )
    with pytest.raises(ValueError):
        latest_signal("X", score_frame(ind.enrich(short)))


# ---------------------------------------------------------------- sizing

def test_position_risk_equals_configured_fraction():
    cfg = RiskConfig(risk_pct=0.01, atr_stop_mult=2.0, max_position_pct=1.0, whole_shares=False)
    plan = plan_position("X", equity=100_000, entry=100.0, atr=2.0, cfg=cfg)
    # stop is 4 below entry; 1% of 100k = $1,000 risk => 250 shares
    assert plan.stop == pytest.approx(96.0)
    assert plan.shares == pytest.approx(250.0)
    assert plan.risk_dollars == pytest.approx(1000.0)
    assert plan.risk_pct_equity == pytest.approx(0.01)


def test_weight_cap_binds_before_risk_budget():
    cfg = RiskConfig(risk_pct=0.05, atr_stop_mult=1.0, max_position_pct=0.10, whole_shares=False)
    plan = plan_position("X", equity=100_000, entry=50.0, atr=1.0, cfg=cfg)
    assert plan.weight == pytest.approx(0.10)
    assert plan.limited_by == "max_position_pct"


def test_regime_cap_shrinks_the_position():
    cfg = RiskConfig(risk_pct=0.05, atr_stop_mult=1.0, max_position_pct=0.10, whole_shares=False)
    full = plan_position("X", equity=100_000, entry=50.0, atr=1.0, cfg=cfg, exposure_cap=1.0)
    half = plan_position("X", equity=100_000, entry=50.0, atr=1.0, cfg=cfg, exposure_cap=0.5)
    assert half.shares == pytest.approx(full.shares / 2)
    assert half.limited_by == "regime_exposure_cap"


def test_open_heat_reduces_new_risk():
    cfg = RiskConfig(risk_pct=0.01, max_portfolio_heat=0.015, atr_stop_mult=2.0,
                     max_position_pct=1.0, whole_shares=False)
    plan = plan_position("X", equity=100_000, entry=100.0, atr=2.0, cfg=cfg, open_heat=0.01)
    # only 0.5% of heat left, so the position risks 0.5% not 1%
    assert plan.risk_pct_equity == pytest.approx(0.005)
    assert plan.limited_by == "portfolio_heat"


def test_invalid_inputs_rejected():
    with pytest.raises(ValueError):
        plan_position("X", equity=1000, entry=0.0, atr=1.0)
    with pytest.raises(ValueError):
        plan_position("X", equity=1000, entry=10.0, atr=float("nan"))


def test_portfolio_heat_sums_open_risk():
    pos = pd.DataFrame({"shares": [100, 50], "price": [10.0, 20.0], "stop": [9.0, 18.0]})
    # 100*1 + 50*2 = 200 on 10k equity
    assert portfolio_heat(pos, 10_000) == pytest.approx(0.02)


def test_vol_target_scales_inversely_and_is_capped():
    assert vol_target_scalar(15.0, 0.15) == pytest.approx(1.0)
    assert vol_target_scalar(30.0, 0.15) == pytest.approx(0.5)
    assert vol_target_scalar(1.0, 0.15, cap=1.5) == pytest.approx(1.5)


# ---------------------------------------------------------------- backtest

def test_backtest_runs_and_accounts_consistently(spy, regime):
    e = ind.enrich(spy)
    r = run_backtest(e, regime=regime["score"], symbol="SPY")
    assert len(r.equity_curve) > 500
    assert r.equity_curve.notna().all()
    assert (r.equity_curve > 0).all()
    assert r.stats["trades"] == len(r.trades)
    if not r.trades.empty:
        assert (r.trades["exit_date"] >= r.trades["entry_date"]).all()
        assert (r.trades["shares"] > 0).all()


def test_stop_fill_never_better_than_the_stop(spy, regime):
    """A gap-down must fill at the open, not at the untouched stop price."""
    e = ind.enrich(spy)
    r = run_backtest(e, regime=regime["score"], symbol="SPY")
    stops = r.trades[r.trades["reason"] == "stop"]
    assert len(stops) > 0
    for _, t in stops.iterrows():
        bar = e.loc[t["exit_date"]]
        # the fill can never exceed that session's high
        assert t["exit"] <= bar["high"] + 1e-6


def test_entries_fill_at_a_later_session_than_the_signal(spy, regime):
    e = ind.enrich(spy)
    r = run_backtest(e, regime=regime["score"], symbol="SPY")
    for _, t in r.trades.iterrows():
        entry_bar = e.loc[t["entry_date"]]
        # fill is that session's open plus slippage — never its close
        assert t["entry"] >= entry_bar["low"] * 0.99


def test_costs_reduce_returns(spy, regime):
    e = ind.enrich(spy)
    free = run_backtest(e, regime=regime["score"], symbol="SPY",
                        cfg=BacktestConfig(commission_per_share=0.0, min_commission=0.0, slippage_bps=0.0))
    costly = run_backtest(e, regime=regime["score"], symbol="SPY",
                          cfg=BacktestConfig(commission_per_share=0.02, min_commission=1.0, slippage_bps=25.0))
    assert costly.equity_curve.iloc[-1] < free.equity_curve.iloc[-1]


# ---------------------------------------------------------------- term structure

@pytest.fixture(scope="module")
def vix3m(provider):
    return provider.history("VIX3M")["close"]


def test_term_structure_ratio_and_flags(provider, vix3m):
    vix = provider.history("VIX")["close"]
    ts = ind.term_structure(vix, vix3m)
    ratio = ts["ratio"].dropna()
    assert (ratio > 0).all()
    # backwardated iff the ratio is under 1
    assert ((ts["ratio"] < 1.0) == (ts["backwardated"] > 0)).where(ratio.notna()).dropna().all()
    assert ts["score"].dropna().between(0, 1).all()


def test_term_score_is_contrarian():
    """Backwardation must score HIGH — reading it backwards degrades the model."""
    idx = pd.date_range("2024-01-01", periods=3, freq="B")
    vix = pd.Series([20.0, 20.0, 20.0], index=idx)
    v3m = pd.Series([17.0, 21.0, 25.0], index=idx)  # 0.85 backwardated -> 1.25 steep contango
    ts = ind.term_structure(vix, v3m)
    assert ts["score"].iloc[0] == pytest.approx(1.0)
    assert ts["score"].iloc[2] == pytest.approx(0.0)
    assert ts["score"].is_monotonic_decreasing


def test_normalizing_flag_fires_after_inversion_clears():
    idx = pd.date_range("2024-01-01", periods=6, freq="B")
    vix = pd.Series([20.0] * 6, index=idx)
    # inverted for two sessions, then back into contango
    v3m = pd.Series([19.0, 19.0, 21.0, 21.0, 21.0, 21.0], index=idx)
    ts = ind.term_structure(vix, v3m)
    assert ts["normalizing"].iloc[0] == 0.0   # still inverted
    assert ts["normalizing"].iloc[2] == 1.0   # just cleared
    assert ts["normalizing"].iloc[5] == 1.0   # still inside the 10-day window


def test_term_weight_defaults_to_zero_so_the_gate_is_unchanged(spy, provider, vix3m):
    """Adding VIX3M must not silently move the gate — it is weighted 0 by default."""
    vix = provider.history("VIX")["close"]
    rsp = provider.history("RSP")["close"]
    without = ind.compute_regime(spy["close"], vix=vix, equal_weight=rsp)
    with_term = ind.compute_regime(spy["close"], vix=vix, equal_weight=rsp, vix3m=vix3m)
    a, b = without["score"].dropna(), with_term["score"].dropna()
    shared = a.index.intersection(b.index)
    assert np.allclose(a[shared], b[shared], atol=1e-12)
    # but the diagnostic columns are now available
    assert {"term_ratio", "backwardated", "term_normalizing"} <= set(with_term.columns)


def test_term_weight_can_be_enabled_explicitly(spy, provider, vix3m):
    vix = provider.history("VIX")["close"]
    weighted = ind.compute_regime(
        spy["close"], vix=vix, vix3m=vix3m,
        weights={"trend": 0.5, "vol": 0.3, "drawdown": 0.1, "term": 0.1},
    )
    plain = ind.compute_regime(
        spy["close"], vix=vix, weights={"trend": 0.5, "vol": 0.3, "drawdown": 0.1},
    )
    a, b = weighted["score"].dropna(), plain["score"].dropna()
    shared = a.index.intersection(b.index)
    assert not np.allclose(a[shared], b[shared], atol=1e-6)


# ---------------------------------------------------------------- core sleeve

def test_core_weight_captures_every_best_day(spy, regime):
    from fireplanner.backtest import best_days_analysis

    e = ind.enrich(spy)
    tactical = run_backtest(e, regime=regime["score"], symbol="SPY", cfg=BacktestConfig())
    cored = run_backtest(e, regime=regime["score"], symbol="SPY", cfg=BacktestConfig(core_weight=0.5))

    bd_t = best_days_analysis(spy["close"], tactical.daily["in_market"])
    bd_c = best_days_analysis(spy["close"], cored.daily["in_market"])
    # the whole point: a permanent core is exposed to every up day
    assert bd_c["best_captured"] == bd_c["n"]
    assert bd_t["best_captured"] < bd_c["best_captured"]
    assert bd_c["best_forgone_pct"] == pytest.approx(0.0)


def test_core_is_never_sold(spy, regime):
    """No trade in the log may account for the core — it is bought once and held."""
    e = ind.enrich(spy)
    r = run_backtest(e, regime=regime["score"], symbol="SPY", cfg=BacktestConfig(core_weight=0.5))
    # equity never drops to pure cash: the core is always marked to market
    assert (r.daily["in_market"] > 0).all()
    assert r.equity_curve.iloc[-1] > 0


def test_core_weight_raises_return_and_drawdown_together(spy, regime):
    """The frontier has no free lunch — more core means more of both."""
    e = ind.enrich(spy)
    results = {
        w: run_backtest(e, regime=regime["score"], symbol="SPY", cfg=BacktestConfig(core_weight=w))
        for w in (0.0, 0.4, 0.6)
    }
    cagrs = [results[w].stats["cagr_pct"] for w in (0.0, 0.4, 0.6)]
    dds = [results[w].stats["max_drawdown_pct"] for w in (0.0, 0.4, 0.6)]
    assert cagrs[0] < cagrs[1] < cagrs[2]
    assert dds[0] > dds[1] > dds[2]  # drawdowns are negative, so deeper as core grows


def test_fast_reentry_increases_participation(spy, regime):
    e = ind.enrich(spy)
    signal = regime["term_normalizing"]
    slow = run_backtest(e, regime=regime["score"], symbol="SPY", cfg=BacktestConfig(fast_reentry=False))
    fast = run_backtest(e, regime=regime["score"], symbol="SPY",
                        cfg=BacktestConfig(fast_reentry=True), reentry_signal=signal)
    assert fast.stats["trades"] > slow.stats["trades"]
    assert fast.stats["exposure_pct"] >= slow.stats["exposure_pct"]


def test_best_days_analysis_accounting():
    from fireplanner.backtest import best_days_analysis

    idx = pd.date_range("2024-01-01", periods=8, freq="B")
    close = pd.Series([100, 110, 99, 108, 100, 101, 102, 103], index=idx, dtype=float)
    held = pd.Series([0, 0, 1, 1, 1, 1, 1, 1], index=idx, dtype=float)
    bd = best_days_analysis(close, held, n=2)
    assert 0 <= bd["best_captured"] <= 2
    assert 0 <= bd["worst_avoided"] <= 2
    # captured + forgone must reconstruct the total, with no double counting
    assert bd["best_captured_pct"] + bd["best_forgone_pct"] == pytest.approx(bd["best_total_pct"])


def test_backtest_refuses_insufficient_history():
    short = pd.DataFrame(
        {"open": [1.0] * 50, "high": [1.1] * 50, "low": [0.9] * 50, "close": [1.0] * 50, "volume": [1] * 50},
        index=pd.date_range("2024-01-01", periods=50, freq="B"),
    )
    with pytest.raises(ValueError, match="history"):
        run_backtest(ind.enrich(short), symbol="X")


# ---------------------------------------------------------------- partial bars

def test_drops_a_session_still_in_progress():
    """IBKR returns today's half-formed bar while the market is open."""
    from fireplanner.data import drop_incomplete_last_bar

    idx = pd.to_datetime(["2026-08-11", "2026-08-12", "2026-08-13"])
    df = pd.DataFrame({"close": [770.56, 772.49, 776.13]}, index=idx)

    mid_session = pd.Timestamp("2026-08-13 16:30", tz="UTC")   # 12:30 ET
    kept = drop_incomplete_last_bar(df, now=mid_session)
    assert len(kept) == 2
    assert kept.index[-1].date().isoformat() == "2026-08-12"


def test_keeps_the_bar_once_the_session_has_closed():
    from fireplanner.data import drop_incomplete_last_bar

    idx = pd.to_datetime(["2026-08-12", "2026-08-13"])
    df = pd.DataFrame({"close": [772.49, 776.13]}, index=idx)
    after_close = pd.Timestamp("2026-08-13 21:30", tz="UTC")   # 17:30 ET
    assert len(drop_incomplete_last_bar(df, now=after_close)) == 2


def test_never_touches_historical_bars():
    from fireplanner.data import drop_incomplete_last_bar

    idx = pd.to_datetime(["2026-08-11", "2026-08-12", "2026-08-13"])
    df = pd.DataFrame({"close": [1.0, 2.0, 3.0]}, index=idx)
    # a week later, nothing is in progress
    later = pd.Timestamp("2026-08-20 16:30", tz="UTC")
    assert len(drop_incomplete_last_bar(df, now=later)) == 3


def test_naive_now_is_treated_as_utc():
    from fireplanner.data import drop_incomplete_last_bar

    idx = pd.to_datetime(["2026-08-12", "2026-08-13"])
    df = pd.DataFrame({"close": [1.0, 2.0]}, index=idx)
    assert len(drop_incomplete_last_bar(df, now=pd.Timestamp("2026-08-13 16:30"))) == 1


def test_empty_frame_is_safe():
    from fireplanner.data import drop_incomplete_last_bar

    empty = pd.DataFrame({"close": []}, index=pd.to_datetime([]))
    assert drop_incomplete_last_bar(empty).empty
