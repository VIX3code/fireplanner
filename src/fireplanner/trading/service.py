"""One loop that owns the broker connection: sync, protect, report, answer the dashboard.

``ib_async`` is single-threaded, so everything that talks to IBKR runs on the
thread that calls `TradingService.run`. The web dashboard lives on another
thread and never touches the broker: it reads the last snapshot, and anything
that needs IBKR (checking a trade, sending a buy) goes through `submit`, which
queues the request for the loop and waits for the answer.

Each cycle:

1. read positions, working orders, fills and prices;
2. `sync_trades` records opens, adds, exits and closes in the journal;
3. `plan_protection` works out the stop and target changes, which are sent
   (or, with orders disabled, logged as "dry run");
4. time stops, earnings and the circuit breakers are checked;
5. a fresh snapshot is built for the dashboard.

Market weather and earnings dates refresh at most once a day. A fill wakes
the loop at once; otherwise it runs every ``interval`` seconds.
"""

from __future__ import annotations

import html
import queue
import threading
import time
from concurrent.futures import Future
from dataclasses import replace
from datetime import datetime

from .breakers import breaker_state
from .broker import Broker, BrokerPosition, OrderSpec, order_ref
from .buckets import bucket_mix
from .earnings import EarningsCalendar, parse_date
from .gate import GateContext, check_entry
from .guardian import Action, plan_protection
from .journal import Journal, Trade
from .markets import MARKETS, Instrument, parse_symbol
from .rules import ADJUSTABLE, TradingRules, with_overrides
from .sizing import plan_entry, target_price
from .stats import journal_stats, trade_r
from .strikes import strike_board, strike_for
from .sync import holdings_from, sync_trades
from .weather import MarketWeather

__all__ = ["TradingService", "import_watchlist"]

#: Info-level events worth a phone notification.
_NOTIFY_KINDS = {"opened", "closed", "stopped-out", "exit-target", "exit-stop", "exit-trail", "exit-breakeven",
                 "exit-manual", "exit-exit", "exit-time-stop", "entry", "stop-state", "added", "time-stop",
                 "earnings", "breaker"}


