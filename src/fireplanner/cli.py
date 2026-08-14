"""Command line entry point.

    fireplanner regime                     # today's gate
    fireplanner scan SPY NVDA ZS           # score a list of names
    fireplanner plan SPY --equity 77674    # size a position
    fireplanner backtest SPY               # strategy vs buy-and-hold
    fireplanner dashboard -o out.html      # the full page

``--provider gateway`` swaps the offline snapshot for a live TWS/IB Gateway
connection; everything else is identical.
"""

from __future__ import annotations

import argparse
import sys

import pandas as pd

from . import indicators as ind
from .backtest import buy_and_hold_stats, run_backtest
from .data import CachedProvider, get_provider
from .risk import RiskConfig, plan_position
from .signals import latest_signal, score_frame


def _provider(args):
    kwargs = {}
    if args.provider == "snapshot":
        kwargs["root"] = args.snapshots
    elif args.provider in {"gateway", "tws", "ib"}:
        kwargs.update(host=args.host, port=args.port, client_id=args.client_id)
    p = get_provider(args.provider, **kwargs)
    if args.provider != "snapshot" and args.cache:
        p = CachedProvider(p, throttle_seconds=0.3)
    return p


def _regime(provider, args):
    bench = provider.history(args.benchmark, lookback_days=args.lookback)
    series = {}
    for key, sym in [("vix", args.vol_symbol), ("breadth", args.breadth_symbol), ("vix3m", args.term_symbol)]:
        try:
            series[key] = provider.history(sym, lookback_days=args.lookback)["close"]
        except Exception:
            print(f"  (note: {sym} unavailable)", file=sys.stderr)
    regime = ind.compute_regime(
        bench["close"],
        vix=series.get("vix"),
        equal_weight=series.get("breadth"),
        vix3m=series.get("vix3m"),
    )
    return bench, regime


def cmd_regime(args) -> int:
    provider = _provider(args)
    _, regime = _regime(provider, args)
    r = ind.latest_regime(regime)
    print(f"\n  {args.benchmark} regime as of {r.date.date()}")
    print(f"  {'-' * 46}")
    print(f"  {r.label.upper():<16} score {r.score * 100:5.1f}/100")
    print(f"  max deployable equity: {r.exposure_cap * 100:.0f}%\n")
    for k, v in r.components.items():
        bar = "#" * int(round((v or 0) * 20))
        print(f"    {k:<10} {(v or 0) * 100:5.1f}  {bar}")
    print()
    return 0


def cmd_scan(args) -> int:
    provider = _provider(args)
    _, regime = _regime(provider, args)

    rows = []
    for sym in args.symbols:
        try:
            bars = provider.history(sym, lookback_days=args.lookback)
            e = ind.enrich(bars)
            sig = latest_signal(sym, score_frame(e, regime=regime["score"]), e)
        except Exception as exc:
            print(f"  {sym}: {exc}", file=sys.stderr)
            continue
        d = sig.detail
        rows.append(
            {
                "symbol": sym,
                "action": sig.action,
                "score": round(sig.score, 1),
                "close": round(sig.close, 2),
                "rsi": round(d.get("rsi14", float("nan")), 1),
                "adx": round(d.get("adx", float("nan")), 1),
                "atr%": round(d.get("natr14", float("nan")), 2),
                "vs50d": round(d.get("pct_from_sma50", float("nan")), 1),
                "vs52wh": round(d.get("dist_52w_high", float("nan")), 1),
            }
        )

    if not rows:
        print("no symbols scored", file=sys.stderr)
        return 1
    df = pd.DataFrame(rows).sort_values("score", ascending=False)
    print()
    print(df.to_string(index=False))
    print()
    return 0


