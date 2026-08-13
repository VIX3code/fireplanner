"""The decision layer: hysteresis, cooldown, and the reliability guarantees.

The tests that matter most here are the *regression guards* at the bottom. The
signal's usefulness rests on it not flip-flopping, and that is a property of the
whole pipeline rather than any one function — so it is asserted directly on five
years of real bars. If a future change to the score, the regime, or the ladder
starts generating churn, these fail.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from fireplanner import indicators as ind
from fireplanner.backtest import run_allocation_backtest
from fireplanner.data import SnapshotProvider
from fireplanner.signals import (
    AllocationPolicy,
    latest_decision,
    score_frame,
    target_allocation,
    trigger_levels,
)


@pytest.fixture(scope="module")
def provider():
    return SnapshotProvider("data/snapshots")


@pytest.fixture(scope="module")
def parts(provider):
    spy = provider.history("SPY")
    regime = ind.compute_regime(
        spy["close"],
        vix=provider.history("VIX")["close"],
        equal_weight=provider.history("RSP")["close"],
        vix3m=provider.history("VIX3M")["close"],
    )
    enriched = ind.enrich(spy)
    scored = score_frame(enriched, regime=regime["score"])
    return spy, enriched, scored, regime


@pytest.fixture(scope="module")
def alloc(parts):
    _, enriched, scored, regime = parts
    return target_allocation(enriched, scored, regime, AllocationPolicy())


def _reversal_stats(a: pd.DataFrame) -> dict:
    d = a.dropna(subset=["target"])
    ch = d[d["changed"].fillna(False)]
    years = max((d.index[-1] - d.index[0]).days / 365.25, 1e-9)
    pos = {dt: i for i, dt in enumerate(d.index)}
    idxs = list(ch.index)
    dirs = np.sign(ch["target"].diff().fillna(0).to_numpy())
    rev = sum(
        1 for i in range(len(idxs) - 1)
        if pos[idxs[i + 1]] - pos[idxs[i]] <= 10 and dirs[i + 1] * dirs[i] < 0
    )
    return {
        "changes": len(ch),
        "per_year": len(ch) / years,
        "reversal_pct": 100.0 * rev / max(1, len(ch) - 1),
    }


# ---------------------------------------------------------------- policy

def test_policy_rejects_inverted_ladder():
    with pytest.raises(ValueError, match="below its ladder_up"):
        AllocationPolicy(ladder_up=(50.0, 60.0, 70.0, 80.0), ladder_down=(55.0, 65.0, 75.0, 85.0))


def test_policy_rejects_mismatched_ladder_length():
    with pytest.raises(ValueError, match="one threshold per"):
        AllocationPolicy(ladder_up=(50.0, 60.0))


def test_max_target_is_core_plus_sleeve():
    assert AllocationPolicy(core_weight=0.4, sleeve_max=0.6).max_target() == pytest.approx(1.0)


# ---------------------------------------------------------------- ladder

def test_schmitt_ladder_does_not_oscillate_in_the_dead_band(parts):
    """A score parked between the up and down thresholds must not generate trades."""
    from fireplanner.signals.allocation import _schmitt_ladder

    pol = AllocationPolicy()
    # Wobble either side of the first up-threshold (48) but stay above the
    # down-threshold (40): the level must climb once and then hold.
    scores = np.array([30, 50, 45, 49, 44, 47, 46, 49, 43], dtype=float)
    fills = _schmitt_ladder(scores, pol)
    assert fills[0] == 0.0
    assert fills[1] == pytest.approx(0.25)
    assert all(f == pytest.approx(0.25) for f in fills[1:]), fills


def test_schmitt_ladder_steps_down_only_below_the_lower_threshold(parts):
    from fireplanner.signals.allocation import _schmitt_ladder

    pol = AllocationPolicy()
    scores = np.array([80, 69, 59, 49, 39], dtype=float)
    fills = _schmitt_ladder(scores, pol)
    # 80 -> top; then each drop crosses one ladder_down rung (70/60/50/40)
    assert list(fills) == [1.0, 0.75, 0.5, 0.25, 0.0]


# ---------------------------------------------------------------- cooldown

def _synthetic(scores, closes=None, n=None):
    """Build the minimal frames target_allocation needs, from a score path."""
    n = n or len(scores)
    idx = pd.date_range("2024-01-01", periods=n, freq="B")
    closes = closes if closes is not None else np.full(n, 100.0)
    enriched = pd.DataFrame(
        {"close": closes, "sma50": np.full(n, 50.0), "sma200": np.full(n, 50.0)}, index=idx
    )
    scored = pd.DataFrame({"score": scores}, index=idx)
    regime = pd.DataFrame(
        {"exposure_cap": np.ones(n), "label": ["Risk-On"] * n}, index=idx
    )
    return enriched, scored, regime


def test_cooldown_allows_continuation_but_blocks_reversal():
    pol = AllocationPolicy(score_smoothing=1, cooldown_days=10)
    # Start part-filled, step up (establishing an upward direction), then drop.
    # The first commit has no prior direction, so it is the *second* move that
    # the reversal rule can act on.
    # Long enough for the 10-session cooldown to actually elapse afterwards.
    scores = np.array([50] * 2 + [90] * 3 + [10] * 15, dtype=float)
    e, s, r = _synthetic(scores)
    a = target_allocation(e, s, r, pol)
    t = a["target"].to_numpy()

    assert t[0] == pytest.approx(0.55)               # first commit: 25% sleeve
    assert t[2] == pytest.approx(1.0)                # continuation up, immediate
    assert t[5] == pytest.approx(1.0), "reversal must not fire immediately"
    assert a["pending_days"].iloc[5] > 0
    # ...and it does fire once the cooldown has run
    assert t[-1] == pytest.approx(pol.core_weight)
    assert a["pending_days"].iloc[-1] == 0


def test_first_commit_has_no_direction_to_reverse():
    """Startup is not a reversal — the very first target is taken as given."""
    pol = AllocationPolicy(score_smoothing=1, cooldown_days=10)
    scores = np.array([90] * 3 + [10] * 6, dtype=float)
    e, s, r = _synthetic(scores)
    a = target_allocation(e, s, r, pol)
    assert a["target"].iloc[0] == pytest.approx(1.0)
    # No prior committed direction exists, so the first change is not blocked.
    assert a["target"].iloc[3] == pytest.approx(pol.core_weight)


def test_urgent_exit_bypasses_the_cooldown():
    """A confirmed break of the 50-day must act at once, cooldown or not."""
    pol = AllocationPolicy(score_smoothing=1, cooldown_days=21, break_confirm_days=2)
    n = 12
    scores = np.full(n, 90.0)
    closes = np.full(n, 100.0)
    closes[6:] = 10.0  # collapse below the 50-day (fixed at 50 in the fixture)
    e, s, r = _synthetic(scores, closes)
    a = target_allocation(e, s, r, pol)
    # fully invested before, flat sleeve within two sessions of the break
    assert a["target"].iloc[5] == pytest.approx(1.0)
    assert a["target"].iloc[7] == pytest.approx(pol.core_weight)
    assert a["urgent_exit"].iloc[7] == 1.0


def test_target_never_leaves_the_ladder(alloc):
    pol = AllocationPolicy()
    allowed = {round(pol.core_weight + pol.sleeve_max * s, 10) for s in pol.steps}
    got = {round(float(v), 10) for v in alloc["target"].dropna().unique()}
    assert got <= allowed, got - allowed


def test_target_never_falls_below_the_core(alloc):
    assert (alloc["target"].dropna() >= AllocationPolicy().core_weight - 1e-9).all()


# ---------------------------------------------------------------- no lookahead

def test_allocation_walk_does_not_look_ahead(parts):
    """Truncating the series must not change any already-committed target.

    The commit loop is stateful, so this is the property most at risk of a subtle
    bug — and the one that would make every reliability number above fiction.
    """
    _, enriched, scored, regime = parts
    pol = AllocationPolicy()
    full = target_allocation(enriched, scored, regime, pol)

    for cut in (30, 90, 200):
        trunc = target_allocation(
            enriched.iloc[:-cut], scored.iloc[:-cut], regime.iloc[:-cut], pol
        )
        shared = trunc["target"].dropna().index
        assert len(shared) > 500
        assert np.allclose(full.loc[shared, "target"], trunc.loc[shared, "target"], atol=1e-12)


# ---------------------------------------------------------------- decisions

def test_small_drift_produces_hold(alloc):
    pol = AllocationPolicy()
    target = float(alloc["target"].dropna().iloc[-1])
    state = latest_decision(alloc, equity=100_000, current_weight=target + 0.01, policy=pol)
    assert state.action == "HOLD"
    assert "too small" in state.reason


def test_wait_when_pending_change_moves_toward_current_holding(alloc):
    """Never instruct a trade the cooldown is about to reverse."""
    pol = AllocationPolicy()
    row = alloc.dropna(subset=["target"]).iloc[-1]
    if row["pending_days"] <= 0 or np.isclose(row["raw_target"], row["target"]):
        pytest.skip("no pending change in the shipped snapshot")
    # sit where the *pending* target is heading
    state = latest_decision(alloc, equity=100_000, current_weight=float(row["raw_target"]), policy=pol)
    assert state.action == "WAIT"
    assert state.shares_delta == 0.0
    assert state.dollars_delta == 0.0


def test_decision_share_maths_reconciles(alloc):
    pol = AllocationPolicy()
    state = latest_decision(alloc, equity=50_000, current_weight=0.0, policy=pol)
    if state.action == "WAIT":
        pytest.skip("no trade instructed today")
    expected_dollars = (state.target_pct - state.current_pct) * 50_000
    assert state.dollars_delta == pytest.approx(expected_dollars)
    assert state.shares_delta == pytest.approx(expected_dollars / state.price)


def test_decision_requires_history():
    idx = pd.date_range("2024-01-01", periods=5, freq="B")
    empty = pd.DataFrame(
        {"target": [np.nan] * 5, "raw_target": [np.nan] * 5, "close": [1.0] * 5,
         "sleeve_fill": [np.nan] * 5, "score": [np.nan] * 5, "regime_cap": [np.nan] * 5,
         "regime_label": ["x"] * 5, "days_in_state": [0] * 5, "changed": [False] * 5},
        index=idx,
    )
    with pytest.raises(ValueError, match="no complete allocation rows"):
        latest_decision(empty, equity=1000, current_weight=0.0)


def test_trigger_levels_match_the_indicators(parts):
    _, enriched, _, regime = parts
    t = trigger_levels(enriched, regime)
    last = enriched.iloc[-1]
    assert t["sma50"]["level"] == pytest.approx(float(last["sma50"]))
    assert t["sma200"]["level"] == pytest.approx(float(last["sma200"]))
    assert t["atr_stop"]["level"] == pytest.approx(float(last["close"] - 2.5 * last["atr14"]))
    assert t["sma50"]["distance_pct"] == pytest.approx(
        100.0 * (float(last["close"]) / float(last["sma50"]) - 1.0)
    )


# ---------------------------------------------------------------- reliability guards

def test_signal_does_not_whipsaw(alloc):
    """Regression guard on the property the whole page depends on.

    Baseline without hysteresis was ~24 changes a year at a 72% reversal rate.
    """
    stats = _reversal_stats(alloc)
    assert stats["per_year"] < 12, f"signal changes too often: {stats}"
    assert stats["reversal_pct"] < 15, f"signal reverses too often: {stats}"


def test_hysteresis_is_what_removes_the_whipsaw(parts):
    """The protections must be doing the work — not the underlying score."""
    _, enriched, scored, regime = parts
    naive = AllocationPolicy(
        score_smoothing=1, cooldown_days=0,
        ladder_up=(48.0, 58.0, 68.0, 78.0), ladder_down=(47.9, 57.9, 67.9, 77.9),
    )
    bad = _reversal_stats(target_allocation(enriched, scored, regime, naive))
    good = _reversal_stats(target_allocation(enriched, scored, regime, AllocationPolicy()))
    assert bad["reversal_pct"] > 40
    assert good["reversal_pct"] < bad["reversal_pct"] / 3


def test_reliability_does_not_cost_return(parts):
    """Damping the signal must not be paid for in performance.

    The claim is "free", not "better": across configurations the risk-adjusted
    result lands within noise of the twitchy version, while the trade count
    roughly halves. Asserting strict improvement would be overfitting to this
    sample — a 0.002 Sharpe difference is not a finding.
    """
    spy, enriched, scored, regime = parts
    noisy = AllocationPolicy(score_smoothing=1, cooldown_days=0)
    calm = AllocationPolicy()

    def run(pol):
        a = target_allocation(enriched, scored, regime, pol)
        return run_allocation_backtest(spy["close"], spy["open"], a["target"], band=pol.min_trade_pct)

    r_noisy, r_calm = run(noisy), run(calm)
    assert r_calm.stats["sharpe"] >= r_noisy.stats["sharpe"] - 0.05
    assert r_calm.stats["max_drawdown_pct"] >= r_noisy.stats["max_drawdown_pct"] - 0.5
    # the part that is unambiguous: far less trading
    assert r_calm.stats["rebalances"] < 0.7 * r_noisy.stats["rebalances"]


def test_following_the_signal_beats_holding_on_risk(parts):
    from fireplanner.backtest import buy_and_hold_stats

    spy, enriched, scored, regime = parts
    a = target_allocation(enriched, scored, regime, AllocationPolicy())
    r = run_allocation_backtest(spy["close"], spy["open"], a["target"])
    bh = buy_and_hold_stats(spy["close"].reindex(r.equity_curve.index).dropna())
    assert r.stats["sharpe"] > bh["sharpe"]
    assert r.stats["max_drawdown_pct"] > bh["max_drawdown_pct"]  # shallower
    # and the core keeps it in for the best days
    assert r.stats["bd_best_captured"] == 20


def test_allocation_backtest_costs_reduce_equity(parts):
    spy, enriched, scored, regime = parts
    a = target_allocation(enriched, scored, regime, AllocationPolicy())
    free = run_allocation_backtest(spy["close"], spy["open"], a["target"],
                                   commission_per_share=0.0, min_commission=0.0, slippage_bps=0.0)
    costly = run_allocation_backtest(spy["close"], spy["open"], a["target"],
                                     commission_per_share=0.05, min_commission=1.0, slippage_bps=30.0)
    assert costly.equity_curve.iloc[-1] < free.equity_curve.iloc[-1]