class TradingService:
    def __init__(self, broker: Broker, journal: Journal, rules: TradingRules, *,
                 orders_enabled: bool = False, notifier=None, seed_watchlist=(), lot_sizes: dict | None = None,
                 weather_proxies: dict | None = None, earnings: EarningsCalendar | None = None):
        self.broker, self.journal = broker, journal
        self.base_rules = rules
        self.rules = with_overrides(rules, journal.settings())
        self.orders_enabled = orders_enabled
        self.notifier = notifier
        self.lot_sizes = {str(k).upper(): int(v) for k, v in (lot_sizes or {}).items()}
        self.weather = MarketWeather(self.rules, weather_proxies, broker.bars) if weather_proxies else None
        self.earnings = earnings if earnings is not None else EarningsCalendar(journal)
        self._cmds: queue.Queue = queue.Queue()
        self._lock = threading.Lock()
        self._state: dict = {}
        self._said: set = set()
        self._inst: dict[int, Instrument] = {}
        self._inst_key: dict[str, Instrument] = {}
        self._resolve_err: dict[str, tuple[str, float]] = {}
        self._atr: dict[int, tuple[str, float | None]] = {}
        self._fx: dict[str, tuple[float, bool]] = {}
        self._positions: dict[int, BrokerPosition] = {}
        self._quotes: dict[int, float] = {}
        self._orders: list = []
        self._cash = None
        self.last_error: str | None = None
        self.cycles = 0
        for entry in seed_watchlist:
            self._seed(entry)

    # -- setup -------------------------------------------------------------
    def _seed(self, entry) -> None:
        if isinstance(entry, dict):
            text, lot, note = entry.get("symbol", ""), entry.get("lot"), entry.get("note", "")
        else:
            text, lot, note = str(entry), None, ""
        sym, mkt = parse_symbol(text)
        key = f"{sym}:{mkt}"
        if not any(w["key"] == key for w in self.journal.watchlist()):
            self.journal.watch(key, sym, mkt, source="config", lot_size=lot, note=note)

    def _reload_rules(self) -> None:
        self.rules = with_overrides(self.base_rules, self.journal.settings())
        if self.weather is not None:
            self.weather.rescale(self.rules)

    # -- lookups -------------------------------------------------------------
    def _lot_for(self, key: str) -> int | None:
        return self.journal.lot_override(key) or self.lot_sizes.get(key)

    def resolve(self, symbol: str, market: str) -> Instrument:
        key = f"{symbol}:{market}"
        lot = self._lot_for(key)
        inst = self._inst_key.get(key)
        if inst is not None and (not lot or inst.lot_size == lot):
            return inst
        inst = self.broker.instrument(symbol, market, lot_size=lot)
        self._remember(inst)
        return inst

    def _remember(self, inst: Instrument) -> None:
        self._inst[inst.con_id] = inst
        self._inst_key[inst.key] = inst
        if inst.sector:
            self.journal.set_sector(inst.key, inst.sector, source="ibkr")

    def _for_position(self, p: BrokerPosition) -> Instrument:
        inst = self._inst.get(p.con_id) or self.broker.instrument_for(p)
        lot = self._lot_for(inst.key)
        if lot and inst.lot_size != lot:
            inst = replace(inst, lot_size=lot)
        self._remember(inst)
        return inst

    def _atr_for(self, inst: Instrument, today: str) -> float | None:
        hit = self._atr.get(inst.con_id)
        if hit and hit[0] == today:
            return hit[1]
        value = self.broker.daily_atr(inst)
        self._atr[inst.con_id] = (today, value)
        return value

    def _usd(self, currency: str) -> float:
        if currency not in self._fx:
            self._fx[currency] = self.broker.usd_per_unit(currency)
        return self._fx[currency][0]

    def _sector(self, key: str) -> str:
        inst = self._inst_key.get(key)
        return self.journal.sectors().get(key) or (inst.sector if inst else "") or ""

    def _open_trade(self, key: str) -> Trade | None:
        return next((t for t in self.journal.open_trades() if t.key == key and t.con_id in self._positions), None)

    # -- events ----------------------------------------------------------------
    def emit(self, level: str, kind: str, message: str, key: str | None = None, once: bool = False) -> None:
        if once:
            sig = (kind, key, message)
            if sig in self._said:
                return
            self._said.add(sig)
        self.journal.log(level, kind, message, key=key, at=self.broker.now())
        if self.notifier is not None and (level in ("warn", "crit") or kind in _NOTIFY_KINDS):
            icon = {"crit": "🔴", "warn": "🟠"}.get(level, "🔵")
            try:
                self.notifier.send(f"{icon} {html.escape(message)}")
            except Exception as exc:  # a dead notifier must never stop the guardian
                self.journal.log("warn", "notify", f"Telegram failed: {exc}")

    # -- the cycle ---------------------------------------------------------------
    def cycle(self) -> dict:
        now = self.broker.now()
        today = now.date()
        positions = self.broker.positions()
        held = {p.con_id: p for p in positions if p.qty > 0}
        for p in positions:
            if p.qty != 0:
                try:
                    self._for_position(p)
                except Exception as exc:
                    # One unresolvable contract must not stop the others being protected.
                    self.emit("crit", "resolve", f"Can't identify {p.symbol} ({p.market}) at IBKR, so it is "
                              f"not being managed: {exc}", key=f"{p.symbol}:{p.market}", once=True)

        for w in self.journal.watchlist():
            if w["key"] in self._inst_key:
                continue
            err = self._resolve_err.get(w["key"])
            if err and time.time() - err[1] < 1800:
                continue
            try:
                self.resolve(w["symbol"], w["market"])
                self._resolve_err.pop(w["key"], None)
            except Exception as exc:
                self._resolve_err[w["key"]] = (str(exc), time.time())

        insts = list(self._inst.values())
        self._fx = {}
        fx = {c: self._usd(c) for c in {i.currency for i in insts} | {"USD"}}
        quotes = self.broker.quotes(insts)
        atr = {i.con_id: self._atr_for(i, today.isoformat()) for i in insts}

        for e in sync_trades(self.journal, positions, self.broker.fills(), quotes, self._inst, atr, fx,
                             self.rules, today, now=now):
            self.emit(e.level, e.kind, e.message, key=e.key)

        self._align_targets()
        orders = self.broker.open_orders()
        before = {t.id: t for t in self.journal.open_trades()}
        actions = plan_protection(list(before.values()), held, orders, quotes, self._inst, self.rules)
        self._execute(actions, before)

        self._positions, self._quotes, self._orders = held, quotes, self.broker.open_orders()
        try:
            self._cash = self.broker.cash()
        except Exception:
            self._cash = None
        if self.weather is not None:
            try:
                self.weather.refresh(today)
            except Exception as exc:
                self.emit("warn", "weather", f"Market weather unavailable: {exc}", once=True)
        try:
            keys = [w["key"] for w in self.journal.watchlist()] + [t.key for t in self.journal.open_trades()]
            self.earnings.refresh(list(dict.fromkeys(keys)), today)
        except Exception as exc:
            self.emit("warn", "earnings", f"Earnings lookup failed: {exc}", once=True)
        self._watch_positions(now)

        state = self._build_state(now)
        with self._lock:
            self._state = state
        self.cycles += 1
        self.last_error = None
        return state

    def _time_stopped(self, t: Trade, now: datetime) -> bool:
        weeks = (now - t.opened).total_seconds() / (7 * 86400)
        return weeks >= self.rules.time_stop_weeks and t.high_water < t.entry * (1 + self.rules.time_stop_min_gain)

    def _watch_positions(self, now: datetime) -> None:
        """Time stops and earnings on open positions: alert once, sell if told to."""
        today = now.date()
        upcoming = self.earnings.upcoming(today)
        for t in self.journal.open_trades():
            if t.con_id not in self._positions:
                continue
            if self._time_stopped(t, now):
                msg = (f"Time stop: {t.symbol} has gone {self.rules.time_stop_weeks:g} weeks without reaching "
                       f"+{self.rules.time_stop_min_gain:.0%}.")
                if self.rules.time_stop_action == "sell":
                    working = any(o.con_id == t.con_id and o.role in ("exit", "timestop") for o in self._orders)
                    if not working:
                        r = self.exit_position(t.key, role="timestop")
                        msg += f" {r['message']}"
                else:
                    msg += " Sell it from the dashboard if you agree."
                self.emit("warn", "time-stop", msg, key=t.key, once=True)
            e = upcoming.get(t.key)
            if e and e["days"] <= 2:
                self.emit("warn", "earnings", f"{t.symbol} reports earnings {'today' if e['days'] == 0 else e['date']}. "
                          f"A stop can't protect against the gap.", key=t.key, once=True)

    def _align_targets(self) -> None:
        """A trade's target follows its stock type and entry until the first exit."""
        for t in self.journal.open_trades():
            inst = self._inst.get(t.con_id)
            if inst is None or t.has_exits:
                continue
            want = target_price(t.entry, self.rules.bucket(t.bucket).target, inst)
            if abs(want - t.target) > inst.tick(t.entry) / 2:
                self.journal.update_trade(t.id, target=want)

    def _execute(self, actions: list[Action], before: dict) -> None:
        for a in actions:
            old = before.get(a.trade_id)
            if a.kind == "note":
                if a.trade_update:
                    self.journal.update_trade(a.trade_id, **a.trade_update)
                self.emit(a.level, "note", a.reason, key=a.key, once=True)
                continue
            if not self.orders_enabled:
                self.emit(a.level, "dry-run", f"Dry run, not sent: {a.reason}", key=a.key, once=True)
                continue
            try:
                if a.kind == "place":
                    self.broker.place(a.spec)
                elif a.kind == "modify":
                    self.broker.modify(a.order, **a.changes)
                elif a.kind == "cancel":
                    self.broker.cancel(a.order)
            except Exception as exc:
                self.emit("crit", "order-failed", f"{a.reason} FAILED: {exc}", key=a.key, once=True)
                continue
            if a.trade_update and a.trade_id is not None:
                self.journal.update_trade(a.trade_id, **a.trade_update)
            new_state = a.trade_update.get("stop_state")
            kind = "stop-state" if old is not None and new_state and new_state != old.stop_state else "order"
            self.emit(a.level, kind, a.reason, key=a.key)

    # -- snapshot ------------------------------------------------------------------
    def _add_problem(self, t: Trade) -> str | None:
        r = self.rules
        if not r.allow_add:
            return "Adding to winners is switched off."
        if t.adds >= r.max_adds:
            return f"Already added to ({r.max_adds} add{'s' if r.max_adds != 1 else ''} allowed)."
        if t.has_exits:
            return "Half was already sold at the target."
        if t.stop < t.entry:
            return (f"Only winners: its stop moves to the entry price once it has been "
                    f"+{r.breakeven_at:.0%}, and then it can be added to.")
        return None

    def _build_state(self, now: datetime) -> dict:
        rules, journal = self.rules, self.journal
        today = now.date()
        fx = {c: v[0] for c, v in self._fx.items()}
        holdings = holdings_from(journal, self._positions, self._quotes, self._inst, fx)
        mix = bucket_mix(holdings, rules)
        confirmed = journal.confirmed_buckets()
        sectors = journal.sectors()
        upcoming = self.earnings.upcoming(today)
        by_con_orders: dict[int, list] = {}
        for o in self._orders:
            by_con_orders.setdefault(o.con_id, []).append(o)

        rows = []
        for t in journal.open_trades():
            inst = self._inst.get(t.con_id)
            pos = self._positions.get(t.con_id)
            if inst is None or pos is None:
                continue
            usd = fx.get(t.currency, 1.0)
            qty = int(pos.qty)
            last = self._quotes.get(t.con_id, t.entry)
            orders = by_con_orders.get(t.con_id, [])
            stop_cover = sum(o.remaining for o in orders if o.is_protective_stop)
            span = t.target - t.stop
            st = strike_for(journal, t.key, today, rules)
            add_problem = self._add_problem(t)
            rows.append({
                "trade_id": t.id, "key": t.key, "symbol": t.symbol, "market": t.market,
                "market_name": MARKETS[t.market].name, "currency": t.currency, "name": inst.description,
                "bucket": t.bucket, "bucket_confirmed": t.key in confirmed,
                "sector": sectors.get(t.key) or t.sector or inst.sector or "",
                "qty": qty, "entry": t.entry, "last": last, "decimals": inst.decimals(last),
                "value_usd": inst.value(qty, last) * usd, "cost_usd": inst.value(qty, t.entry) * usd,
                "pl_usd": inst.value(qty, last - t.entry) * usd, "pl_pct": last / t.entry - 1,
                "stop": t.stop, "stop_state": t.stop_state, "stop_from_entry": t.stop / t.entry - 1,
                "risk_usd": max(0.0, inst.value(qty, t.entry - t.stop) * usd),
                "target": t.target, "target_active": not t.has_exits,
                "target_order": any(o.role == "target" for o in orders),
                "target_qty": t.target_qty, "progress": (last - t.stop) / span if span > 0 else 0,
                "entry_progress": (t.entry - t.stop) / span if span > 0 else 0,
                "days": max(0, (today - t.opened.date()).days), "atr": t.atr,
                "protected": stop_cover >= qty, "strikes": st.strikes, "half_sold": t.has_exits,
                "inverse": inst.inverse, "setup": t.setup, "adds": t.adds,
                "can_add": add_problem is None, "add_problem": add_problem,
                "exit_working": any(o.role in ("exit", "timestop") for o in orders),
                "time_stop": self._time_stopped(t, now),
                "earnings": upcoming.get(t.key),
                "mfe": t.high_water / t.entry - 1, "mae": ((t.low_water or t.entry) / t.entry - 1),
            })

        total_value = sum(h.value_usd for h in holdings)
        total_cost = sum(h.cost_usd for h in holdings)
        watch = []
        held_keys = {r["key"] for r in rows}
        for w in journal.watchlist():
            inst = self._inst_key.get(w["key"])
            a = self._atr.get(inst.con_id, (None, None))[1] if inst else None
            st = strike_for(journal, w["key"], today, rules)
            err = self._resolve_err.get(w["key"])
            watch.append({
                "key": w["key"], "symbol": w["symbol"], "market": w["market"], "source": w["source"],
                "note": w["note"], "lot_size": inst.lot_size if inst else w["lot_size"],
                "currency": inst.currency if inst else MARKETS[w["market"]].currency,
                "name": inst.description if inst else "",
                "sector": sectors.get(w["key"]) or (inst.sector if inst else "") or "",
                "last": self._quotes.get(inst.con_id) if inst else None,
                "decimals": inst.decimals(self._quotes.get(inst.con_id, 1.0)) if inst else 2,
                "atr": a, "suggested": rules.suggest_bucket(a) if inst else None,
                "confirmed": confirmed.get(w["key"]), "held": w["key"] in held_keys,
                "tries_left": st.tries_left, "locked": st.locked, "manual_lock": st.manual,
                "unlocks_on": st.unlocks_on.isoformat() if st.unlocks_on else None,
                "earnings": upcoming.get(w["key"]),
                "error": err[0] if err else None,
            })

        board = []
        for s in strike_board(journal, today, rules):
            d = s.as_dict()
            d["held"] = s.key in held_keys
            d["bucket"] = confirmed.get(s.key) or next(
                (t.bucket for t in reversed(journal.closed_trades(s.key))), "volatile")
            board.append(d)

        by_sector: dict[str, dict] = {}
        for r in rows:
            sec = r["sector"] or "Unknown"
            g = by_sector.setdefault(sec, {"sector": sec, "n": 0, "value_usd": 0.0})
            g["n"] += 1
            g["value_usd"] += r["value_usd"]

        closed = journal.closed_trades()
        recent = []
        for t in reversed(closed[-40:]):
            r = trade_r(t)
            recent.append({
                "trade_id": t.id, "key": t.key, "symbol": t.symbol, "market": t.market, "bucket": t.bucket,
                "setup": t.setup, "note": t.note, "opened": t.opened_at, "closed": t.closed_at,
                "days": t.days_held, "entry": t.entry, "result_pct": t.realized_pct, "result_usd": t.realized_usd,
                "r": r, "strike": bool(t.strike), "adds": t.adds,
                "mfe": t.high_water / t.entry - 1, "mae": ((t.low_water or t.entry) / t.entry - 1),
                "exit": ", ".join(sorted({e.kind for e in journal.exits(t.id)})),
            })

        cash = self._cash
        breaker = breaker_state(journal, rules, now)
        weather = {}
        if self.weather is not None:
            for m, r in self.weather.readings.items():
                d = r.as_dict()
                d["market_name"] = MARKETS[m].name if m in MARKETS else m
                weather[m] = d
        return {
            "generated_at": now.isoformat(timespec="seconds"),
            "mode": self.broker.mode, "orders_enabled": self.orders_enabled,
            "rules": rules.as_dict(),
            "settings": {k: {"value": getattr(rules, k), "min": lo, "max": hi} for k, (lo, hi) in ADJUSTABLE.items()},
            "totals": {
                "slots_used": len(rows), "max_slots": rules.max_slots,
                "invested_usd": total_value, "cost_usd": total_cost,
                "pl_usd": total_value - total_cost,
                "pl_pct": (total_value / total_cost - 1) if total_cost else 0.0,
                "risk_usd": sum(h.risk_usd for h in holdings),
                "moved": sum(1 for r in rows if r["stop_state"] != "initial"),
                "protected": sum(1 for r in rows if r["protected"]), "open": len(rows),
                "cash_usd": cash.available_usd if cash else None,
            },
            "buckets": [b.as_dict() for b in mix],
            "sectors": sorted(by_sector.values(), key=lambda g: (-g["n"], -g["value_usd"])),
            "positions": rows,
            "watchlist": watch,
            "strikes": board,
            "weather": weather,
            "breaker": breaker.as_dict(),
            "earnings_online": self.earnings.online,
            "journal": {"stats": journal_stats(closed, now), "trades": recent},
            "events": journal.events(40),
            "error": self.last_error,
        }

    def snapshot(self) -> dict:
        with self._lock:
            return dict(self._state)

    # -- commands (run on the loop thread) -------------------------------------------
    def _context(self, inst: Instrument, today, *, is_add=False, add_problem=None,
                 suggested="volatile", confirmed=False, live=True) -> GateContext:
        fx = {c: v[0] for c, v in self._fx.items()}
        holdings = holdings_from(self.journal, self._positions, self._quotes, self._inst, fx)
        held_keys = {t.key for t in self.journal.open_trades() if t.con_id in self._positions}
        working = {self._inst[o.con_id].key for o in self.broker.open_orders()
                   if o.action == "BUY" and o.con_id in self._inst}
        cash = self.broker.cash()
        counts: dict[str, int] = {}
        for t in self.journal.open_trades():
            if t.con_id in self._positions:
                sec = self._sector(t.key) or t.sector
                if sec:
                    counts[sec] = counts.get(sec, 0) + 1
        return GateContext(
            holdings=holdings, held_keys=held_keys, working_buy_keys=working,
            strike=strike_for(self.journal, inst.key, today, self.rules),
            suggested_bucket=suggested, bucket_confirmed=confirmed,
            cash_usd=cash.available_usd, currency_cash=cash.by_currency.get(inst.currency), fx_live=live,
            weather=self.weather.for_market(inst.market) if self.weather else None,
            breaker=breaker_state(self.journal, self.rules, self.broker.now()),
            earnings=self.earnings.upcoming(today).get(inst.key),
            sector=self._sector(inst.key), sector_counts=counts, is_add=is_add, add_problem=add_problem,
        )

    def check(self, symbol: str, bucket: str | None = None, limit: float | None = None, add: bool = False) -> dict:
        sym, mkt = parse_symbol(symbol)
        inst = self.resolve(sym, mkt)
        today = self.broker.now().date()
        last = self.broker.quotes([inst]).get(inst.con_id)
        if last is None:
            raise LookupError(f"No price for {inst.key} right now. Market data may be unavailable.")
        a = self._atr_for(inst, today.isoformat())
        rate, live = self.broker.usd_per_unit(inst.currency)
        suggested = self.rules.suggest_bucket(a)
        confirmed = self.journal.confirmed_bucket(inst.key)
        add_problem, trade = None, None
        if add:
            trade = self._open_trade(inst.key)
            if trade is None:
                raise LookupError(f"No open position in {inst.key} to add to.")
            add_problem = self._add_problem(trade)
            chosen = trade.bucket
        else:
            chosen = bucket or confirmed or suggested
        w = self.weather.for_market(inst.market) if self.weather else None
        budget = self.rules.risk_per_trade_usd * (w.multiplier if w and w.label else 1.0)
        plan = plan_entry(inst, last, a, chosen, self.rules, rate, limit=limit, risk_usd=budget)
        ctx = self._context(inst, today, is_add=add, add_problem=add_problem, suggested=suggested,
                            confirmed=bool(confirmed) or bucket is not None, live=live)
        out = check_entry(plan, inst, rules=self.rules, ctx=ctx).as_dict()
        out["instrument"] = {"key": inst.key, "symbol": inst.symbol, "market": inst.market,
                             "market_name": MARKETS[inst.market].name, "name": inst.description,
                             "currency": inst.currency, "lot_size": inst.lot_size, "sector": ctx.sector,
                             "decimals": inst.decimals(last), "inverse": inst.inverse, "leverage": inst.leverage}
        out["orders_enabled"] = self.orders_enabled
        out["is_add"] = add
        return out

    def _send_buy(self, checked: dict, role: str, ref_id: int, expect_qty: int | None, verb: str) -> dict:
        plan = checked["plan"]
        if checked["verdict"] == "blocked":
            reasons = "; ".join(c["title"] for c in checked["checks"] if c["level"] == "crit")
            return {"ok": False, "message": f"Blocked: {reasons}", "check": checked}
        if expect_qty is not None and int(expect_qty) != plan["qty"]:
            return {"ok": False, "message": "The price moved and the size changed. Review the new plan and confirm again.",
                    "check": checked}
        inst = self._inst_key[plan["key"]]
        spec = OrderSpec(con_id=inst.con_id, action="BUY", order_type="LMT", qty=plan["qty"],
                         limit_price=plan["limit"], tif="DAY", order_ref=order_ref(role, ref_id))
        desc = (f"{verb} {plan['qty']} {inst.symbol} ({MARKETS[inst.market].name}) limit {inst.fmt(plan['limit'])}, "
                f"about ${plan['cost_usd']:,.0f}, risking ${plan['max_loss_usd']:,.0f}")
        if not self.orders_enabled:
            self.emit("info", "entry", f"Dry run, not sent: {desc}", key=inst.key)
            return {"ok": False, "dry_run": True, "message": f"Orders are switched off (dry run). Would send: {desc}.",
                    "check": checked}
        preview = self.broker.preview_cost_usd(spec)
        if preview is not None and abs(preview - plan["cost_usd"]) > 0.4 * plan["cost_usd"]:
            msg = (f"Not sent: IBKR values this order at ${preview:,.0f} but the plan says "
                   f"${plan['cost_usd']:,.0f}. The price units disagree; check {inst.key} before trading it.")
            self.emit("crit", "entry", msg, key=inst.key)
            return {"ok": False, "message": msg, "check": checked}
        self.broker.place(spec)
        self.journal.breaker_event("entry", note=inst.key, at=self.broker.now())
        self.emit("info", "entry", f"Order sent: {desc}. The stop and target follow as soon as it fills.", key=inst.key)
        note = "" if preview is not None or inst.market == "US" else " (IBKR's cost preview was unavailable.)"
        return {"ok": True, "message": f"Sent: {desc}.{note}", "check": checked}

    def enter(self, symbol: str, bucket: str | None = None, limit: float | None = None,
              expect_qty: int | None = None, setup: str = "", note: str = "") -> dict:
        checked = self.check(symbol, bucket=bucket, limit=limit)
        if checked["verdict"] != "blocked":
            key = checked["plan"]["key"]
            if bucket:
                self.journal.confirm_bucket(key, bucket)
            elif not self.journal.confirmed_bucket(key):
                self.journal.confirm_bucket(key, checked["plan"]["bucket"])
            self.journal.set_pending(key, setup=setup or "", note=note or "")
        return self._send_buy(checked, "entry", int(time.time()), expect_qty, "BUY")

    def add_to(self, key: str, limit: float | None = None, expect_qty: int | None = None) -> dict:
        checked = self.check(key, limit=limit, add=True)
        trade = self._open_trade(checked["plan"]["key"])
        return self._send_buy(checked, "add", trade.id if trade else 0, expect_qty, "ADD")

    def exit_position(self, key: str, role: str = "exit") -> dict:
        """Sell a whole position now with a marketable limit, in the stop's OCA group."""
        sym, mkt = parse_symbol(key)
        t = self._open_trade(f"{sym}:{mkt}")
        if t is None:
            raise LookupError(f"No open position in {sym}:{mkt}.")
        inst = self._inst[t.con_id]
        pos = self._positions[t.con_id]
        last = self.broker.quotes([inst]).get(t.con_id) or self._quotes.get(t.con_id, t.entry)
        limit = inst.round(last * (1 - self.rules.entry_limit_buffer), "down")
        group = next((o.oca_group for o in self._orders if o.con_id == t.con_id and o.role == "stop"), "")
        spec = OrderSpec(con_id=t.con_id, action="SELL", order_type="LMT", qty=int(pos.qty), limit_price=limit,
                         tif="DAY", oca_group=group, order_ref=order_ref(role, t.id))
        desc = f"SELL {int(pos.qty)} {t.symbol} limit {inst.fmt(limit)}"
        if not self.orders_enabled:
            self.emit("info", "exit", f"Dry run, not sent: {desc}", key=t.key)
            return {"ok": False, "dry_run": True, "message": f"Orders are switched off (dry run). Would send: {desc}."}
        self.broker.place(spec)
        self.emit("info", "exit", f"Sell order sent: {desc}. If it fills, the stop and target are cancelled with it.",
                  key=t.key)
        return {"ok": True, "message": f"Sent: {desc}."}

    def unlock(self, key: str) -> dict:
        sym, mkt = parse_symbol(key)
        key = f"{sym}:{mkt}"
        self.journal.unlock(key, at=self.broker.now())
        self.emit("info", "unlock", f"{key} unlocked by you: two tries again.", key=key)
        return {"ok": True, "message": f"{key} unlocked."}

    def pause(self, note: str = "") -> dict:
        self.journal.breaker_event("pause", note=note, at=self.broker.now())
        self.emit("warn", "breaker", "New buys paused by you. Stops and targets keep working.")
        return {"ok": True, "message": "New buys paused."}

    def resume(self) -> dict:
        self.journal.breaker_event("resume", at=self.broker.now())
        self.emit("info", "breaker", "New buys resumed.")
        return {"ok": True, "message": "Resumed. Loss limits still apply."}

    def kill(self) -> dict:
        """Cancel every buy order this system has working, then pause."""
        cancelled = 0
        for o in self.broker.open_orders():
            if o.action == "BUY" and o.role in ("entry", "add"):
                if self.orders_enabled:
                    self.broker.cancel(o)
                cancelled += 1
        self.journal.breaker_event("kill", note=f"{cancelled} buy orders", at=self.broker.now())
        verb = "cancelled" if self.orders_enabled else "would be cancelled (dry run)"
        self.emit("crit", "breaker", f"Kill switch: {cancelled} working buy order(s) {verb}; new buys paused. "
                  f"Stops and targets are untouched.")
        return {"ok": True, "message": f"Kill switch on: {cancelled} buy order(s) {verb}, new buys paused."}

    def set_earnings(self, key: str, date: str | None = None) -> dict:
        sym, mkt = parse_symbol(key)
        key = f"{sym}:{mkt}"
        d = parse_date(date)
        self.journal.set_earnings(key, d, source="manual")
        return {"ok": True, "message": f"{key}: earnings {'set to ' + d.isoformat() if d else 'cleared'}."}

    def set_sector(self, key: str, sector: str) -> dict:
        sym, mkt = parse_symbol(key)
        self.journal.set_sector(f"{sym}:{mkt}", sector.strip(), source="manual")
        return {"ok": True, "message": f"{sym}:{mkt} grouped under {sector.strip()}."}

    def set_settings(self, **values) -> dict:
        changed = []
        for k, v in values.items():
            if k not in ADJUSTABLE or v is None:
                continue
            lo, hi = ADJUSTABLE[k]
            v = min(hi, max(lo, type(lo)(v)))
            self.journal.set_setting(k, v)
            changed.append(f"{k.replace('_', ' ')} = {v:g}")
        self._reload_rules()
        if changed:
            self.emit("info", "settings", "Settings changed: " + ", ".join(changed))
        return {"ok": bool(changed), "message": "Saved: " + ", ".join(changed) if changed else "Nothing to change."}

    def set_trade_note(self, trade_id: int, setup: str | None = None, note: str | None = None) -> dict:
        changes = {}
        if setup is not None:
            changes["setup"] = setup
        if note is not None:
            changes["note"] = note
        self.journal.update_trade(int(trade_id), **changes)
        return {"ok": True, "message": "Journal updated."}

    def watch_add(self, symbol: str, lot_size: int | None = None, note: str = "") -> dict:
        sym, mkt = parse_symbol(symbol)
        key = f"{sym}:{mkt}"
        self.journal.watch(key, sym, mkt, source="manual", lot_size=lot_size, note=note)
        self._inst_key.pop(key, None)
        self._resolve_err.pop(key, None)
        try:
            inst = self.resolve(sym, mkt)
            return {"ok": True, "key": key, "message": f"Added {inst.key} to the watchlist."}
        except Exception as exc:
            return {"ok": True, "key": key, "message": f"Added {key}, but IBKR couldn't find it yet: {exc}"}

    def watch_remove(self, key: str) -> dict:
        ok = self.journal.unwatch(key)
        return {"ok": ok, "message": f"Removed {key}." if ok else f"{key} was not on the watchlist."}

    def set_bucket(self, key: str, bucket: str) -> dict:
        b = self.rules.bucket(bucket)
        self.journal.confirm_bucket(key, b.id)
        self._align_targets()
        self.emit("info", "bucket", f"{key} confirmed as {b.name} (target +{b.target:.0%}).", key=key)
        return {"ok": True, "message": f"{key} is now {b.name}."}

    _COMMANDS = {"check", "enter", "add_to", "exit_position", "unlock", "pause", "resume", "kill",
                 "set_earnings", "set_sector", "set_settings", "set_trade_note",
                 "watch_add", "watch_remove", "set_bucket"}

    def submit(self, name: str, **kwargs) -> Future:
        if name not in self._COMMANDS:
            raise ValueError(f"unknown command {name!r}")
        fut: Future = Future()
        self._cmds.put((name, kwargs, fut))
        return fut

    def process_commands(self) -> int:
        n = 0
        while True:
            try:
                name, kwargs, fut = self._cmds.get_nowait()
            except queue.Empty:
                return n
            n += 1
            try:
                fut.set_result(getattr(self, name)(**kwargs))
            except Exception as exc:
                fut.set_exception(exc)

    # -- run ---------------------------------------------------------------------------
    def run(self, interval: float = 30.0, stop: threading.Event | None = None) -> None:
        stop = stop or threading.Event()
        while not stop.is_set():
            try:
                self.cycle()
            except Exception as exc:
                self.last_error = str(exc)
                with self._lock:
                    self._state = {**self._state, "error": self.last_error}
                self.emit("crit", "cycle-failed", f"Guardian cycle failed: {exc}", once=True)
            deadline = time.monotonic() + interval
            while not stop.is_set() and time.monotonic() < deadline:
                self.process_commands()
                try:
                    self.broker.wait(0.25)
                    if self.broker.pop_dirty():
                        break
                except Exception:
                    time.sleep(1.0)


