"""The decision layer: how much of the index should be owned today.

Everything else in this package answers *what the market is doing*. This module
answers the only question that costs money: **in or out, and by how much.**

The design follows three rules, each of which exists to stop a specific failure:

1. **Discrete steps, not a continuous dial.** A target that drifts 62% → 61% →
   63% invites trading noise. Targets snap to a ladder (0 / 25 / 50 / 75 / 100%
   of the tactical sleeve), so a change is always a real change.

2. **Hysteresis.** A new reading must persist for ``confirm_days`` sessions
   before the committed target moves. This is what stops the signal flip-flopping
   across a threshold and handing you two trades where zero were warranted.

3. **A permanent core.** The sleeve moves; the core never does. Measured on the
   shipped sample, a purely tactical rule captured *none* of the market's twenty
   best days. The core is what keeps you in the up-tape while the sleeve manages
   risk around it.

The output is deliberately boring: a target percentage, the trade to get there,
and the price levels that would change the answer.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

__all__ = [
    "AllocationPolicy",
    "AllocationState",
    "target_allocation",
    "trigger_levels",
    "latest_decision",
]


@dataclass(frozen=True)
class AllocationPolicy:
    """Portfolio policy — the part you decide once, not every morning.

    The defaults are not guesses. A first cut using a single score threshold and
    two-day confirmation produced **24 target changes a year with a 61% reversal
    rate** — nearly two thirds of trades undone within ten sessions. Three
    changes fixed it, and each is measured in ``tests/test_allocation.py``:

    * **Smoothing.** The raw score has a 4.9-point daily standard deviation and
      crossed a fixed threshold 33 times in 250 sessions. A short EMA is applied
      before laddering.
    * **A Schmitt trigger.** Separate thresholds to step up and to step back
      down, with a dead band between them, so a reading that hovers on a
      boundary cannot oscillate.
    * **A cooldown.** A minimum number of sessions between committed changes.
    """

    #: Never sold. This is an allocation decision, not a signal.
    core_weight: float = 0.40
    #: Maximum tactical exposure stacked on top of the core.
    sleeve_max: float = 0.60
    #: Sleeve fill levels. Discrete on purpose.
    steps: tuple[float, ...] = (0.0, 0.25, 0.50, 0.75, 1.0)
    #: Smoothed score needed to step **up** to each successive fill level.
    ladder_up: tuple[float, ...] = (48.0, 58.0, 68.0, 78.0)
    #: Smoothed score that forces a step **down** from each level. The gap to
    #: ``ladder_up`` is the dead band that kills oscillation.
    ladder_down: tuple[float, ...] = (40.0, 50.0, 60.0, 70.0)
    #: EMA span applied to the score before laddering.
    score_smoothing: int = 10
    #: Minimum sessions between committed target changes. Roughly one month.
    cooldown_days: int = 21
    #: Consecutive closes below the 50-day before the sleeve is forced flat.
    #: One close is noise; two is a break.
    break_confirm_days: int = 2
    #: Skip trades smaller than this fraction of equity — commissions and spread
    #: make a 2% rebalance worse than doing nothing.
    min_trade_pct: float = 0.05
    #: A confirmed close below the 50-day forces the sleeve flat regardless of score.
    hard_exit_below_sma50: bool = True
    #: Risk-off exits bypass the cooldown. Protection should never wait.
    urgent_exit_bypasses_cooldown: bool = True
    #: Sessions above the 50-day before the sleeve may be restored after a
    #: *forced* exit. Defaults to the full cooldown — i.e. no fast path.
    #:
    #: This knob exists because the obvious argument for a fast path is wrong,
    #: and it is worth being able to re-check that. A protective exit does lock
    #: you out of part of the recovery: in July 2026 it held the target at
    #: core-only for 15 sessions while the index rallied 3.4%. But shortening the
    #: wait makes the signal oscillate around the 50-day, and that costs more::
    #:
    #:     reentry_confirm   changes/yr   reversal    CAGR    Sharpe
    #:     full cooldown           7.6       3.2%    9.31%     1.14
    #:     3 sessions             11.4      31.9%    8.54%     1.03
    #:     5 sessions             10.0      24.4%    9.04%     1.10
    #:     10 sessions             7.9       3.1%    9.20%     1.13
    #:
    #: Lower it only if you re-measure and accept the churn.
    reentry_confirm_days: int = 21

    def __post_init__(self):
        if len(self.ladder_up) != len(self.steps) - 1:
            raise ValueError("ladder_up must have one threshold per non-zero step")
        if len(self.ladder_down) != len(self.ladder_up):
            raise ValueError("ladder_down must be the same length as ladder_up")
        if any(d >= u for d, u in zip(self.ladder_down, self.ladder_up)):
            raise ValueError("each ladder_down threshold must sit below its ladder_up pair")

    def max_target(self) -> float:
        return self.core_weight + self.sleeve_max


@dataclass
class AllocationState:
    """One day's decision, ready to act on."""

    date: pd.Timestamp
    target_pct: float
    previous_pct: float
    current_pct: float
    action: str
    reason: str
    sleeve_fill: float
    regime_label: str
    regime_cap: float
    score: float
    days_in_state: int
    equity: float
    price: float
    shares_delta: float
    dollars_delta: float
    triggers: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        def clean(v):
            if v is None:
                return None
            f = float(v)
            return None if np.isnan(f) else round(f, 4)

        return {
            "date": str(self.date.date()) if hasattr(self.date, "date") else str(self.date),
            "target_pct": clean(self.target_pct),
            "previous_pct": clean(self.previous_pct),
            "current_pct": clean(self.current_pct),
            "action": self.action,
            "reason": self.reason,
            "sleeve_fill": clean(self.sleeve_fill),
            "regime_label": self.regime_label,
            "regime_cap": clean(self.regime_cap),
            "score": clean(self.score),
            "days_in_state": int(self.days_in_state),
            "equity": clean(self.equity),
            "price": clean(self.price),
            "shares_delta": clean(self.shares_delta),
            "dollars_delta": clean(self.dollars_delta),
            "triggers": self.triggers,
        }


