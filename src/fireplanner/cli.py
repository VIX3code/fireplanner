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


# --------------------------------------------------------------------------
# trade management
# --------------------------------------------------------------------------

def _trade_service(args):
    from .notify import TelegramNotifier
    from .trading import Journal, TradingService, load_rules, load_settings
    from .trading.ib_broker import IBBroker

    rules = load_rules(args.config)
    settings = load_settings(args.config)
    journal = Journal(args.journal or settings["journal"])
    broker = IBBroker(host=args.host, port=args.port,
                      client_id=args.trade_client_id or settings["guardian"]["client_id"],
                      allow_live=bool(settings["allow_live"]), rules=rules, lot_sizes=settings["lot_sizes"])
    notifier = None
    if not args.no_notify:
        n = TelegramNotifier(env_file=args.env_file or None, source="Swing Desk")
        notifier = n if n.configured else None
    service = TradingService(broker, journal, rules,
                             orders_enabled=bool(settings["enabled"]) and not args.dry_run,
                             notifier=notifier, seed_watchlist=settings["watchlist"],
                             lot_sizes=settings["lot_sizes"], weather_proxies=settings["weather"],
                             breadth_baskets=settings["breadth"].get("baskets") or None,
                             breadth_per_cycle=int(settings["breadth"].get("per_cycle", 8)))
    return service, settings


def _print_book(state: dict) -> None:
    t = state["totals"]
    mode = f"{state['mode']} · {'orders ON' if state['orders_enabled'] else 'dry run'}"
    print(f"\n  Swing Desk  ({mode})  {state['generated_at']}")
    print(f"  {t['slots_used']}/{t['max_slots']} slots · invested ${t['invested_usd']:,.0f} · "
          f"P/L ${t['pl_usd']:+,.0f} · lost if every stop hits ${t['risk_usd']:,.0f} · "
          f"{t['protected']}/{t['open']} protected\n")
    for b in state["buckets"]:
        print(f"    {b['name']:<9} {b['n']:>2}/{b['cap']:<2}  money {b['money_share'] * 100:5.1f}%   "
              f"daily swing {b['swing_share'] * 100:5.1f}%")
    if state.get("weather"):
        print("\n    weather  " + "  ".join(f"{m}: {w['label'] or '—'}" for m, w in state["weather"].items()))
    br = state.get("breaker") or {}
    if br.get("paused"):
        print("    BUYS PAUSED: " + " ".join(br["reasons"]))
    if state["positions"]:
        print()
        print(f"    {'stock':<12}{'type':<9}{'qty':>6}{'entry':>11}{'last':>11}{'stop':>11}{'target':>11}  state")
        for p in state["positions"]:
            d = p["decimals"]
            print(f"    {p['key']:<12}{p['bucket']:<9}{p['qty']:>6}{p['entry']:>11,.{d}f}{p['last']:>11,.{d}f}"
                  f"{p['stop']:>11,.{d}f}{p['target']:>11,.{d}f}  {p['stop_state']}"
                  f"{'' if p['protected'] else '  NO STOP'}")
    print()


def cmd_trade_run(args) -> int:
    import os

    from .trading.server import serve

    service, settings = _trade_service(args)
    dash = settings["dashboard"]
    token = os.environ.get("FIREPLANNER_DASH_TOKEN", "")
    host = args.dash_host or dash["host"]
    port = args.dash_port or int(dash["port"])
    httpd = serve(service, host=host, port=port, token=token)
    b = service.broker
    print(f"\n  Swing Desk: {b.mode.upper()} account on {args.host}:{args.port}, "
          f"{'orders ON' if service.orders_enabled else 'DRY RUN (trading.enabled is false)'}")
    print(f"  dashboard  http://{host}:{port}/{'?token=…' if token else ''}")
    print("  Ctrl-C to stop. Stops and targets already placed stay live at IBKR.\n")
    try:
        service.run(interval=float(settings["guardian"]["interval_seconds"]))
    except KeyboardInterrupt:
        pass
    finally:
        httpd.shutdown()
        b.disconnect()
    return 0


def cmd_trade_once(args) -> int:
    service, _ = _trade_service(args)
    try:
        state = service.cycle()
    finally:
        service.broker.disconnect()
    _print_book(state)
    for e in reversed(state["events"][:12]):
        print(f"    [{e['level']}] {e['message']}")
    print()
    return 0