def import_watchlist(journal: Journal, text: str, source: str = "ibkr") -> list[str]:
    """Add tickers from a TWS watchlist export (or any CSV / text list).

    Lenient on purpose: each line's first ticker-like cell is the symbol, and an
    exchange code anywhere on the line (SEHK, SGX, TSEJ, LSE) picks the market.
    Header lines and TWS's ``DES``/``SYM`` prefixes are skipped.
    """
    added = []
    for raw in text.splitlines():
        cells = [c.strip().strip('"') for c in raw.replace("\t", ",").split(",")]
        cells = [c for c in cells if c]
        if not cells:
            continue
        if cells[0].upper() in {"SYMBOL", "TICKER", "FINANCIAL INSTRUMENT", "COLUMN"}:
            continue                                  # a header row
        if cells[0].upper() in {"DES", "SYM"}:
            cells = cells[1:]
        if not cells:
            continue
        market = None
        joined = " ".join(cells[1:]).upper().replace("/", " ")
        for code, mkt in (("SEHK", "SEHK"), ("SGX", "SGX"), ("TSEJ", "TSEJ"), ("LSE", "LSE")):
            if code in joined.split():
                market = mkt
                break
        text_sym = cells[0] if market is None or ":" in cells[0] else f"{cells[0]}:{market}"
        try:
            sym, mkt = parse_symbol(text_sym)
        except ValueError:
            continue
        if not sym.replace(".", "").replace("-", "").isalnum():
            continue
        key = f"{sym}:{mkt}"
        journal.watch(key, sym, mkt, source=source)
        added.append(key)
    return added