def cmd_plan(args) -> int:
    provider = _provider(args)
    _, regime = _regime(provider, args)
    r = ind.latest_regime(regime)

    e = ind.enrich(provider.history(args.symbol, lookback_days=args.lookback))
    last = e.dropna(subset=["atr14"]).iloc[-1]
    plan = plan_position(
        args.symbol,
        equity=args.equity,
        entry=float(last["close"]),
        atr=float(last["atr14"]),
        cfg=RiskConfig(risk_pct=args.risk_pct, atr_stop_mult=args.atr_mult),
        exposure_cap=r.exposure_cap,
        open_heat=args.open_heat,
    )
    d = plan.as_dict()
    print(f"\n  {args.symbol} position plan   (regime: {r.label}, cap {r.exposure_cap * 100:.0f}%)")
    print(f"  {'-' * 52}")
    print(f"    entry            {d['entry']:>12,.2f}")
    print(f"    stop             {d['stop']:>12,.2f}   ({100 * (1 - d['stop'] / d['entry']):.2f}% away)")
    print(f"    shares           {d['shares']:>12,.0f}")
    print(f"    notional         {d['notional']:>12,.2f}")
    print(f"    risk if stopped  {d['risk_dollars']:>12,.2f}   ({d['risk_pct_equity'] * 100:.2f}% of equity)")
    print(f"    weight           {d['weight'] * 100:>11.1f}%")
    print(f"    targets          " + "  ".join(f"{k} {v:,.2f}" for k, v in d["r_multiple_targets"].items()))
    print(f"    limited by       {d['limited_by']}\n")
    return 0


def cmd_backtest(args) -> int:
    from .backtest import BacktestConfig, best_days_analysis

    provider = _provider(args)
    _, regime = _regime(provider, args)
    bars = provider.history(args.symbol, lookback_days=args.lookback)
    e = ind.enrich(bars)
    reentry = regime["term_normalizing"] if "term_normalizing" in regime.columns else None
    cfg = BacktestConfig(core_weight=args.core_weight)
    result = run_backtest(e, regime=regime["score"], symbol=args.symbol, cfg=cfg, reentry_signal=reentry)
    bh = buy_and_hold_stats(bars["close"].reindex(result.equity_curve.index).dropna())

    label = f"core {args.core_weight * 100:.0f}% + tactical" if args.core_weight else "tactical only"
    print(f"\n  {args.symbol} — regime-gated swing model ({label})")
    print(f"  {'-' * 52}")
    print(result.summary())

    bd = best_days_analysis(bars["close"], result.daily["in_market"])
    print(f"\n  best/worst day capture")
    print(f"    best {bd['n']} days held    {bd['best_captured']}/{bd['n']}"
          f"   upside forgone {bd['best_forgone_pct']:+.1f}%")
    print(f"    worst {bd['n']} days dodged {bd['worst_avoided']}/{bd['n']}"
          f"   downside avoided {bd['worst_avoided_pct']:+.1f}%")
    print(f"\n  buy & hold, same window")
    print(f"    CAGR {bh['cagr_pct']:.2f}%   vol {bh['vol_pct']:.2f}%   "
          f"Sharpe {bh['sharpe']:.2f}   maxDD {bh['max_drawdown_pct']:.2f}%\n")
    if args.trades and not result.trades.empty:
        print(result.trades.to_string(index=False))
        print()
    return 0


def cmd_desk(args) -> int:
    from .dashboard import build_payload, render_desk
    from .dashboard.build import add_decision
    from .signals import AllocationPolicy

    provider = _provider(args)
    payload = build_payload(
        provider, benchmark=args.benchmark, vol_symbol=args.vol_symbol,
        breadth_symbol=args.breadth_symbol, term_symbol=args.term_symbol,
        watchlist=[args.benchmark], lookback_days=args.lookback,
        equity=args.equity if args.equity > 0 else None,
    )
    policy = AllocationPolicy(core_weight=args.core, sleeve_max=1.0 - args.core)
    payload = add_decision(
        payload, policy=policy,
        current_weight=args.current if args.current >= 0 else None,
        equity=args.equity if args.equity > 0 else None,
    )
    d = payload.decision
    print(f"\n  {args.benchmark} — {d['date']}")
    print(f"  {'-' * 56}")
    print(f"    ACTION           {d['action']}")
    print(f"    target           {d['target_pct'] * 100:.0f}% of equity")
    print(f"    current          {d['current_pct'] * 100:.0f}%  ({payload.exposure_basis.get('basis')})")
    if d["action"] not in {"HOLD", "WAIT"}:
        print(f"    trade            {d['shares_delta']:+,.0f} shares  (${d['dollars_delta']:+,.0f})")
    print(f"    regime           {d['regime_label']}  ·  score {d['score']:.0f}")
    print(f"\n    {d['reason']}\n")
    rel = payload.reliability
    print(f"    reliability      {rel['changes_per_year']:.1f} changes/yr, "
          f"{rel['reversal_pct']:.0f}% reversed within 10 sessions")
    if args.output:
        with open(args.output, "w") as f:
            f.write(render_desk(payload))
        print(f"\n    wrote {args.output}")
    return 0


