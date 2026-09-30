"""Trade management: sizing across five markets, the guardian, strikes, the gate, the dashboard.

Most tests drive the real loop against `SimBroker`, so they exercise the same
code paths as a paper account: sync, protection planning, order placement.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone

import pytest

from fireplanner.trading import (
    Instrument, Journal, SimBroker, TradingRules, TradingService, import_watchlist, load_rules,
    parse_symbol, plan_entry, strike_status,
)
from fireplanner.trading.broker import OrderSpec
from fireplanner.trading.buckets import Holding, bucket_mix, swing_share_after
from fireplanner.trading.guardian import plan_protection
from fireplanner.trading.ib_broker import IBBroker, _num, market_for
from fireplanner.trading.markets import detect_inverse, fallback_increments, round_to_tick
from fireplanner.trading.strikes import add_trading_days

RULES = TradingRules()
T0 = datetime(2026, 9, 1, 14, 0, tzinfo=timezone.utc)


def us(sym="NVDA", cid=1, **kw) -> Instrument:
    return Instrument(sym, "US", cid, "USD", 1, **kw)


def make(orders=True, cash=100_000.0):
    broker = SimBroker(cash_usd=cash, clock=T0)
    journal = Journal(":memory:")
    svc = TradingService(broker, journal, RULES, orders_enabled=orders)
    return broker, journal, svc


def buy(broker, svc, inst, price, when=None):
    """Fill a buy the way TWS would, outside the dashboard."""
    if when:
        broker.clock = when
    broker._price[inst.con_id] = price
    qty = plan_entry(inst, price, broker.daily_atr(inst), RULES.suggest_bucket(broker.daily_atr(inst)),
                     RULES, broker.usd_per_unit(inst.currency)[0], limit=price).qty
    broker.place(OrderSpec(inst.con_id, "BUY", "LMT", qty, limit_price=price, tif="DAY"))
    return svc.cycle()


def managed(broker, role=None):
    return [o for o in broker.open_orders() if o.managed and (role is None or o.role == role)]


# ---------------------------------------------------------------- markets

@pytest.mark.parametrize("text,expected", [
    ("nvda", ("NVDA", "US")), ("0700:HK", ("700", "SEHK")), ("700.HK", ("700", "SEHK")),
    ("D05:SG", ("D05", "SGX")), ("7203:JP", ("7203", "TSEJ")), ("HSBA.L", ("HSBA", "LSE")),
    ("HSBA:LN", ("HSBA", "LSE")), ("BRK.B", ("BRK.B", "US")), ("T", ("T", "US")),
])
def test_parse_symbol(text, expected):
    assert parse_symbol(text) == expected


def test_tick_rounding_respects_the_hong_kong_spread_table():
    hk = fallback_increments("SEHK")
    assert round_to_tick(3.6765, hk, "up") == 3.68
    assert round_to_tick(3.6765, hk, "down") == 3.67
    # rounding up across a band edge lands on the coarser grid
    assert round_to_tick(9.995, hk, "up") == 10.0
    assert round_to_tick(19.99, hk, "up") == 20.0
    assert round_to_tick(152.33, hk, "down") == 152.3
    # float noise is not a tick
    assert round_to_tick(95.00000000001, fallback_increments("US"), "up") == 95.0


def test_inverse_etf_detection():
    assert detect_inverse("ProShares UltraPro Short QQQ") == (True, 3)
    assert detect_inverse("ProShares Short S&P500") == (True, 1)
    assert detect_inverse("Apple Inc") == (False, None)


# ---------------------------------------------------------------- rules

def test_stock_type_suggestion_and_stop_distance():
    assert RULES.suggest_bucket(0.012) == "steady"
    assert RULES.suggest_bucket(0.025) == "core"
    assert RULES.suggest_bucket(0.05) == "volatile"
    assert RULES.suggest_bucket(None) == "volatile"        # unknown never looks calm
    assert RULES.stop_pct(0.012) == pytest.approx(0.03)     # 2.5 x ATR is tighter
    assert RULES.stop_pct(0.04) == pytest.approx(0.05)      # capped at 5%
    assert RULES.stop_pct(None) == pytest.approx(0.05)


def test_config_file_matches_the_agreed_rules():
    r = load_rules("config/config.yaml")
    assert (r.slot_usd, r.max_slots, r.max_stop_pct, r.atr_stop_mult) == (5000, 20, 0.05, 2.5)
    assert [(b.id, b.target, b.cap) for b in r.buckets] == [
        ("steady", 0.10, 20), ("core", 0.15, 10), ("volatile", 0.20, 5)]
    assert (r.max_strikes, r.lockout_days, r.swing_guide) == (2, 10, 0.5)


def test_a_typo_in_a_risk_limit_is_an_error(tmp_path):
    cfg = tmp_path / "c.yaml"
    cfg.write_text("trading:\n  max_slot: 5\n")
    with pytest.raises(ValueError, match="max_slot"):
        load_rules(cfg)


# ---------------------------------------------------------------- sizing

def test_us_volatile_trade_is_capped_at_five_percent():
    p = plan_entry(us(), 190.40, 0.036, "volatile", RULES, 1.0)
    assert p.qty == 26 and p.cost_usd <= 5000
    assert p.stop_pct <= 0.05 + 1e-9 and p.stop_basis == "5% cap"
    assert p.target == pytest.approx(p.limit * 1.2, abs=0.01)
    assert p.target_qty == 13
    assert p.max_loss_usd <= 250


def test_steady_trade_uses_the_tighter_atr_stop():
    p = plan_entry(us("KO"), 69.05, 0.011, "steady", RULES, 1.0)
    assert p.stop_pct == pytest.approx(0.0275, abs=0.002)
    assert "ATR" in p.stop_basis


def test_tokyo_rounds_down_to_board_lots_and_blocks_a_lot_bigger_than_a_slot():
    toyota = Instrument("7203", "TSEJ", 3, "JPY", 100)
    p = plan_entry(toyota, 2950, 0.018, "steady", RULES, 0.0067)
    assert p.qty % 100 == 0 and p.qty == 200
    assert p.cost_usd <= 5000
    assert p.stop == round(p.stop) and p.target % 5 == 0     # yen ticks
    pricey = Instrument("6758", "TSEJ", 4, "JPY", 100)
    q = plan_entry(pricey, 9000, 0.02, "core", RULES, 0.0067)
    assert q.qty == 0 and "more than the $5,000 slot" in q.problem


def test_london_prices_in_pence_are_valued_in_pounds():
    hsba = Instrument("HSBA", "LSE", 5, "GBP", 1, 100.0)
    p = plan_entry(hsba, 1000.0, 0.015, "steady", RULES, 1.27)
    assert p.qty == 391
    assert p.cost_local == pytest.approx(391 * 10.05)          # pounds, not pence
    assert p.cost_usd <= 5000


def test_hong_kong_needs_a_known_board_lot():
    p = plan_entry(Instrument("700", "SEHK", 6, "HKD", 0), 500, 0.02, "core", RULES, 0.128)
    assert p.qty == 0 and "board lot" in p.problem
    ok = plan_entry(Instrument("9988", "SEHK", 7, "HKD", 100), 152.3, 0.03, "core", RULES, 0.128)
    assert ok.qty == 200 and ok.target_qty == 100


def test_a_single_lot_sells_whole_at_the_target():
    dbs = Instrument("D05", "SGX", 8, "SGD", 100)
    p = plan_entry(dbs, 49.2, 0.012, "steady", RULES, 0.77)
    assert p.qty == 100 and p.target_qty == 100


# ---------------------------------------------------------------- buckets

def test_bucket_mix_measures_money_and_daily_swing():
    hs = [Holding("A", "steady", 5000, 5000, 0, 0.012), Holding("B", "steady", 5000, 5000, 0, 0.012),
          Holding("C", "volatile", 5000, 5000, 0, 0.05)]
    mix = {b.id: b for b in bucket_mix(hs, RULES)}
    assert mix["volatile"].money_share == pytest.approx(1 / 3)
    assert mix["volatile"].swing_share == pytest.approx(250 / (250 + 120))
    assert sum(b.money_share for b in mix.values()) == pytest.approx(1.0)
    assert mix["volatile"].room == 4 and mix["core"].room == 10
    before, after = swing_share_after(hs, Holding("D", "volatile", 5000, 5000, 0, 0.05))
    assert after > before


def test_room_is_limited_by_the_whole_book():
    hs = [Holding(str(i), "steady", 5000, 5000, 0, 0.01) for i in range(19)]
    mix = {b.id: b for b in bucket_mix(hs, RULES)}
    assert mix["volatile"].room == 1 and mix["steady"].room == 1


# ---------------------------------------------------------------- strikes

def _closed(journal, key, day, pct, cid=1):
    t = journal.open_trade(key=key, symbol=key.split(":")[0], market="US", con_id=cid, currency="USD",
                           bucket="volatile", entry=100.0, qty=10, atr=0.04, stop=95.0, target=120.0,
                           target_qty=5, opened_at=datetime.fromisoformat(day).replace(tzinfo=timezone.utc))
    journal.add_exit(t.id, 10, 100 * (1 + pct), "stop")
    return journal.close_trade(t.id, magnifier=1, usd_per_unit=1, strike_loss_pct=RULES.strike_loss_pct,
                               at=datetime.fromisoformat(day).replace(hour=20, tzinfo=timezone.utc))


def test_two_stop_outs_lock_a_stock_for_ten_trading_days():
    j = Journal(":memory:")
    _closed(j, "MP:US", "2026-09-11", -0.05)
    s = strike_status("MP:US", j.closed_trades("MP:US"), date(2026, 9, 14), RULES)
    assert (s.strikes, s.tries_left, s.locked) == (1, 1, False)
    _closed(j, "MP:US", "2026-09-24", -0.05)
    s = strike_status("MP:US", j.closed_trades("MP:US"), date(2026, 9, 25), RULES)
    assert s.locked and s.locked_until == date(2026, 10, 8) and s.unlocks_on == date(2026, 10, 9)
    later = strike_status("MP:US", j.closed_trades("MP:US"), date(2026, 10, 9), RULES)
    assert not later.locked and later.strikes == 0 and later.tries_left == 2


def test_a_win_or_a_scratch_resets_the_count():
    j = Journal(":memory:")
    _closed(j, "AMD:US", "2026-09-01", -0.05)
    t = _closed(j, "AMD:US", "2026-09-03", -0.003)      # breakeven stop, commissions only
    assert t.strike == 0
    s = strike_status("AMD:US", j.closed_trades("AMD:US"), date(2026, 9, 4), RULES)
    assert s.strikes == 0
    _closed(j, "AMD:US", "2026-09-08", -0.05)
    _closed(j, "AMD:US", "2026-09-09", 0.12)
    assert strike_status("AMD:US", j.closed_trades("AMD:US"), date(2026, 9, 10), RULES).strikes == 0


def test_trading_days_skip_weekends():
    assert add_trading_days(date(2026, 9, 24), 10) == date(2026, 10, 8)
    assert add_trading_days(date(2026, 9, 25), 1) == date(2026, 9, 28)


# ---------------------------------------------------------------- guardian

def test_a_new_position_gets_a_stop_and_half_target_in_one_oca_group():
    broker, journal, svc = make()
    nv = broker.add(us(), 190.0, 0.036)
    buy(broker, svc, nv, 190.0)
    stop, = managed(broker, "stop")
    target, = managed(broker, "target")
    assert (stop.qty, stop.order_type, stop.tif) == (26, "STP", "GTC")
    assert stop.stop_price == pytest.approx(180.5)
    assert (target.qty, target.limit_price) == (13, 228.0)
    assert stop.oca_group == target.oca_group != ""
    # nothing left to do on the next cycle
    assert plan_protection(journal.open_trades(), {p.con_id: p for p in broker.positions()},
                           broker.open_orders(), broker.quotes([nv]), {1: nv}, RULES) == []


def test_the_stop_moves_to_entry_then_trails_and_never_down():
    broker, journal, svc = make()
    nv = broker.add(us(), 190.0, 0.036)
    buy(broker, svc, nv, 190.0)
    broker.set_price(1, 200.0); svc.cycle()                # +5.3%
    assert managed(broker, "stop")[0].stop_price == pytest.approx(190.0)
    broker.set_price(1, 192.0); svc.cycle()                # falls back: stop stays
    assert managed(broker, "stop")[0].stop_price == pytest.approx(190.0)
    broker.set_price(1, 227.0); svc.cycle()                # trail: 227 x (1 - 3 x 3.6%)
    assert managed(broker, "stop")[0].stop_price == pytest.approx(202.48, abs=0.01)
    assert journal.open_trades()[0].stop_state == "trailing"


def test_target_fill_leaves_a_trailing_stop_on_the_rest():
    broker, journal, svc = make()
    nv = broker.add(us(), 190.0, 0.036)
    buy(broker, svc, nv, 190.0)
    broker.set_price(1, 229.0); svc.cycle()
    stop, = managed(broker, "stop")
    assert stop.qty == 13 and not managed(broker, "target")
    assert stop.stop_price > 190.0
    ev = journal.events(10)
    assert not any(e["level"] == "crit" for e in ev)        # expected, not an emergency
    broker.set_price(1, stop.stop_price - 1); svc.cycle()   # the trailing stop fires
    t = journal.closed_trades()[0]
    assert t.realized_pct > 0.1 and t.strike == 0


def test_a_position_without_a_stop_is_repaired_with_a_critical_alert():
    broker, journal, svc = make()
    nv = broker.add(us(), 190.0, 0.036)
    buy(broker, svc, nv, 190.0)
    for o in managed(broker):
        broker.cancel(o)                                    # someone cancels them in TWS
    svc.cycle()
    assert len(managed(broker, "stop")) == 1 and len(managed(broker, "target")) == 1
    assert any(e["level"] == "crit" and "had no stop" in e["message"] for e in journal.events(10))


def test_a_manual_stop_is_respected_and_a_tightened_stop_is_adopted():
    broker, journal, svc = make()
    ko = broker.add(us("KO", 2), 68.0, 0.011)
    broker.place(OrderSpec(2, "SELL", "STP", 73, stop_price=66.0, tif="GTC"))   # the user's own stop
    buy(broker, svc, ko, 68.0)
    assert not managed(broker, "stop")
    assert any("placed yourself" in e["message"] for e in journal.events(10))

    broker2, journal2, svc2 = make()
    ko2 = broker2.add(us("KO", 2), 68.0, 0.011)
    buy(broker2, svc2, ko2, 68.0)
    ours = managed(broker2, "stop")[0]
    broker2.modify(ours, stop_price=67.5)                   # tightened by hand
    svc2.cycle()
    assert managed(broker2, "stop")[0].stop_price == 67.5
    assert journal2.open_trades()[0].stop == 67.5


def test_a_stock_already_below_its_stop_gets_a_stop_under_the_market():
    broker, journal, svc = make()
    broker.add(us("ZS", 3), 288.0, 0.039)
    broker.hold(3, 17, 288.0)                               # bought earlier; guardian was off
    broker._price[3] = 270.0                                # already -6.3%
    svc.cycle()
    stop = managed(broker, "stop")[0]
    assert stop.stop_price < 270.0
    assert any(e["level"] == "crit" and "already below its stop" in e["message"] for e in journal.events(10))


def test_one_unknown_contract_does_not_stop_the_others_being_protected():
    broker, journal, svc = make()
    nv = broker.add(us(), 190.0, 0.036)
    buy(broker, svc, nv, 190.0)
    ghost = us("GHOST", 77)
    broker._inst[77] = ghost
    broker._price[77] = 10.0
    broker.hold(77, 100, 10.0)
    real = broker.instrument_for

    def flaky(p):
        if p.con_id == 77:
            raise LookupError("no security definition")
        return real(p)

    broker.instrument_for = flaky
    for o in managed(broker):
        broker.cancel(o)
    svc.cycle()
    assert len(managed(broker, "stop")) == 1                 # NVDA still protected
    assert any("not being managed" in e["message"] for e in journal.events(10))


def test_leftover_orders_are_cancelled_when_the_position_is_gone():
    broker, journal, svc = make()
    nv = broker.add(us(), 190.0, 0.036)
    buy(broker, svc, nv, 190.0)
    broker._pos[1] = [0, 0.0]                               # sold in TWS with a market order
    svc.cycle()
    assert not managed(broker)
    assert journal.closed_trades()[0].status == "closed"


def test_dry_run_sends_nothing():
    broker, journal, svc = make(orders=False)
    nv = broker.add(us(), 190.0, 0.036)
    buy(broker, svc, nv, 190.0)
    assert not managed(broker)
    assert any(e["message"].startswith("Dry run") for e in journal.events(10))
    broker.add(us("KO", 2), 68.0, 0.011)
    r = svc.enter("KO")
    assert r["dry_run"] and not r["ok"]
    assert not [o for o in broker.open_orders() if o.action == "BUY"]


# ---------------------------------------------------------------- the whole loop

def test_two_stop_outs_then_the_gate_blocks_a_third_try():
    broker, journal, svc = make()
    broker.add(us("MP", 9), 64.0, 0.061)
    for day, entry, exit_px in (("2026-09-10", 64.0, 59.5), ("2026-09-14", 62.5, 58.5)):
        broker.clock = datetime.fromisoformat(day).replace(hour=15, tzinfo=timezone.utc)
        broker._price[9] = entry
        svc.cycle()
        assert svc.enter("MP", bucket="volatile", limit=entry)["ok"]
        svc.cycle()
        broker.clock += timedelta(days=1)
        broker.set_price(9, exit_px)
        svc.cycle()
    assert [t.strike for t in journal.closed_trades("MP:US")] == [1, 1]
    r = svc.enter("MP", limit=58.5)
    assert not r["ok"] and "two-strike" in r["message"]
    broker.clock = datetime(2026, 10, 1, 15, tzinfo=timezone.utc)
    assert svc.check("MP")["verdict"] != "blocked"


def test_the_gate_blocks_a_full_bucket_and_checks_cash():
    broker, journal, svc = make(cash=100_000)
    for i in range(5):
        buy(broker, svc, broker.add(us(f"V{i}", 100 + i), 50.0, 0.05), 50.0)
    broker.add(us("V9", 199), 50.0, 0.05)
    res = svc.check("V9")
    assert res["verdict"] == "blocked"
    assert any(c["title"] == "Volatile bucket is full" for c in res["checks"])
    broker.add(us("KO", 2), 68.0, 0.011)
    broker.cash_usd = 1000
    res = svc.check("KO")
    assert any(c["title"] == "Not enough settled cash" and c["level"] == "crit" for c in res["checks"])


def test_a_buy_outside_the_dashboard_is_protected_and_flagged():
    broker, journal, svc = make()
    for i in range(6):                                      # the 6th breaks the Volatile cap
        buy(broker, svc, broker.add(us(f"V{i}", 100 + i), 50.0, 0.05), 50.0)
    assert len(managed(broker, "stop")) == 6
    assert any("over its cap of 5" in e["message"] for e in journal.events(40))


def test_entry_refuses_when_the_price_moved_or_ibkr_disagrees(monkeypatch):
    broker, journal, svc = make()
    broker.add(us(), 190.0, 0.036)
    svc.cycle()
    r = svc.enter("NVDA", expect_qty=99)
    assert not r["ok"] and "size changed" in r["message"]
    monkeypatch.setattr(broker, "preview_cost_usd", lambda spec: 500_000.0)
    r = svc.enter("NVDA")
    assert not r["ok"] and "price units disagree" in r["message"]
    assert not [o for o in broker.open_orders() if o.action == "BUY"]


def test_confirming_a_new_type_moves_the_target():
    broker, journal, svc = make()
    nv = broker.add(us(), 190.0, 0.036)
    buy(broker, svc, nv, 190.0)
    svc.set_bucket("NVDA:US", "core")
    svc.cycle()
    assert managed(broker, "target")[0].limit_price == pytest.approx(218.5)


# ---------------------------------------------------------------- demo book

@pytest.fixture(scope="module")
def demo():
    from fireplanner.trading.demo import build_demo
    return build_demo()


def test_demo_book_obeys_every_rule(demo):
    _, state = demo
    ps = state["positions"]
    assert len(ps) == 15 and all(p["protected"] for p in ps)
    for p in ps:
        assert p["cost_usd"] <= 5000 + 1e-6
        if p["stop_state"] == "initial":
            assert p["stop"] / p["entry"] >= 0.95 - 1e-9
        else:
            assert p["stop"] >= p["entry"]
    vol = next(b for b in state["buckets"] if b["id"] == "volatile")
    assert vol["n"] <= vol["cap"]
    strikes = {s["key"]: s for s in state["strikes"]}
    assert strikes["MP:US"]["locked"] and strikes["MP:US"]["unlocks_on"] == "2026-10-09"
    assert strikes["AMD:US"]["strikes"] == 1 and strikes["AMD:US"]["held"]
    wmt = next(p for p in ps if p["symbol"] == "WMT")
    assert wmt["half_sold"] and wmt["stop_state"] == "trailing"


def test_demo_checks_show_each_kind_of_verdict(demo):
    _, state = demo
    c = state["demo_checks"]
    assert c["MP:US"]["verdict"] == "blocked"
    assert c["700:SEHK"]["verdict"] == "blocked" and c["700:SEHK"]["plan"]["qty"] == 0
    assert c["HOOD:US"]["verdict"] == "warning"
    assert any("Leveraged inverse" in x["title"] for x in c["SQQQ:US"]["checks"])
    assert any(x["title"] == "Last try on this stock" for x in c["COIN:US"]["checks"])


# ---------------------------------------------------------------- dashboard server

def test_dashboard_embeds_state_safely():
    from fireplanner.trading.server import render_dashboard
    page = render_dashboard({"x": "</script><script>alert(1)</script>"})
    assert "</script><script>alert(1)" not in page
    assert "const LIVE = false;" in page


def test_dashboard_api_round_trip():
    from fireplanner.trading.server import make_server
    broker, journal, svc = make()
    broker.add(us(), 190.0, 0.036)
    journal.watch("NVDA:US", "NVDA", "US")
    svc.cycle()
    httpd = make_server(svc, "127.0.0.1", 0, token="s3cret", timeout=5)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    stop = threading.Event()

    def loop():
        while not stop.is_set():
            svc.process_commands()
            time.sleep(0.01)

    threading.Thread(target=loop, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"

    def call(path, body=None, token="s3cret", ctype="application/json"):
        req = urllib.request.Request(base + path, data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Content-Type": ctype, "Authorization": f"Bearer {token}"})
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read())

    try:
        assert call("/api/state")["totals"]["open"] == 0
        res = call("/api/check", {"symbol": "NVDA"})
        assert res["plan"]["qty"] == 26 and res["verdict"] in ("allowed", "warning")
        assert call("/api/enter", {"symbol": "NVDA", "expect_qty": 26})["ok"]
        with pytest.raises(urllib.error.HTTPError) as e:
            call("/api/state", token="wrong")
        assert e.value.code == 401
        with pytest.raises(urllib.error.HTTPError) as e:
            call("/api/check", {"symbol": "NVDA"}, ctype="text/plain")
        assert e.value.code == 415
        with pytest.raises(urllib.error.HTTPError) as e:
            call("/api/check", {"symbol": "NOPE"})
        assert e.value.code == 400
        page = urllib.request.urlopen(base + "/", timeout=10).read().decode()
        assert "const LIVE = true;" in page
        assert "NVDA" not in page.split("let STATE =")[1].split(";")[0]   # no data without the token
    finally:
        stop.set()
        httpd.shutdown()


# ---------------------------------------------------------------- IBKR adapter (no connection)

def test_live_ports_are_refused_unless_allowed():
    with pytest.raises(ValueError, match="LIVE"):
        IBBroker(port=4001)
    with pytest.raises(ValueError, match="LIVE"):
        IBBroker(port=7496)
    assert IBBroker(port=4002).mode == "paper"
    assert IBBroker(port=4001, allow_live=True).mode == "live"


def test_ibkr_value_cleanup_and_market_mapping():
    assert _num(1.7976931348623157e308) is None
    assert _num(float("nan")) is None and _num(0) is None and _num("12.5") == 12.5
    assert market_for("HKD") == "SEHK" and market_for("USD", "SMART", "NASDAQ") == "US"
    assert market_for("GBP", "LSE") == "LSE" and market_for("JPY") == "TSEJ" and market_for("SGD") == "SGX"


def test_watchlist_import_reads_a_tws_export():
    j = Journal(":memory:")
    text = "Symbol,Exchange\nDES,AAPL,STK,SMART/NASDAQ\nDES,700,STK,SEHK\n0005,SEHK\nD05,SGX\n7203,TSEJ\n\nHSBA,LSE\n"
    added = import_watchlist(j, text)
    assert added == ["AAPL:US", "700:SEHK", "5:SEHK", "D05:SGX", "7203:TSEJ", "HSBA:LSE"]
