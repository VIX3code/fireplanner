"""Daily-bar backtester for the swing model.

Deliberate choices, because these are where backtests usually lie to you:

* **Signals at the close, fills at the next open.** You cannot trade a close you
  needed to compute the signal. Every entry and every discretionary exit is
  filled on the following session's open.
* **Gap-aware stops.** If a session opens below the stop, the fill is the open,
  not the stop. Assuming you got your stop price in a gap-down is the single
  most common way a backtest invents money it never made.
* **Stops checked intrabar.** A stop that was touched during the day is hit,
  even if the close recovered.
* **Costs on both sides.** Commission per share plus slippage in basis points.
* **Warm-up respected.** Trading starts only once every indicator the model
  reads is non-NaN, so early bars cannot produce phantom signals.

The engine is long/flat on a single instrument. That matches the stated use —
trading the S&P 500 index or one stock at a time — and keeps the accounting
auditable: every trade is a row you can tie back to two dated fills.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from ..risk.sizing import RiskConfig
from ..signals.model import SignalConfig, score_frame

__all__ = ["BacktestConfig", "BacktestResult", "run_backtest"]


@dataclass(frozen=True)
class BacktestConfig:
    initial_equity: float = 100_000.0
    #: Per-share commission (IBKR tiered US equities is ~$0.0035, $1.00 minimum).
    commission_per_share: float = 0.0035
    min_commission: float = 1.00
    #: One-way slippage in basis points applied to every fill.
    slippage_bps: float = 2.0
    #: Score at or above which a flat book opens a position.
    entry_score: float = 65.0
    #: Score below which an open position is closed.
    exit_score: float = 45.0
    #: Exit when price closes below the 50-day, regardless of score.
    exit_on_sma50_break: bool = True
    #: Exit when Supertrend flips down.
    exit_on_supertrend_flip: bool = True
    use_trailing_stop: bool = True
    #: Minimum sessions to hold before a score-based exit may fire (0 = none).
    min_hold_days: int = 0
    allow_reentry_same_day: bool = False
    #: Fraction of equity held permanently in the asset and never sold.
    #:
    #: This is the direct answer to "a filter that goes to cash misses the best
    #: days". Measured on the shipped sample, the tactical rule alone captured
    #: **0 of SPY's 20 best sessions** (+64.4% of return forgone) while avoiding
    #: 20 of 20 worst — it swaps one tail for the other almost exactly, then pays
    #: costs. A permanent core participates in every up day; the tactical sleeve
    #: adds and removes risk around it. Set 0.0 for a pure tactical book.
    core_weight: float = 0.0
    #: Allow re-entry at a lower score when the vol curve says the panic is passing.
    #:
    #: Exits should be fast and re-entries faster: best days cluster within days
    #: of worst days (45% of the top 20 fell within a week of a bottom-20 day),
    #: so waiting for the 50-day to be reclaimed guarantees you miss them.
    fast_reentry: bool = True
    reentry_score: float = 45.0
    #: Cap on this position as a fraction of equity.
    #:
    #: This intentionally overrides ``RiskConfig.max_position_pct``. That 10% cap
    #: exists to stop one name dominating a *diversified book*; applying it to a
    #: single-instrument backtest would silently leave 90% of the account in cash
    #: and make the result incomparable to buy-and-hold. In single-name mode the
    #: ATR risk rule is the real constraint — with SPY's ~1% ATR and a 2.5-ATR
    #: stop, risking 0.75% of equity naturally sizes to roughly 29% of it.
    position_cap_pct: float = 1.0


@dataclass
class BacktestResult:
    equity_curve: pd.Series
    trades: pd.DataFrame
    daily: pd.DataFrame
    stats: dict = field(default_factory=dict)

    def summary(self) -> str:
        s = self.stats
        lines = [
            f"  bars traded      {s.get('bars', 0)} ({s.get('start', '?')} -> {s.get('end', '?')})",
            f"  total return     {s.get('total_return_pct', float('nan')):.2f}%",
            f"  CAGR             {s.get('cagr_pct', float('nan')):.2f}%",
            f"  volatility       {s.get('vol_pct', float('nan')):.2f}%",
            f"  Sharpe           {s.get('sharpe', float('nan')):.2f}",
            f"  Sortino          {s.get('sortino', float('nan')):.2f}",
            f"  max drawdown     {s.get('max_drawdown_pct', float('nan')):.2f}%",
            f"  Calmar           {s.get('calmar', float('nan')):.2f}",
            f"  time in market   {s.get('exposure_pct', float('nan')):.1f}%",
            f"  trades           {s.get('trades', 0)}  (win rate {s.get('win_rate_pct', float('nan')):.1f}%)",
            f"  profit factor    {s.get('profit_factor', float('nan')):.2f}",
            f"  avg hold         {s.get('avg_hold_days', float('nan')):.1f} days",
        ]
        return "\n".join(lines)


def _commission(shares: float, cfg: BacktestConfig) -> float:
    return max(cfg.min_commission, abs(shares) * cfg.commission_per_share) if shares else 0.0


def run_backtest(
    enriched: pd.DataFrame,
    regime: pd.Series | None = None,
    cfg: BacktestConfig | None = None,
    signal_cfg: SignalConfig | None = None,
    risk_cfg: RiskConfig | None = None,
    symbol: str = "ASSET",
    reentry_signal: pd.Series | None = None,
) -> BacktestResult:
    """Run the model over an enriched OHLCV frame.

    ``reentry_signal`` is an optional boolean series (typically the regime's
    ``term_normalizing`` column) that unlocks the lower ``reentry_score``
    threshold when ``cfg.fast_reentry`` is set.
    """
    cfg = cfg or BacktestConfig()
    risk_cfg = risk_cfg or RiskConfig()

    scored = score_frame(enriched, cfg=signal_cfg, regime=regime)

    # Warm-up: first bar where everything the model reads is available.
    needed = ["sma50", "sma200", "atr14", "adx", "rsi14", "supertrend_dir", "bb_pct_b"]
    ready = enriched[needed].notna().all(axis=1) & scored["score"].notna()
    if not ready.any():
        raise ValueError(f"{symbol}: not enough history to trade — need >200 sessions")
    start_idx = int(np.argmax(ready.to_numpy()))

    dates = enriched.index
    op = enriched["open"].to_numpy(float)
    hi = enriched["high"].to_numpy(float)
    lo = enriched["low"].to_numpy(float)
    cl = enriched["close"].to_numpy(float)
    atr = enriched["atr14"].to_numpy(float)
    sma50 = enriched["sma50"].to_numpy(float)
    st_dir = enriched["supertrend_dir"].to_numpy(float)
    score = scored["score"].to_numpy(float)
    exposure_cap = (
        regime.reindex(enriched.index).ffill().to_numpy(float)
        if regime is not None
        else np.ones(len(enriched))
    )

    n = len(enriched)
    if reentry_signal is not None:
        reentry = reentry_signal.reindex(enriched.index).fillna(0).to_numpy(float) > 0
    else:
        reentry = np.zeros(n, dtype=bool)

    cash = cfg.initial_equity
    shares = 0.0
    core_shares = 0.0
    entry_price = stop = high_water = 0.0
    entry_date = None
    entry_bar = -1
    pending: tuple[str, float] | None = None  # (side, stop_at_entry)

    equity = np.full(n, np.nan)
    position_flag = np.zeros(n)
    stop_track = np.full(n, np.nan)
    trades: list[dict] = []

    # Buy the permanent core once, at the first tradeable open, and never sell it.
    if cfg.core_weight > 0:
        core_fill = op[start_idx] * (1 + cfg.slippage_bps / 1e4)
        core_shares = float(np.floor(cfg.initial_equity * cfg.core_weight / core_fill))
        if core_shares > 0:
            cash -= core_shares * core_fill + _commission(core_shares, cfg)

    for i in range(start_idx, n):
        # ---- 1. execute anything decided on the previous close -----------
        if pending is not None:
            action = pending[0]
            fill = op[i] * (1 + cfg.slippage_bps / 1e4) if action == "ENTER" else op[i] * (1 - cfg.slippage_bps / 1e4)

            if action == "ENTER" and shares == 0:
                per_share_risk = risk_cfg.atr_stop_mult * atr[i - 1]
                if per_share_risk > 0:
                    # Risk is measured against the whole account, but the tactical
                    # sleeve can only ever deploy the cash the core left behind.
                    equity_now = cash + core_shares * cl[i - 1]
                    budget = equity_now * risk_cfg.risk_pct
                    qty = budget / per_share_risk
                    cap_qty = (equity_now * cfg.position_cap_pct * min(1.0, exposure_cap[i - 1])) / fill
                    cap_cash = cash / fill
                    qty = float(np.floor(min(qty, cap_qty, cap_cash)))
                    if qty > 0 and qty * fill >= risk_cfg.min_notional:
                        fee = _commission(qty, cfg)
                        cash -= qty * fill + fee
                        shares = qty
                        entry_price = fill
                        stop = fill - per_share_risk
                        high_water = hi[i]
                        entry_date = dates[i]
                        entry_bar = i

            elif action == "EXIT" and shares > 0:
                fee = _commission(shares, cfg)
                cash += shares * fill - fee
                trades.append(
                    {
                        "symbol": symbol,
                        "entry_date": entry_date,
                        "exit_date": dates[i],
                        "entry": entry_price,
                        "exit": fill,
                        "shares": shares,
                        "pnl": shares * (fill - entry_price) - fee,
                        "return_pct": 100.0 * (fill / entry_price - 1.0),
                        "hold_days": int(i - entry_bar),
                        "reason": pending[1],
                    }
                )
                shares = 0.0
                entry_price = stop = high_water = 0.0
                entry_bar = -1
            pending = None

        # ---- 2. intrabar stop check --------------------------------------
        if shares > 0 and cfg.use_trailing_stop:
            high_water = max(high_water, hi[i])
            trail = high_water - risk_cfg.atr_trail_mult * atr[i]
            stop = max(stop, trail)  # ratchets up only

        if shares > 0 and lo[i] <= stop:
            # Gap-aware: a session that opens below the stop fills at the open.
            fill = min(op[i], stop) * (1 - cfg.slippage_bps / 1e4)
            fee = _commission(shares, cfg)
            cash += shares * fill - fee
            trades.append(
                {
                    "symbol": symbol,
                    "entry_date": entry_date,
                    "exit_date": dates[i],
                    "entry": entry_price,
                    "exit": fill,
                    "shares": shares,
                    "pnl": shares * (fill - entry_price) - fee,
                    "return_pct": 100.0 * (fill / entry_price - 1.0),
                    "hold_days": int(i - entry_bar),
                    "reason": "stop",
                }
            )
            shares = 0.0
            entry_price = stop = high_water = 0.0
            entry_bar = -1

        # ---- 3. mark to market -------------------------------------------
        equity[i] = cash + (shares + core_shares) * cl[i]
        # "In market" means exposed to the asset at all — the core counts.
        position_flag[i] = 1.0 if (shares > 0 or core_shares > 0) else 0.0
        stop_track[i] = stop if shares > 0 else np.nan

        # ---- 4. decide for tomorrow's open -------------------------------
        if i + 1 >= n:
            continue

        if shares > 0:
            held = i - entry_bar
            reason = None
            if cfg.exit_on_sma50_break and cl[i] < sma50[i]:
                reason = "sma50_break"
            elif cfg.exit_on_supertrend_flip and st_dir[i] < 0:
                reason = "supertrend_flip"
            elif score[i] < cfg.exit_score and held >= cfg.min_hold_days:
                reason = "score_decay"
            if reason:
                pending = ("EXIT", reason)
        else:
            threshold = cfg.entry_score
            if cfg.fast_reentry and reentry[i]:
                # The vol curve has un-inverted: take the lower bar so the sleeve
                # is back on before the trend indicators have finished rebuilding.
                threshold = min(threshold, cfg.reentry_score)
            if score[i] >= threshold and exposure_cap[i] > 0:
                pending = ("ENTER", "signal")

    # ---- close any open position at the final close -----------------------
    if shares > 0:
        fill = cl[n - 1]
        fee = _commission(shares, cfg)
        cash += shares * fill - fee
        trades.append(
            {
                "symbol": symbol,
                "entry_date": entry_date,
                "exit_date": dates[n - 1],
                "entry": entry_price,
                "exit": fill,
                "shares": shares,
                "pnl": shares * (fill - entry_price) - fee,
                "return_pct": 100.0 * (fill / entry_price - 1.0),
                "hold_days": int(n - 1 - entry_bar),
                "reason": "end_of_data",
            }
        )
        # The core is never sold — it stays marked to the final close.
        equity[n - 1] = cash + core_shares * fill

    eq = pd.Series(equity, index=dates, name="equity").dropna()
    daily = pd.DataFrame(
        {
            "equity": pd.Series(equity, index=dates),
            "in_market": pd.Series(position_flag, index=dates),
            "stop": pd.Series(stop_track, index=dates),
            "score": scored["score"],
            "close": enriched["close"],
        }
    ).loc[eq.index]

    from .metrics import compute_stats

    trades_df = pd.DataFrame(trades)
    stats = compute_stats(eq, trades_df, daily["in_market"])
    return BacktestResult(equity_curve=eq, trades=trades_df, daily=daily, stats=stats)