def cmd_trade_check(args) -> int:
    service, _ = _trade_service(args)
    try:
        service.cycle()
        res = service.check(args.symbol, bucket=args.bucket, limit=args.limit)
    finally:
        service.broker.disconnect()
    p, inst = res["plan"], res["instrument"]
    d = inst["decimals"]
    print(f"\n  {inst['key']}  {inst['name']}  ->  {res['verdict'].upper()}")
    print(f"  buy {p['qty']} @ {p['limit']:,.{d}f} {inst['currency']} (~${p['cost_usd']:,.0f}, "
          f"loses ${p['max_loss_usd']:,.0f} at the stop) · "
          f"stop {p['stop']:,.{d}f} (-{p['stop_pct'] * 100:.1f}%, {p['stop_basis']}) · "
          f"target {p['target']:,.{d}f} (+{p['target_pct'] * 100:.0f}%) on {p['target_qty']}")
    for c in res["checks"]:
        mark = {"good": "ok ", "warn": "!! ", "crit": "XX ", "info": " i "}[c["level"]]
        print(f"    {mark} {c['title']}: {c['detail']}")
    print()
    return 0 if res["verdict"] != "blocked" else 2


def cmd_trade_demo(args) -> int:
    from .trading.demo import build_demo
    from .trading.server import render_dashboard

    _, state = build_demo()
    html = render_dashboard(state, live=False)
    with open(args.output, "w") as f:
        f.write(html)
    print(f"wrote {args.output}  ({len(html):,} bytes, simulated data)")
    if args.print:
        _print_book(state)
    return 0


def cmd_trade_doctor(args) -> int:
    """Check the IBKR connection is ready for the Swing Desk. Places no orders."""
    from .trading import load_rules, load_settings
    from .trading.doctor import run_doctor
    from .trading.ib_broker import IBBroker

    rules, settings = load_rules(args.config), load_settings(args.config)
    broker = IBBroker(host=args.host, port=args.port,
                      client_id=args.trade_client_id or settings["guardian"]["client_id"] + 1,
                      allow_live=bool(settings["allow_live"]), rules=rules, lot_sizes=settings["lot_sizes"])
    try:
        findings = run_doctor(broker, rules, settings)
    finally:
        broker.disconnect()
    mark = {"good": "ok ", "warn": "!! ", "crit": "XX ", "info": " i "}
    print(f"\n  Swing Desk doctor: IBKR at {args.host}:{args.port}\n")
    for f in findings:
        print(f"  {mark[f.level]} {f.title}")
        if f.detail:
            print(f"       {f.detail}")
    bad = [f for f in findings if f.level == "crit"]
    print(f"\n  {'Ready.' if not bad else f'{len(bad)} problem(s) to fix before running the guardian.'}\n")
    return 1 if bad else 0


def cmd_trade_snapshot(args) -> int:
    """One self-contained HTML file of your real dashboard, to host behind a password."""
    import os

    from .trading.server import fetch_state, render_dashboard

    if args.direct:
        service, _ = _trade_service(args)
        service.orders_enabled = False           # a snapshot never sends an order
        try:
            for _ in range(3):
                state = service.cycle()
        finally:
            service.broker.disconnect()
    else:
        from .trading import load_settings

        dash = load_settings(args.config)["dashboard"]
        url = args.url or f"http://{dash['host']}:{dash['port']}"
        try:
            state = fetch_state(url, os.environ.get("FIREPLANNER_DASH_TOKEN", ""))
        except Exception as exc:
            print(f"  can't read the running dashboard at {url}: {exc}\n"
                  f"  start it with `fireplanner trade run`, or use --direct for a one-off read from IBKR.",
                  file=sys.stderr)
            return 1
    html = render_dashboard(state, live=False)
    out = os.path.abspath(args.output)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        f.write(html)
    t = state["totals"]
    print(f"wrote {args.output}  ({len(html):,} bytes, {t['open']} positions, {state['mode']} account)")
    print("  This page shows your positions and account figures. Host it behind a password, never publicly.")
    return 0


def cmd_trade_watch(args) -> int:
    from .trading import Journal, import_watchlist, load_settings, parse_symbol

    journal = Journal(args.journal or load_settings(args.config)["journal"])
    if args.action == "add":
        for text in args.items:
            sym, mkt = parse_symbol(text)
            journal.watch(f"{sym}:{mkt}", sym, mkt, source="manual", lot_size=args.lot)
            print(f"  added {sym}:{mkt}")
    elif args.action == "rm":
        for key in args.items:
            sym, mkt = parse_symbol(key)
            print(f"  {'removed' if journal.unwatch(f'{sym}:{mkt}') else 'not found:'} {sym}:{mkt}")
    elif args.action == "import":
        for path in args.items:
            with open(path, encoding="utf-8", errors="replace") as f:
                added = import_watchlist(journal, f.read())
            print(f"  imported {len(added)} from {path}: {', '.join(added)}")
    for w in journal.watchlist():
        lot = f"  lot {w['lot_size']}" if w["lot_size"] else ""
        print(f"    {w['key']:<14} {w['source']:<8}{lot}")
    return 0