def _schmitt_ladder(scores: np.ndarray, policy: AllocationPolicy) -> np.ndarray:
    """Walk the score through a hysteresis ladder.

    Stepping up requires clearing ``ladder_up``; stepping back down requires
    falling under ``ladder_down``. Between the two the level simply holds, which
    is what stops a score loitering on a boundary from generating trades.
    """
    n_levels = len(policy.steps)
    level = 0
    out = np.zeros(len(scores))

    for i, s in enumerate(scores):
        if np.isnan(s):
            out[i] = policy.steps[level]
            continue
        # Climb as far as the score justifies...
        while level < n_levels - 1 and s >= policy.ladder_up[level]:
            level += 1
        # ...then fall only through the lower thresholds.
        while level > 0 and s < policy.ladder_down[level - 1]:
            level -= 1
        out[i] = policy.steps[level]
    return out


def target_allocation(
    enriched: pd.DataFrame,
    scored: pd.DataFrame,
    regime: pd.DataFrame,
    policy: AllocationPolicy | None = None,
) -> pd.DataFrame:
    """Daily target allocation, before and after hysteresis.

    Returns ``raw_target`` (what today's readings say on their own) alongside
    ``target`` (what the policy actually commits to, after confirmation). The gap
    between the two is where the whipsaw protection lives.
    """
    policy = policy or AllocationPolicy()

    idx = enriched.index
    score = scored["score"].reindex(idx)
    cap = regime["exposure_cap"].reindex(idx).ffill()
    label = regime["label"].reindex(idx).ffill()

    # 1. Smooth before laddering — the raw score is too noisy to threshold.
    smooth = score.ewm(span=policy.score_smoothing, adjust=False, min_periods=1).mean()

    # 2. Hysteresis ladder.
    fills = _schmitt_ladder(smooth.to_numpy(float), policy)

    # 3. The regime cap is a hard ceiling: conviction never overrides the gate.
    fills = np.minimum(fills, np.nan_to_num(cap.to_numpy(float), nan=0.0))

    # 4. Structural override: a *confirmed* break of the 50-day flattens the
    #    sleeve whatever the score says. One close below is noise, not a break.
    urgent = np.zeros(len(idx), dtype=bool)
    if policy.hard_exit_below_sma50:
        below = (enriched["close"] < enriched["sma50"]).fillna(False)
        confirmed = below.rolling(policy.break_confirm_days, min_periods=policy.break_confirm_days).min()
        urgent = (confirmed > 0).to_numpy()
        fills = np.where(urgent, 0.0, fills)

    raw_target = policy.core_weight + policy.sleeve_max * fills

    # 5. Cooldown — a committed change stands for at least cooldown_days, so a
    #    real move is not chopped up into a sequence of small ones. Risk-off
    #    exits may bypass it: protection should never sit in a queue.
    committed = np.full(len(idx), np.nan)
    days_in_state = np.zeros(len(idx), dtype=int)

    valid = (score.notna() & cap.notna()).to_numpy()
    current = policy.core_weight
    since_change = policy.cooldown_days
    last_direction = 0
    last_exit_was_urgent = False
    clear_run = 0
    state_run = 0
    started = False
    pending_countdown = np.zeros(len(idx), dtype=int)

    for i in range(len(idx)):
        if not valid[i]:
            committed[i] = np.nan
            continue
        rt = raw_target[i]
        # Consecutive sessions with no forced-exit condition in play.
        clear_run = 0 if urgent[i] else clear_run + 1
        if not started:
            current, started, state_run, since_change = rt, True, 1, 0
        elif np.isclose(rt, current):
            state_run += 1
            since_change += 1
        else:
            direction = 1 if rt > current else -1
            is_exit = direction < 0
            urgent_now = is_exit and urgent[i] and policy.urgent_exit_bypasses_cooldown

            # The cooldown exists to stop *reversals*, not to slow a trend down.
            # Continuing in the same direction as the last move is always allowed:
            # blocking it would re-create the slow-re-entry problem the core was
            # added to solve. Only a change of direction has to wait.
            reversing = last_direction != 0 and direction != last_direction

            # Recovering from a forced exit is not a discretionary reversal, so it
            # answers to a short confirmation rather than the full cooldown —
            # otherwise the protective rule also locks you out of the rebound.
            recovering = (
                last_exit_was_urgent
                and direction > 0
                and not urgent[i]
                and clear_run >= policy.reentry_confirm_days
            )

            allowed = urgent_now or recovering or not reversing or since_change >= policy.cooldown_days

            if allowed:
                current = rt
                last_direction = direction
                last_exit_was_urgent = urgent_now
                state_run, since_change = 1, 0
            else:
                state_run += 1
                since_change += 1
                pending_countdown[i] = max(0, policy.cooldown_days - since_change)
        committed[i] = current
        days_in_state[i] = state_run

    out = pd.DataFrame(
        {
            "raw_target": raw_target,
            "target": committed,
            "sleeve_fill": fills,
            "score": score,
            "score_smooth": smooth,
            "regime_cap": cap,
            "regime_label": label,
            "days_in_state": days_in_state,
            "urgent_exit": urgent.astype(float),
            "pending_days": pending_countdown,
            "close": enriched["close"],
        },
        index=idx,
    )
    out.loc[~valid, ["raw_target", "sleeve_fill", "score_smooth"]] = np.nan

    prev = out["target"].shift(1)
    delta = out["target"] - prev
    out["changed"] = delta.abs() > 1e-9
    out["action"] = np.where(
        delta > 1e-9, "ADD", np.where(delta < -1e-9, "TRIM", "HOLD")
    )
    out.loc[out["target"].notna() & prev.isna(), "action"] = "START"
    return out


