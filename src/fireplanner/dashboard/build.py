"""Assemble everything the dashboard renders into one payload.

Kept separate from rendering so the same payload can feed the HTML page, a JSON
API, or a test assertion without duplicating any of the maths.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from .. import indicators as ind
from ..backtest import BacktestConfig, best_days_analysis, buy_and_hold_stats, run_backtest
from ..risk import RiskConfig, plan_position
from ..signals import latest_signal, score_frame

__all__ = ["DashboardPayload", "build_payload"]


@dataclass
class DashboardPayload:
    as_of: str
    benchmark: str
    bars: dict = field(default_factory=dict)
    enriched: dict = field(default_factory=dict)
    regime: pd.DataFrame | None = None
    regime_now: dict = field(default_factory=dict)
    signals: list = field(default_factory=list)
    trade_plan: dict = field(default_factory=dict)
    account: dict = field(default_factory=dict)
    positions: pd.DataFrame | None = None
    backtest: dict = field(default_factory=dict)
    quotes: dict = field(default_factory=dict)
    notes: list = field(default_factory=list)
    variants: list = field(default_factory=list)
    best_days: dict = field(default_factory=dict)
    # ---- decision layer (populated by add_decision) ----
    decision: dict = field(default_factory=dict)
    allocation: pd.DataFrame | None = None
    reliability: dict = field(default_factory=dict)
    decision_history: list = field(default_factory=list)
    ladder_steps: tuple = ()
    score_smooth: float | None = None
    next_steps: dict = field(default_factory=dict)
    cooldown_days: int = 0
    staleness_days: int | None = None
    alloc_backtest: dict = field(default_factory=dict)
    alloc_buyhold: dict = field(default_factory=dict)
    #: Daily grading of what the signal said against what the market then did.
    #: Read-only by design — see signals/selftest.py.
    selftest: object | None = None
    exposure_note: str = ""
    exposure_basis: dict = field(default_factory=dict)
    total_changes: int = 0


def build_payload(
    provider,
    benchmark: str = "SPY",
    vol_symbol: str = "VIX",
    breadth_symbol: str = "RSP",
    term_symbol: str = "VIX3M",
    watchlist: list[str] | None = None,
    lookback_days: int = 1260,
    equity: float | None = None,
    risk_cfg: RiskConfig | None = None,
) -> DashboardPayload:
    """Pull bars, compute indicators, score the watchlist, and size a trade."""
    risk_cfg = risk_cfg or RiskConfig()
    notes: list[str] = []

    bench_bars = provider.history(benchmark, lookback_days=lookback_days)

    vix = breadth = None
    try:
        vix = provider.history(vol_symbol, lookback_days=lookback_days)["close"]
    except Exception:
        notes.append(f"{vol_symbol} unavailable — regime computed without the volatility component.")
    try:
        breadth = provider.history(breadth_symbol, lookback_days=lookback_days)["close"]
    except Exception:
        notes.append(f"{breadth_symbol} unavailable — regime computed without the breadth component.")

    vix3m = None
    try:
        vix3m = provider.history(term_symbol, lookback_days=lookback_days)["close"]
    except Exception:
        notes.append(f"{term_symbol} unavailable — no term-structure panel or fast re-entry.")

    regime = ind.compute_regime(bench_bars["close"], vix=vix, equal_weight=breadth, vix3m=vix3m)
    regime_now = ind.latest_regime(regime).as_dict()

    # ---- account ---------------------------------------------------------
    account, positions = {}, None
    if hasattr(provider, "account"):
        account = provider.account() or {}
    if account.get("positions"):
        positions = pd.DataFrame(account["positions"])
    elif hasattr(provider, "positions"):
        try:
            positions = provider.positions()
        except Exception:
            notes.append("Positions unavailable — connect TWS/Gateway for the live book.")

    net_liq = equity
    if net_liq is None:
        net_liq = float(account.get("summary", {}).get("net_liquidation", 100_000.0))

    if positions is not None and not positions.empty:
        positions = positions.copy()
        if "market_value" not in positions and {"position", "market_price"} <= set(positions.columns):
            positions["market_value"] = positions["position"] * positions["market_price"]
        positions["weight_pct"] = 100.0 * positions["market_value"] / net_liq
        if {"market_price", "average_price"} <= set(positions.columns):
            positions["pnl_pct"] = 100.0 * (positions["market_price"] / positions["average_price"] - 1.0)
        positions = positions.sort_values("market_value", ascending=False)

    # ---- watchlist scoring ----------------------------------------------
    watchlist = watchlist or [benchmark]
    if benchmark not in watchlist:
        watchlist = [benchmark] + list(watchlist)

    bars_map, enriched_map, signals = {}, {}, []
    for sym in watchlist:
        try:
            b = provider.history(sym, lookback_days=lookback_days)
        except Exception:
            notes.append(f"{sym}: no history available — skipped.")
            continue
        if len(b) < 220:
            notes.append(f"{sym}: only {len(b)} bars — needs ~250 for the 200-day trend gate; skipped.")
            continue
        e = ind.enrich(b)
        scored = score_frame(e, regime=regime["score"])
        try:
            sig = latest_signal(sym, scored, e)
        except ValueError as exc:
            notes.append(str(exc))
            continue
        bars_map[sym], enriched_map[sym] = b, e
        signals.append(sig.as_dict())

    signals.sort(key=lambda s: (s["score"] is None, -(s["score"] or 0)))

    # ---- trade plan on the benchmark ------------------------------------
    trade_plan = {}
    if benchmark in enriched_map:
        e = enriched_map[benchmark]
        last = e.dropna(subset=["atr14"]).iloc[-1]
        try:
            plan = plan_position(
                benchmark,
                equity=net_liq,
                entry=float(last["close"]),
                atr=float(last["atr14"]),
                cfg=risk_cfg,
                exposure_cap=regime_now.get("exposure_cap", 1.0),
                open_heat=0.0,
            )
            trade_plan = plan.as_dict()
            trade_plan["atr_pct"] = float(last["natr14"])
        except ValueError as exc:
            notes.append(f"{benchmark} sizing skipped: {exc}")

    # ---- backtest evidence ----------------------------------------------
    backtest, variants, best_days = {}, [], {}
    if benchmark in enriched_map:
        e_bench = enriched_map[benchmark]
        reentry = regime["term_normalizing"] if "term_normalizing" in regime.columns else None
        result = run_backtest(e_bench, regime=regime["score"], symbol=benchmark, reentry_signal=reentry)
        bh_curve = bench_bars["close"].reindex(result.equity_curve.index)
        bh = buy_and_hold_stats(bh_curve.dropna())

        # The efficient frontier between "always in" and "purely tactical".
        # This is the answer to how much of the best days a rule gives up, and
        # what a permanent core buys back.
        specs = [
            ("Tactical only", BacktestConfig(fast_reentry=False)),
            ("+ term re-entry", BacktestConfig(fast_reentry=True)),
            ("+ 40% core", BacktestConfig(core_weight=0.40, fast_reentry=True)),
            ("+ 60% core", BacktestConfig(core_weight=0.60, fast_reentry=True)),
        ]
        for name, spec in specs:
            v = run_backtest(e_bench, regime=regime["score"], symbol=benchmark, cfg=spec,
                             reentry_signal=reentry if spec.fast_reentry else None)
            bd = best_days_analysis(bench_bars["close"], v.daily["in_market"])
            variants.append({
                "name": name,
                "core_weight": spec.core_weight,
                "stats": v.stats,
                "best_days": bd,
                "equity": 100.0 * v.equity_curve / v.equity_curve.iloc[0],
            })
        variants.append({
            "name": "Buy & hold",
            "core_weight": 1.0,
            "stats": bh,
            "best_days": best_days_analysis(
                bench_bars["close"],
                pd.Series(1.0, index=result.equity_curve.index),
            ),
            "equity": 100.0 * bh_curve / bh_curve.iloc[0],
        })
        best_days = variants[0]["best_days"]

        backtest = {
            "stats": result.stats,
            "buy_hold": bh,
            "equity": result.equity_curve,
            "bh_equity": 100.0 * bh_curve / bh_curve.iloc[0],
            "strategy_indexed": 100.0 * result.equity_curve / result.equity_curve.iloc[0],
            "trades": result.trades,
            "daily": result.daily,
        }

    quotes = account.get("quotes", {}) if account else {}
    as_of = str(bench_bars.index[-1].date())

    return DashboardPayload(
        as_of=as_of,
        benchmark=benchmark,
        bars=bars_map,
        enriched=enriched_map,
        regime=regime,
        regime_now=regime_now,
        signals=signals,
        trade_plan=trade_plan,
        account=account,
        positions=positions,
        backtest=backtest,
        quotes=quotes,
        notes=notes,
        variants=variants,
        best_days=best_days,
    )


def add_decision(
    payload: DashboardPayload,
    policy=None,
    current_weight: float | None = None,
    equity: float | None = None,
    reference_date: pd.Timestamp | None = None,
) -> DashboardPayload:
    """Attach the allocation decision layer to an existing payload.

    ``current_weight`` is what you actually hold in the benchmark right now, as a
    fraction of equity. Left as None it is inferred from the account's benchmark
    position — which is usually zero, so the exposure note below matters.
    """
    from ..backtest import buy_and_hold_stats, run_allocation_backtest
    from ..signals import AllocationPolicy, latest_decision, target_allocation, trigger_levels

    policy = policy or AllocationPolicy()
    bench = payload.benchmark
    e = payload.enriched.get(bench)
    if e is None or payload.regime is None:
        payload.notes.append(f"No decision computed — {bench} history unavailable.")
        return payload

    scored = score_frame(e, regime=payload.regime["score"])
    alloc = target_allocation(e, scored, payload.regime, policy)

    summary = payload.account.get("summary", {}) if payload.account else {}
    if equity is None:
        equity = float(summary.get("net_liquidation", 100_000.0))

    # What counts as "currently invested"?
    #
    # The benchmark position alone is the wrong basis for a book of correlated
    # single names: this account holds no SPY but ~74% gross equity across 24
    # names, and reading current exposure as 0% would turn a "trim" instruction
    # into a "buy" one — doubling equity risk exactly when the model wants it cut.
    # Total equity exposure is the honest default; the benchmark-only figure is
    # kept alongside it so the basis is never ambiguous.
    held_value = 0.0
    if payload.positions is not None and not payload.positions.empty:
        sym_col = "symbol" if "symbol" in payload.positions.columns else "contract_description"
        match = payload.positions[payload.positions[sym_col].astype(str).str.upper() == bench.upper()]
        held_value = float(match["market_value"].sum()) if not match.empty else 0.0

    bench_weight = held_value / equity if equity else 0.0
    gross_value = float(summary.get("gross_position_value", 0.0) or 0.0)
    if not gross_value and payload.positions is not None and not payload.positions.empty:
        gross_value = float(payload.positions["market_value"].sum())
    total_equity_weight = gross_value / equity if equity else 0.0

    payload.exposure_basis = {
        "benchmark_pct": 100.0 * bench_weight,
        "total_equity_pct": 100.0 * total_equity_weight,
        "n_positions": int(len(payload.positions)) if payload.positions is not None else 0,
        "basis": "total_equity",
    }

    if current_weight is None:
        # Use total equity exposure whenever the book carries meaningful equity
        # risk outside the benchmark itself.
        if total_equity_weight - bench_weight > 0.05:
            current_weight = total_equity_weight
        else:
            current_weight = bench_weight
            payload.exposure_basis["basis"] = "benchmark"
    else:
        payload.exposure_basis["basis"] = "explicit"

    triggers = trigger_levels(e, payload.regime, policy)
    state = latest_decision(alloc, equity=equity, current_weight=current_weight,
                            policy=policy, triggers=triggers)
    payload.decision = state.as_dict()
    payload.allocation = alloc
    payload.ladder_steps = tuple(policy.core_weight + policy.sleeve_max * s for s in policy.steps)
    payload.cooldown_days = policy.cooldown_days
    payload.score_smooth = float(alloc["score_smooth"].dropna().iloc[-1])

    # Where the next rung sits, in score terms.
    fill = float(alloc["sleeve_fill"].iloc[-1])
    try:
        level = list(policy.steps).index(min(policy.steps, key=lambda s: abs(s - fill)))
    except ValueError:
        level = 0
    nxt = {}
    if level > 0:
        nxt["down"] = {
            "threshold": policy.ladder_down[level - 1],
            "target": policy.core_weight + policy.sleeve_max * policy.steps[level - 1],
        }
    if level < len(policy.steps) - 1:
        nxt["up"] = {
            "threshold": policy.ladder_up[level],
            "target": policy.core_weight + policy.sleeve_max * policy.steps[level + 1],
        }
    payload.next_steps = nxt

    # ---- reliability ----------------------------------------------------
    d = alloc.dropna(subset=["target"])
    changes = d[d["changed"].fillna(False)]
    years = max((d.index[-1] - d.index[0]).days / 365.25, 1e-9)
    pos = {dt: i for i, dt in enumerate(d.index)}
    idxs = list(changes.index)
    dirs = np.sign(changes["target"].diff().fillna(0).to_numpy())
    reversals = sum(
        1 for i in range(len(idxs) - 1)
        if pos[idxs[i + 1]] - pos[idxs[i]] <= 10 and dirs[i + 1] * dirs[i] < 0
    )
    gaps = np.diff([pos[x] for x in idxs]) if len(idxs) > 1 else np.array([0])

    bt = run_allocation_backtest(
        payload.bars[bench]["close"], payload.bars[bench]["open"], alloc["target"],
        band=policy.min_trade_pct,
    )
    bh = buy_and_hold_stats(payload.bars[bench]["close"].reindex(bt.equity_curve.index).dropna())
    payload.alloc_backtest = bt.stats
    payload.alloc_buyhold = bh

    # ---- daily self-test -------------------------------------------------
    # Grades the signal against the sessions that followed it. Deliberately
    # computed after everything else and consumed by nobody: it reports on the
    # model, and must never become an input to it.
    from ..signals.selftest import self_test

    payload.selftest = self_test(payload.allocation, payload.ladder_steps)

    payload.reliability = {
        "changes_per_year": len(changes) / years,
        "reversal_pct": 100.0 * reversals / max(1, len(changes) - 1),
        # Measured with hysteresis disabled, so the improvement is not a claim.
        "baseline_reversal_pct": _baseline_reversal(e, scored, payload.regime),
        "median_gap": float(np.median(gaps)),
        "avg_target_pct": 100.0 * float(d["target"].mean()),
        "rebalances": bt.stats.get("rebalances", 0),
        "total_costs": bt.stats.get("total_costs", 0.0),
    }

    prev = d["target"].shift(1)
    # How long the *previous* target stood before this change. days_in_state on a
    # change row is 1 by construction, so reporting it would say nothing.
    change_positions = [pos[x] for x in idxs]
    history = []
    for n, (dt, row) in enumerate(changes.iterrows()):
        if n == 0:
            stood = change_positions[0] - 0
        else:
            stood = change_positions[n] - change_positions[n - 1]
        history.append({
            "date": dt,
            "from": float(prev.loc[dt]) if pd.notna(prev.loc[dt]) else float(row["target"]),
            "to": float(row["target"]),
            "score": float(row["score"]),
            "regime": row["regime_label"],
            "held_days": int(stood),
        })
    payload.decision_history = list(reversed(history))[:12]
    payload.total_changes = int(len(changes))

    ref = reference_date or pd.Timestamp.now('UTC').tz_localize(None).normalize()
    payload.staleness_days = int((ref - d.index[-1]).days)

    eb = payload.exposure_basis
    if eb["basis"] == "total_equity":
        payload.exposure_note = (
            f"Your current exposure is measured as <strong>total equity risk</strong> — "
            f"{eb['total_equity_pct']:.0f}% of net liquidation across {eb['n_positions']} single "
            f"names — not as your {bench} position, which is {eb['benchmark_pct']:.0f}%. Those names "
            f"carry substantially the same market risk the index does, so treating them as cash "
            f"would invert the instruction. Act on this by trimming or adding across the whole book, "
            f"or by hedging the difference — not by trading {bench} as though the rest were flat."
        )
    return payload


def _baseline_reversal(enriched, scored, regime) -> float:
    """Reversal rate with the whipsaw protections switched off, for comparison."""
    from ..signals import AllocationPolicy, target_allocation

    naive = AllocationPolicy(score_smoothing=1, cooldown_days=0,
                             ladder_up=(48.0, 58.0, 68.0, 78.0),
                             ladder_down=(47.9, 57.9, 67.9, 77.9))
    a = target_allocation(enriched, scored, regime, naive).dropna(subset=["target"])
    ch = a[a["changed"].fillna(False)]
    if len(ch) < 2:
        return 0.0
    pos = {dt: i for i, dt in enumerate(a.index)}
    idxs = list(ch.index)
    dirs = np.sign(ch["target"].diff().fillna(0).to_numpy())
    rev = sum(1 for i in range(len(idxs) - 1)
              if pos[idxs[i + 1]] - pos[idxs[i]] <= 10 and dirs[i + 1] * dirs[i] < 0)
    return 100.0 * rev / max(1, len(ch) - 1)