def cmd_trade_bucket(args) -> int:
    from .trading import Journal, load_rules, load_settings, parse_symbol

    rules = load_rules(args.config)
    b = rules.bucket(args.bucket)
    sym, mkt = parse_symbol(args.symbol)
    journal = Journal(args.journal or load_settings(args.config)["journal"])
    journal.confirm_bucket(f"{sym}:{mkt}", b.id)
    print(f"  {sym}:{mkt} confirmed as {b.name} (target +{b.target:.0%}, cap {b.cap}). "
          f"A running guardian updates an open position's target on its next cycle.")
    return 0


def cmd_trade_unlock(args) -> int:
    from .trading import Journal, load_settings, parse_symbol

    journal = Journal(args.journal or load_settings(args.config)["journal"])
    sym, mkt = parse_symbol(args.symbol)
    journal.unlock(f"{sym}:{mkt}")
    print(f"  {sym}:{mkt} unlocked: two tries again.")
    return 0


def cmd_trade_earnings(args) -> int:
    from .trading import Journal, load_settings, parse_symbol
    from .trading.earnings import parse_date

    journal = Journal(args.journal or load_settings(args.config)["journal"])
    if args.symbol:
        sym, mkt = parse_symbol(args.symbol)
        d = None if args.date in (None, "", "clear") else parse_date(args.date)
        journal.set_earnings(f"{sym}:{mkt}", d, source="manual")
    for key, e in sorted(journal.earnings().items(), key=lambda kv: (kv[1]["date"] is None, kv[1]["date"] or "")):
        print(f"    {key:<14} {e['date'] or '—'}  ({e['source']})")
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

    t = sub.add_parser("trade", help="manage positions: stops, targets, buckets, the live dashboard")
    tsub = t.add_subparsers(dest="trade_cmd", required=True)

    def trade_parser(name, help_text, func):
        p = tsub.add_parser(name, help=help_text)
        p.add_argument("--config", default="config/config.yaml")
        p.add_argument("--journal", default="", help="journal file (default: trading.journal in the config)")
        p.set_defaults(func=func)
        return p

    def broker_opts(p):
        p.add_argument("--dry-run", action="store_true", help="log orders instead of sending them, whatever the config says")
        p.add_argument("--trade-client-id", type=int, default=0, help="IBKR client id for the guardian (default: config)")
        p.add_argument("--env-file", default="", help="read TELEGRAM_* from this env file")
        p.add_argument("--no-notify", action="store_true", help="no Telegram messages")

    p = trade_parser("run", "run the guardian and the live dashboard", cmd_trade_run)
    broker_opts(p)
    p.add_argument("--dash-host", default="")
    p.add_argument("--dash-port", type=int, default=0)

    broker_opts(trade_parser("once", "one guardian cycle, then print the book", cmd_trade_once))

    p = trade_parser("check", "run the pre-trade check for one stock", cmd_trade_check)
    broker_opts(p)
    p.add_argument("symbol", help="NVDA, 700:HK, D05:SG, 7203:JP, HSBA:LN ...")
    p.add_argument("--bucket", choices=["steady", "core", "volatile"])
    p.add_argument("--limit", type=float)

    p = trade_parser("demo", "write the dashboard for a simulated book (no IBKR needed)", cmd_trade_demo)
    p.add_argument("-o", "--output", default="swing_desk.html")
    p.add_argument("--print", action="store_true", help="also print the book")

    p = trade_parser("doctor", "check the IBKR connection is ready (places no orders)", cmd_trade_doctor)
    p.add_argument("--trade-client-id", type=int, default=0,
                   help="client id for the check (default: the guardian's + 1, so it can run alongside)")

    p = trade_parser("snapshot", "write your real dashboard as one HTML file to host", cmd_trade_snapshot)
    broker_opts(p)
    p.add_argument("-o", "--output", default="site/swing-desk/index.html")
    p.add_argument("--url", default="", help="the running dashboard (default: trading.dashboard in the config)")
    p.add_argument("--direct", action="store_true",
                   help="read IBKR directly instead of a running dashboard (use a --trade-client-id the guardian isn't using)")

    p = trade_parser("watch", "edit the watchlist", cmd_trade_watch)
    p.add_argument("action", choices=["add", "rm", "import", "list"])
    p.add_argument("items", nargs="*", help="tickers (add/rm) or files (import)")
    p.add_argument("--lot", type=int, default=None, help="board lot, for Hong Kong listings")

    p = trade_parser("unlock", "unlock a stock after two stop-outs", cmd_trade_unlock)
    p.add_argument("symbol")

    p = trade_parser("earnings", "set, clear or list earnings dates", cmd_trade_earnings)
    p.add_argument("symbol", nargs="?")
    p.add_argument("date", nargs="?", help="YYYY-MM-DD, or 'clear'")

    p = trade_parser("bucket", "confirm a stock's type", cmd_trade_bucket)
    p.add_argument("symbol")
    p.add_argument("bucket", choices=["steady", "core", "volatile"])

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