def trigger_levels(enriched: pd.DataFrame, regime: pd.DataFrame, policy: AllocationPolicy | None = None) -> dict:
    """Price levels that would change tomorrow's answer.

    A dashboard that only reports today's state makes you re-check it constantly.
    These are the levels worth setting an alert on, so you can ignore the screen
    until one is hit.
    """
    policy = policy or AllocationPolicy()
    last = enriched.iloc[-1]
    close = float(last["close"])

    levels = {}

    sma50 = float(last["sma50"])
    levels["sma50"] = {
        "label": "50-day average",
        "level": sma50,
        "distance_pct": 100.0 * (close / sma50 - 1.0),
        "meaning": "close below forces the tactical sleeve flat",
        "side": "below",
    }

    sma200 = float(last["sma200"])
    levels["sma200"] = {
        "label": "200-day average",
        "level": sma200,
        "distance_pct": 100.0 * (close / sma200 - 1.0),
        "meaning": "the primary trend line — below it the gate collapses",
        "side": "below",
    }

    st = float(last["supertrend"])
    levels["supertrend"] = {
        "label": "Supertrend stop",
        "level": st,
        "distance_pct": 100.0 * (close / st - 1.0),
        "meaning": "trailing volatility stop; a flip down is an exit condition",
        "side": "below",
    }

    atr = float(last["atr14"])
    levels["atr_stop"] = {
        "label": "2.5 × ATR stop",
        "level": close - 2.5 * atr,
        "distance_pct": -100.0 * (2.5 * atr) / close,
        "meaning": "where a new position opened today would be stopped out",
        "side": "below",
    }

    if "vix" in regime.columns and regime["vix"].notna().any():
        vix_now = float(regime["vix"].dropna().iloc[-1])
        levels["vix"] = {
            "label": "VIX",
            "level": 25.0,
            "current": vix_now,
            "distance_pct": 100.0 * (vix_now / 25.0 - 1.0),
            "meaning": "above 25 the volatility component starts cutting the cap",
            "side": "above",
        }

    if "term_ratio" in regime.columns and regime["term_ratio"].notna().any():
        tr = float(regime["term_ratio"].dropna().iloc[-1])
        levels["term"] = {
            "label": "VIX3M / VIX",
            "level": 1.0,
            "current": tr,
            "distance_pct": 100.0 * (tr / 1.0 - 1.0),
            "meaning": "below 1.0 is backwardation — panic, and historically a bottom",
            "side": "below",
        }

    return levels