def cmd_notify(args) -> int:
    from .dashboard import build_payload
    from .dashboard.build import add_decision
    from .notify import TelegramNotifier, notify_if_changed
    from .signals import AllocationPolicy

    provider = _provider(args)
    payload = build_payload(
        provider, benchmark=args.benchmark, vol_symbol=args.vol_symbol,
        breadth_symbol=args.breadth_symbol, term_symbol=args.term_symbol,
        watchlist=[args.benchmark], lookback_days=args.lookback,
    )
    payload = add_decision(payload, policy=AllocationPolicy(core_weight=args.core,
                                                            sleeve_max=1.0 - args.core))
    notifier = TelegramNotifier(
        dry_run=args.dry_run,
        env_file=args.env_file or None,
        thread_id=args.thread_id or None,
        source=args.source,
    )
    result = notify_if_changed(payload, notifier=notifier,
                               state_path=args.state, force=args.force)

    print()
    if result.events:
        for ev in result.events:
            print(f"  [{ev.urgency:6s}] {ev.headline}")
    else:
        print("  no change since the last notification")
    print(f"\n  sent: {result.sent}   ({result.reason})")
    # Show exactly what went over the wire (which includes the source label),
    # falling back to the rendered body when nothing was handed to the client.
    preview = notifier.sent[-1] if notifier.sent else result.message
    if preview and (args.dry_run or not result.sent):
        print("\n  --- message as sent ---")
        for line in preview.splitlines():
            print(f"  {line}")
        print()
    return 0


def cmd_publish(args) -> int:
    from .dashboard import build_payload
    from .dashboard.build import add_decision
    from .publish import publish_site
    from .signals import AllocationPolicy

    provider = _provider(args)
    payload = build_payload(
        provider, benchmark=args.benchmark, vol_symbol=args.vol_symbol,
        breadth_symbol=args.breadth_symbol, term_symbol=args.term_symbol,
        watchlist=args.symbols or None, lookback_days=args.lookback,
        equity=args.equity if args.equity > 0 else None,
    )
    payload = add_decision(payload, policy=AllocationPolicy(core_weight=args.core,
                                                            sleeve_max=1.0 - args.core))
    manifest = publish_site(payload, out_dir=args.output, redact=args.redact,
                            single=args.single)
    shape = "one page" if manifest["single"] else "three files"
    print(f"\n  wrote {manifest['out_dir']}/  ({shape}, redacted: {manifest['redacted']})")
    for name, size in manifest["files"].items():
        print(f"    {name:20s} {size:>9,} bytes")
    for name in manifest["removed"]:
        print(f"    {name:20s} {'removed':>9s}  (stale from the previous layout)")
    d = payload.decision
    print(f"\n  signal: {d['action']}  target {d['target_pct'] * 100:.0f}%  as of {d['date']}\n")
    return 0