def latest_decision(
    alloc: pd.DataFrame,
    equity: float,
    current_weight: float,
    policy: AllocationPolicy | None = None,
    triggers: dict | None = None,
) -> AllocationState:
    """Turn the allocation frame into today's instruction."""
    policy = policy or AllocationPolicy()
    valid = alloc.dropna(subset=["target"])
    if valid.empty:
        raise ValueError("no complete allocation rows — need >200 sessions of history")

    row = valid.iloc[-1]
    prev = float(valid["target"].iloc[-2]) if len(valid) > 1 else float(row["target"])
    target = float(row["target"])
    price = float(row["close"])
    # Report the *committed* sleeve fill, not today's raw reading — the two can
    # differ while a reversal waits out the cooldown, and showing the raw one
    # would contradict the target printed beside it.
    committed_fill = (
        (target - policy.core_weight) / policy.sleeve_max if policy.sleeve_max else 0.0
    )

    drift = target - current_weight
    dollars = drift * equity
    shares = dollars / price if price > 0 else 0.0

    pending = int(row.get("pending_days", 0) or 0)
    raw = float(row["raw_target"])
    pending_change = pending > 0 and not np.isclose(raw, target)

    if abs(drift) < policy.min_trade_pct:
        action = "HOLD"
        reason = (
            f"Already within {policy.min_trade_pct * 100:.0f}% of target — "
            f"the trade is too small to be worth the costs."
        )
    elif pending_change and abs(raw - current_weight) < abs(target - current_weight):
        # Trading to a target that is about to move toward where you already sit
        # would be a round trip: sell today, buy back next week. The cooldown
        # exists to prevent exactly that, so it has to govern the instruction too,
        # not just the internal state.
        action = "WAIT"
        reason = (
            f"Do not trade yet. The committed target is {target * 100:.0f}%, but the signal has "
            f"already moved to {raw * 100:.0f}% and is waiting out the cooldown — and "
            f"{raw * 100:.0f}% is closer to the {current_weight * 100:.0f}% you already hold. "
            f"Trading to {target * 100:.0f}% now would mean reversing it within "
            f"{pending} session{'s' if pending != 1 else ''}."
        )
    elif drift > 0:
        action = "BUY"
        reason = f"Under target by {abs(drift) * 100:.1f}% of equity."
    else:
        action = "SELL"
        reason = f"Over target by {abs(drift) * 100:.1f}% of equity."

    if row["changed"] and action != "WAIT":
        reason = f"Target moved {prev * 100:.0f}% → {target * 100:.0f}% today. " + reason

    if pending_change and action != "WAIT":
        direction = "up" if raw > target else "down"
        reason += (
            f" The underlying signal now reads {raw * 100:.0f}%, but a reversal has to wait out the "
            f"cooldown: if it still reads that way in {pending} session"
            f"{'s' if pending != 1 else ''}, the target steps {direction}."
        )

    if action == "WAIT":
        shares = dollars = 0.0

    return AllocationState(
        date=valid.index[-1],
        target_pct=target,
        previous_pct=prev,
        current_pct=current_weight,
        action=action,
        reason=reason,
        sleeve_fill=float(committed_fill),
        regime_label=str(row["regime_label"]),
        regime_cap=float(row["regime_cap"]),
        score=float(row["score"]),
        days_in_state=int(row["days_in_state"]),
        equity=equity,
        price=price,
        shares_delta=shares,
        dollars_delta=dollars,
        triggers=triggers or {},
    )