def cmd_dashboard(args) -> int:
    from .dashboard import build_payload, render_html

    provider = _provider(args)
    payload = build_payload(
        provider,
        benchmark=args.benchmark,
        vol_symbol=args.vol_symbol,
        breadth_symbol=args.breadth_symbol,
        watchlist=args.symbols or None,
        lookback_days=args.lookback,
        equity=args.equity if args.equity > 0 else None,
    )
    out = render_html(payload)
    with open(args.output, "w") as f:
        f.write(out)
    print(f"wrote {args.output}  ({len(out):,} bytes)")
    for n in payload.notes:
        print(f"  note: {n}")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="fireplanner", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--provider", default="snapshot", choices=["snapshot", "gateway", "web"])
    ap.add_argument("--snapshots", default="data/snapshots")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=4002, help="7496 TWS live, 7497 TWS paper, 4001/4002 Gateway")
    ap.add_argument("--client-id", type=int, default=17)
    ap.add_argument("--cache", action="store_true", help="cache bars on disk between runs")
    ap.add_argument("--benchmark", default="SPY")
    ap.add_argument("--vol-symbol", default="VIX")
    ap.add_argument("--breadth-symbol", default="RSP")
    ap.add_argument("--term-symbol", default="VIX3M", help="3-month VIX, for curve shape and fast re-entry")
    ap.add_argument("--lookback", type=int, default=1260)

    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("regime", help="print today's market gate").set_defaults(func=cmd_regime)

    s = sub.add_parser("scan", help="score symbols")
    s.add_argument("symbols", nargs="+")
    s.set_defaults(func=cmd_scan)

    s = sub.add_parser("plan", help="size a position")
    s.add_argument("symbol")
    s.add_argument("--equity", type=float, required=True)
    s.add_argument("--risk-pct", type=float, default=0.0075)
    s.add_argument("--atr-mult", type=float, default=2.5)
    s.add_argument("--open-heat", type=float, default=0.0)
    s.set_defaults(func=cmd_plan)

    s = sub.add_parser("backtest", help="strategy vs buy-and-hold")
    s.add_argument("symbol")
    s.add_argument("--trades", action="store_true")
    s.add_argument("--core-weight", type=float, default=0.0,
                   help="fraction held permanently and never sold (0.4 is a reasonable start)")
    s.set_defaults(func=cmd_backtest)

    s = sub.add_parser("desk", help="today's enter/exit instruction")
    s.add_argument("-o", "--output", default="", help="also write the HTML signal desk")
    s.add_argument("--equity", type=float, default=0.0)
    s.add_argument("--current", type=float, default=-1.0,
                   help="current equity exposure as a fraction (default: inferred from the account)")
    s.add_argument("--core", type=float, default=0.40,
                   help="permanent core allocation, never sold")
    s.set_defaults(func=cmd_desk)

    s = sub.add_parser("notify", help="push the signal to Telegram when it changes")
    s.add_argument("--core", type=float, default=0.40)
    s.add_argument("--dry-run", action="store_true", help="render the message without sending")
    s.add_argument("--force", action="store_true", help="send even if nothing changed")
    s.add_argument("--state", default=".cache/notify_state.json",
                   help="where the last-notified state is kept")
    s.add_argument("--env-file", default="",
                   help="read TELEGRAM_* from an existing env file, e.g. another "
                        "project's .env, so the token lives in one place only")
    s.add_argument("--thread-id", default="",
                   help="Telegram forum topic id, to keep these out of a shared group's main feed")
    s.add_argument("--source", default="FirePlanner",
                   help="label prefixed to every message; matters when one bot serves several systems")
    s.set_defaults(func=cmd_notify)

    s = sub.add_parser("publish", help="write the static site (index + both pages)")
    s.add_argument("symbols", nargs="*")
    s.add_argument("-o", "--output", default="site")
    s.add_argument("--equity", type=float, default=0.0)
    s.add_argument("--core", type=float, default=0.40)
    s.add_argument("--redact", action="store_true",
                   help="strip balances, positions and trade sizes - required for public hosting")
    s.add_argument("--single", action="store_true",
                   help="write one index.html holding both pages behind a tab strip, "
                        "instead of three linked files")
    s.set_defaults(func=cmd_publish)

    s = sub.add_parser("dashboard", help="render the HTML dashboard")
    s.add_argument("symbols", nargs="*")
    s.add_argument("-o", "--output", default="dashboard.html")
    s.add_argument("--equity", type=float, default=0.0)
    s.set_defaults(func=cmd_dashboard)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
