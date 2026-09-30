"""Trade management: fixed-loss sizing across five markets, the guardian, strikes, the gate,
adds, time stops, market weather, circuit breakers, earnings, the journal and the dashboard.

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
from dataclasses import replace

from fireplanner.trading.breakers import breaker_state
from fireplanner.trading.broker import OrderSpec
from fireplanner.trading.earnings import EarningsCalendar, fmp_symbol, parse_fmp
from fireplanner.trading.stats import journal_stats
from fireplanner.trading.weather import MarketWeather, WeatherReading, read_weather
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
    assert (r.risk_per_trade_usd, r.max_position_usd, r.max_invested_usd, r.max_slots) == (250, 10000, 100000, 20)
    assert (r.max_stop_pct, r.atr_stop_mult, r.round_up) == (0.05, 2.5, True)
    assert [(b.id, b.target, b.cap) for b in r.buckets] == [
        ("steady", 0.10, 10), ("core", 0.15, 10), ("volatile", 0.20, 7)]
    assert (r.max_strikes, r.lockout_days, r.swing_guide) == (2, None, 0.5)
    assert (r.allow_add, r.max_adds, r.time_stop_weeks, r.max_per_sector) == (True, 1, 4, 4)
    assert r.weather_multiplier("Defensive") == 0.5 and r.weather_multiplier("Risk-Off") == 0


def test_a_typo_in_a_risk_limit_is_an_error(tmp_path):
    cfg = tmp_path / "c.yaml"
    cfg.write_text("trading:\n  max_slot: 5\n")
    with pytest.raises(ValueError, match="max_slot"):
        load_rules(cfg)


# ---------------------------------------------------------------- sizing

def test_every_trade_loses_the_same_fixed_amount_at_its_stop():
    nv = plan_entry(us(), 190.40, 0.036, "volatile", RULES, 1.0)
    assert nv.stop_pct <= 0.05 + 1e-9 and nv.stop_basis == "5% cap"
    assert nv.qty == 27 and nv.max_loss_usd == pytest.approx(258.1, abs=0.1)   # rounded up: 26.2 -> 27
    ko = plan_entry(us("KO"), 69.05, 0.011, "steady", RULES, 1.0)
    assert ko.stop_pct == pytest.approx(0.0275, abs=0.002) and "ATR" in ko.stop_basis
    assert ko.cost_usd > 1.7 * nv.cost_usd                  # tighter stop, bigger position...
    assert ko.max_loss_usd == pytest.approx(250, abs=5)      # ...same loss
    assert nv.target == pytest.approx(nv.limit * 1.2, abs=0.01) and nv.target_qty == 13


def test_rounding_up_is_capped_and_so_is_the_position():
    sh = plan_entry(us("SH"), 38.2, 0.006, "steady", RULES, 1.0)      # 1.5% stop: $250 would need $17k
    assert sh.sized_by == "max position" and sh.cost_usd <= 10_000 and sh.max_loss_usd < 250
    dbs = plan_entry(Instrument("D05", "SGX", 8, "SGD", 100), 49.2, 0.012, "steady", RULES, 0.77)
    assert dbs.qty == 200 and dbs.sized_by == "rounded down"          # a 3rd lot would lose $342
    down = plan_entry(us(), 190.40, 0.036, "volatile", replace(RULES, round_up=False), 1.0)
    assert down.qty == 26 and down.max_loss_usd <= 250


def test_tokyo_trades_whole_lots_and_blocks_a_lot_that_loses_too_much():
    toyota = Instrument("7203", "TSEJ", 3, "JPY", 100)
    p = plan_entry(toyota, 2950, 0.018, "steady", RULES, 0.0067)
    assert p.qty % 100 == 0 and p.qty == 300
    assert p.max_loss_usd <= 250 * 1.2
    assert p.stop == round(p.stop) and p.target % 5 == 0     # yen ticks
    q = plan_entry(Instrument("6758", "TSEJ", 4, "JPY", 100), 9000, 0.02, "core", RULES, 0.0067)
    assert q.qty == 0 and "fixed loss" in q.problem


def test_london_prices_in_pence_are_valued_in_pounds():
    hsba = Instrument("HSBA", "LSE", 5, "GBP", 1, 100.0)
    p = plan_entry(hsba, 1000.0, 0.015, "steady", RULES, 1.27)
    assert p.qty == 533
    assert p.cost_local == pytest.approx(533 * 10.05)          # pounds, not pence
    assert p.max_loss_usd == pytest.approx(250, abs=2)


def test_hong_kong_needs_a_known_board_lot():
    p = plan_entry(Instrument("700", "SEHK", 6, "HKD", 0), 500, 0.02, "core", RULES, 0.128)
    assert p.qty == 0 and "board lot" in p.problem
    ok = plan_entry(Instrument("9988", "SEHK", 7, "HKD", 100), 152.3, 0.03, "core", RULES, 0.128)
    assert ok.qty == 300 and ok.target_qty == 100


def test_a_single_lot_sells_whole_at_the_target():
    p = plan_entry(Instrument("S68", "SGX", 8, "SGD", 100), 70.0, 0.012, "steady", RULES, 0.77)
    assert p.qty == 100 and p.target_qty == 100


def test_weather_scales_the_fixed_loss():
    half = plan_entry(us(), 190.40, 0.036, "volatile", RULES, 1.0, risk_usd=125)
    assert half.qty == 14 and half.max_loss_usd <= 150
    none = plan_entry(us(), 190.40, 0.036, "volatile", RULES, 1.0, risk_usd=0)
    assert none.qty == 0 and "Risk-Off" in none.problem


# ---------------------------------------------------------------- buckets

def test_bucket_mix_measures_money_and_daily_swing():
    hs = [Holding("A", "steady", 5000, 5000, 0, 0.012), Holding("B", "steady", 5000, 5000, 0, 0.012),
          Holding("C", "volatile", 5000, 5000, 0, 0.05)]
    mix = {b.id: b for b in bucket_mix(hs, RULES)}
    assert mix["volatile"].money_share == pytest.approx(1 / 3)
    assert mix["volatile"].swing_share == pytest.approx(250 / (250 + 120))
    assert sum(b.money_share for b in mix.values()) == pytest.approx(1.0)
    assert mix["volatile"].room == 6 and mix["core"].room == 10
    before, after = swing_share_after(hs, Holding("D", "volatile", 5000, 5000, 0, 0.05))
    assert after > before


def test_room_is_limited_by_the_whole_book():
    hs = [Holding(str(i), "core", 5000, 5000, 0, 0.03) for i in range(10)]
    hs += [Holding(f"s{i}", "steady", 5000, 5000, 0, 0.01) for i in range(9)]
    mix = {b.id: b for b in bucket_mix(hs, RULES)}
    assert mix["volatile"].room == 1 and mix["steady"].room == 1 and mix["core"].room == 0


# ---------------------------------------------------------------- strikes

def _closed(journal, key, day, pct, cid=1):
    t = journal.open_trade(key=key, symbol=key.split(":")[0], market="US", con_id=cid, currency="USD",
                           bucket="volatile", entry=100.0, qty=10, atr=0.04, stop=95.0, target=120.0,
                           target_qty=5, opened_at=datetime.fromisoformat(day).replace(tzinfo=timezone.utc))
    journal.add_exit(t.id, 10, 100 * (1 + pct), "stop")
    return journal.close_trade(t.id, magnifier=1, usd_per_unit=1, strike_loss_pct=RULES.strike_loss_pct,
                               at=datetime.fromisoformat(day).replace(hour=20, tzinfo=timezone.utc))


def test_two_stop_outs_lock_a_stock_until_you_unlock_it():
    j = Journal(":memory:")
    _closed(j, "MP:US", "2026-09-11", -0.05)
    s = strike_status("MP:US", j.closed_trades("MP:US"), date(2026, 9, 14), RULES)
    assert (s.strikes, s.tries_left, s.locked) == (1, 1, False)
    _closed(j, "MP:US", "2026-09-24", -0.05)
    s = strike_status("MP:US", j.closed_trades("MP:US"), date(2026, 12, 25), RULES)
    assert s.locked and s.manual and s.unlocks_on is None       # months later: still locked
    j.unlock("MP:US", at=datetime(2026, 12, 26, tzinfo=timezone.utc))
    s = strike_status("MP:US", j.closed_trades("MP:US"), date(2026, 12, 26), RULES, j.last_unlock("MP:US"))
    assert not s.locked and s.strikes == 0 and s.tries_left == 2


def test_a_timed_lock_ends_by_itself():
    rules = replace(RULES, lockout_days=10)
    j = Journal(":memory:")
    _closed(j, "MP:US", "2026-09-11", -0.05)
    _closed(j, "MP:US", "2026-09-24", -0.05)
    s = strike_status("MP:US", j.closed_trades("MP:US"), date(2026, 9, 25), rules)
    assert s.locked and s.locked_until == date(2026, 10, 8) and s.unlocks_on == date(2026, 10, 9)
    assert not strike_status("MP:US", j.closed_trades("MP:US"), date(2026, 10, 9), rules).locked


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
    assert (stop.qty, stop.order_type, stop.tif) == (27, "STP", "GTC")
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
    assert stop.qty == 14 and not managed(broker, "target")
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
    broker.place(OrderSpec(2, "SELL", "STP", 200, stop_price=66.0, tif="GTC"))  # the user's own stop
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

def test_two_stop_outs_then_the_gate_blocks_a_third_try_until_unlocked():
    broker, journal, svc = make()
    broker.add(us("MP", 9), 64.0, 0.061)
    for day, entry, exit_px in (("2026-09-10", 64.0, 59.5), ("2026-09-14", 62.5, 58.5)):
        broker.clock = datetime.fromisoformat(day).replace(hour=15, tzinfo=timezone.utc)
        broker._price[9] = entry
        svc.cycle()
        assert svc.enter("MP", bucket="volatile", limit=entry, setup="Breakout")["ok"]
        svc.cycle()
        broker.clock += timedelta(days=1)
        broker.set_price(9, exit_px)
        svc.cycle()
    assert [t.strike for t in journal.closed_trades("MP:US")] == [1, 1]
    assert [t.setup for t in journal.closed_trades("MP:US")] == ["Breakout", "Breakout"]
    r = svc.enter("MP", limit=58.5)
    assert not r["ok"] and "two-strike" in r["message"]
    broker.clock = datetime(2026, 11, 1, 15, tzinfo=timezone.utc)
    assert svc.check("MP")["verdict"] == "blocked"                   # a month on, still locked
    svc.unlock("MP")
    assert svc.check("MP")["verdict"] != "blocked"


def test_the_gate_blocks_a_full_bucket_and_checks_cash():
    broker, journal, svc = make(cash=100_000)
    for i in range(7):
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
    for i in range(8):                                      # the 8th breaks the Volatile cap of 7
        buy(broker, svc, broker.add(us(f"V{i}", 100 + i), 50.0, 0.05), 50.0)
    assert len(managed(broker, "stop")) == 8
    assert any("over its cap of 7" in e["message"] for e in journal.events(60))


def test_sector_capital_and_open_risk_caps_block():
    broker, journal, svc = make(cash=200_000)
    for i in range(4):
        buy(broker, svc, broker.add(us(f"S{i}", 10 + i, sector="Semiconductors"), 100.0, 0.02), 100.0)
    broker.add(us("S9", 19, sector="Semiconductors"), 100.0, 0.02)
    res = svc.check("S9")
    assert any(c["title"] == "Sector full: Semiconductors" for c in res["checks"])
    svc.rules = replace(svc.rules, max_invested_usd=25_000, max_open_risk_usd=900)
    broker.add(us("KO", 2), 68.0, 0.011)
    titles = {c["title"] for c in svc.check("KO")["checks"]}
    assert {"Over the capital limit", "Too much open risk"} <= titles


def test_entry_refuses_when_the_price_moved_or_ibkr_disagrees(monkeypatch):
    broker, journal, svc = make()
    broker.add(us(), 190.0, 0.036)
    svc.cycle()
    r = svc.enter("NVDA", expect_qty=999)
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
    ps = {p["key"]: p for p in state["positions"]}
    assert len(ps) == 13 and all(p["protected"] for p in ps.values())
    for p in ps.values():
        assert p["risk_usd"] <= 250 * 1.2 + 1                 # a fixed loss, give or take a lot
        if p["stop_state"] != "initial":
            assert p["stop"] >= p["entry"] - 1e-9
    assert ps["7203:TSEJ"]["risk_usd"] <= 125 * 1.2           # Tokyo is Defensive: half size
    assert ps["JNJ:US"]["time_stop"] and not ps["MSFT:US"]["time_stop"]
    assert ps["NVDA:US"]["adds"] == 1 and not ps["NVDA:US"]["can_add"]
    assert ps["GOOGL:US"]["can_add"]
    assert ps["WMT:US"]["half_sold"] and ps["WMT:US"]["stop_state"] == "trailing"
    assert ps["NVDA:US"]["earnings"]["days"] == 2
    for b in state["buckets"]:
        assert b["n"] <= b["cap"]
    strikes = {s["key"]: s for s in state["strikes"]}
    assert strikes["MP:US"]["locked"] and strikes["MP:US"]["manual"]
    assert strikes["AMD:US"]["strikes"] == 1 and strikes["AMD:US"]["held"]
    assert state["weather"]["TSEJ"]["label"] == "Defensive" and state["weather"]["US"]["label"] == "Risk-On"
    assert not state["breaker"]["paused"] and state["breaker"]["losses_in_a_row"] == 2
    st = state["journal"]["stats"]
    assert st["all"]["trades"] == 11 and st["all"]["expectancy_r"] > 0
    assert st["by_bucket"]["volatile"]["win_rate"] == 0


def test_demo_checks_show_each_kind_of_verdict(demo):
    _, state = demo
    c = state["demo_checks"]
    assert c["MP:US"]["verdict"] == "blocked"
    assert c["700:SEHK"]["verdict"] == "blocked" and c["700:SEHK"]["plan"]["qty"] == 0
    assert c["HOOD:US"]["verdict"] == "warning"
    assert any(x["title"].startswith("Earnings in 5 days") for x in c["HOOD:US"]["checks"])
    assert any("Leveraged inverse" in x["title"] for x in c["SQQQ:US"]["checks"])
    assert any(x["title"] == "Last try on this stock" for x in c["COIN:US"]["checks"])
    assert not any(x["title"] == "Above the swing guide" for x in c["HD:US"]["checks"])   # Steady lowers it


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
        assert res["plan"]["qty"] == 27 and res["verdict"] in ("allowed", "warning")
        assert call("/api/enter", {"symbol": "NVDA", "expect_qty": 27, "setup": "Pullback"})["ok"]
        assert call("/api/settings", {"time_stop_weeks": 6})["ok"]
        assert svc.rules.time_stop_weeks == 6
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


# ---------------------------------------------------------------- adds, time stops, settings

def test_add_to_a_winner_raises_the_stop_so_the_position_risks_one_loss():
    broker, journal, svc = make()
    nv = broker.add(us(), 190.0, 0.036)
    buy(broker, svc, nv, 190.0)
    r = svc.add_to("NVDA:US")
    assert not r["ok"] and any("Only winners" in c["detail"] for c in r["check"]["checks"])   # stop below entry
    broker.set_price(1, 201.0); svc.cycle()                            # +5.8%: stop to entry
    r = svc.add_to("NVDA:US", limit=201.0)
    assert r["ok"], r["message"]
    svc.cycle()
    t = journal.open_trades()[0]
    assert t.adds == 1 and t.qty > 27
    assert t.stop == pytest.approx(190.95)                             # 5% under the 201.00 add
    stop, = managed(broker, "stop")
    assert stop.qty == t.qty and stop.stop_price == pytest.approx(t.stop)
    risk = (t.entry - t.stop) * t.qty
    assert risk < 250                                                  # the whole position: under one loss
    again = svc.add_to("NVDA:US", limit=201.0)
    assert not again["ok"] and any("Already added" in c["detail"] for c in again["check"]["checks"])


def test_time_stop_flags_then_sells_when_told_to():
    broker, journal, svc = make()
    zs = broker.add(us("ZS", 3), 288.0, 0.039)
    buy(broker, svc, zs, 288.0)
    broker.clock += timedelta(days=29)
    broker.set_price(3, 292.0)
    state = svc.cycle()
    assert state["positions"][0]["time_stop"]
    assert any(e["kind"] == "time-stop" for e in journal.events(10))
    svc.rules = replace(svc.rules, time_stop_action="sell")
    svc._said.clear()
    svc.cycle()
    broker.set_price(3, 292.0); svc.cycle()
    t = journal.closed_trades()[0]
    assert {e.kind for e in journal.exits(t.id)} == {"time-stop"}
    assert not managed(broker)


def test_selling_from_the_dashboard_cancels_the_stop_and_target():
    broker, journal, svc = make()
    nv = broker.add(us(), 190.0, 0.036)
    buy(broker, svc, nv, 190.0)
    assert svc.exit_position("NVDA")["ok"]
    svc.cycle()
    assert journal.closed_trades()[0].status == "closed" and not managed(broker)
    assert {e.kind for e in journal.exits(journal.closed_trades()[0].id)} == {"exit"}


def test_settings_are_clamped_kept_and_survive_a_restart():
    broker, journal, svc = make()
    svc.set_settings(time_stop_weeks=100, earnings_warn_days=3)
    assert svc.rules.time_stop_weeks == 26 and svc.rules.earnings_warn_days == 3
    again = TradingService(broker, journal, RULES)
    assert again.rules.time_stop_weeks == 26


# ---------------------------------------------------------------- weather

def test_weather_reads_the_real_spy_snapshot():
    from fireplanner.data import SnapshotProvider
    sp = SnapshotProvider("data/snapshots")
    r = read_weather("US", "SPY", {"index": sp.history("SPY")["close"], "vix": sp.history("VIX")["close"],
                                   "breadth": sp.history("RSP")["close"]}, RULES)
    assert r.label in RULES.weather_sizing and 0 <= r.score <= 1
    assert read_weather("US", "SPY", {"index": sp.history("SPY")["close"].tail(50)}, RULES).label is None


def _weather(svc, label, mult):
    svc.weather = MarketWeather(svc.rules, {}, lambda s: None)
    svc.weather.readings = {"US": WeatherReading("US", "SPY", label, 0.3, mult, "2026-09-01", {})}


def test_defensive_weather_halves_size_and_risk_off_blocks():
    broker, journal, svc = make()
    broker.add(us(), 190.4, 0.036)
    svc.cycle()
    _weather(svc, "Defensive", 0.5)
    res = svc.check("NVDA")
    assert res["plan"]["risk_budget_usd"] == 125 and res["verdict"] == "warning"
    _weather(svc, "Risk-Off", 0.0)
    assert svc.check("NVDA")["verdict"] == "blocked"


# ---------------------------------------------------------------- circuit breakers

def test_three_stop_outs_in_a_row_pause_buys_until_resumed():
    broker, journal, svc = make()
    for i, day in enumerate(("2026-09-01", "2026-09-02", "2026-09-03")):
        _closed(journal, f"L{i}:US", day, -0.05, cid=50 + i)
    now = datetime(2026, 9, 4, 15, tzinfo=timezone.utc)
    st = breaker_state(journal, RULES, now)
    assert st.paused and st.losses_in_a_row == 3
    _closed(journal, "W:US", "2026-09-04", 0.10, cid=60)             # a win doesn't lift it...
    assert breaker_state(journal, RULES, now).paused
    journal.breaker_event("resume", at=datetime(2026, 9, 4, 18, tzinfo=timezone.utc))   # ...you do
    assert not breaker_state(journal, RULES, datetime(2026, 9, 4, 19, tzinfo=timezone.utc)).paused


def test_daily_loss_limit_and_kill_switch():
    broker, journal, svc = make()
    broker.add(us(), 190.4, 0.036)
    broker.add(us("KO", 2), 69.0, 0.011)
    svc.cycle()
    broker._price[1] = 190.4
    assert svc.enter("NVDA", limit=180.0)["ok"]                        # a limit below the market: rests
    assert [o for o in broker.open_orders() if o.role == "entry"]
    r = svc.kill()
    assert "1 buy order" in r["message"]
    assert not [o for o in broker.open_orders() if o.role == "entry"]
    assert svc.check("KO")["verdict"] == "blocked"
    svc.resume()
    assert svc.check("KO")["verdict"] != "blocked"
    for i in range(3):
        _closed(journal, f"D{i}:US", T0.date().isoformat(), -0.30, cid=70 + i)      # -$300 each
    broker.clock = T0.replace(hour=20)
    st = breaker_state(journal, RULES, broker.clock)
    assert st.paused and any("Daily loss limit" in x for x in st.reasons)


def test_buys_per_day_are_capped():
    broker, journal, svc = make(cash=1_000_000)
    svc.rules = replace(svc.rules, max_entries_per_day=2)
    for i in range(3):
        broker.add(us(f"B{i}", 80 + i), 50.0, 0.05)
    svc.cycle()
    assert svc.enter("B0", limit=40.0)["ok"] and svc.enter("B1", limit=40.0)["ok"]
    r = svc.enter("B2", limit=40.0)
    assert not r["ok"] and "Daily buy limit" in r["message"]


# ---------------------------------------------------------------- earnings

def test_earnings_inside_the_window_warn_or_block():
    broker, journal, svc = make()
    broker.add(us(), 190.4, 0.036)
    svc.cycle()
    svc.set_earnings("NVDA", "2026-09-04")
    res = svc.check("NVDA")
    assert any(c["title"].startswith("Earnings in 3 days") and c["level"] == "warn" for c in res["checks"])
    svc.rules = replace(svc.rules, earnings_block=True)
    assert svc.check("NVDA")["verdict"] == "blocked"


def test_fmp_symbols_and_parsing():
    assert fmp_symbol("BRK.B", "US") == "BRK-B" and fmp_symbol("700", "SEHK") == "0700.HK"
    assert fmp_symbol("D05", "SGX") == "D05.SI" and fmp_symbol("7203", "TSEJ") == "7203.T"
    assert fmp_symbol("HSBA", "LSE") == "HSBA.L"
    rows = [{"date": "2026-07-30", "epsActual": 1.2}, {"date": "2026-10-29", "epsActual": None}, {"x": 1}]
    assert parse_fmp(rows, date(2026, 9, 30)) == date(2026, 10, 29)
    assert parse_fmp({"Error Message": "bad key"}, date(2026, 9, 30)) is None


def test_earnings_calendar_fetches_but_never_overrides_your_date():
    j = Journal(":memory:")
    calls = []

    def fake(url):
        calls.append(url)
        return [{"date": "2026-10-20"}]

    cal = EarningsCalendar(j, api_key="k", fetch=fake)
    j.set_earnings("AAPL:US", date(2026, 10, 15), source="manual")
    assert cal.refresh(["AAPL:US", "700:SEHK"], date(2026, 9, 30)) == 1
    assert "symbol=0700.HK" in calls[0]
    up = cal.upcoming(date(2026, 9, 30))
    assert up["AAPL:US"]["date"] == "2026-10-15" and up["700:SEHK"]["days"] == 20
    assert cal.refresh(["700:SEHK"], date(2026, 9, 30)) == 0             # once a day


# ---------------------------------------------------------------- journal

def test_journal_stats_are_in_r():
    j = Journal(":memory:")
    _closed(j, "A:US", "2026-09-01", -0.05)                              # a full stop-out
    t = _closed(j, "B:US", "2026-09-02", 0.10, cid=2)                     # twice the risk
    assert t.r_multiple is None                                           # no risk recorded: from prices
    st = journal_stats(j.closed_trades(), datetime(2026, 9, 3, tzinfo=timezone.utc))
    assert st["all"]["trades"] == 2 and st["all"]["win_rate"] == 0.5
    assert st["all"]["expectancy_r"] == pytest.approx(0.5)
    assert st["by_bucket"]["volatile"]["avg_win_r"] == pytest.approx(2.0)


def test_an_older_journal_is_upgraded_in_place(tmp_path):
    import sqlite3
    path = tmp_path / "old.sqlite"
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE trades (id INTEGER PRIMARY KEY, key TEXT, symbol TEXT, market TEXT, con_id INTEGER,"
               " currency TEXT, bucket TEXT, entry REAL, qty INTEGER, initial_qty INTEGER, atr REAL, stop REAL,"
               " initial_stop REAL, target REAL, target_qty INTEGER, stop_state TEXT, high_water REAL,"
               " opened_at TEXT, closed_at TEXT, status TEXT, exited_qty INTEGER, exit_value REAL,"
               " realized_pct REAL, realized_usd REAL, strike INTEGER, oca_rev INTEGER, source TEXT, note TEXT)")
    db.commit()
    db.close()
    j = Journal(path)
    t = j.open_trade(key="X:US", symbol="X", market="US", con_id=1, currency="USD", bucket="core", entry=10,
                     qty=1, atr=None, stop=9.5, target=11.5, target_qty=1, setup="Base")
    assert t.setup == "Base" and t.adds == 0


def test_journal_csv_lists_closed_trades():
    from fireplanner.trading.server import journal_csv
    j = Journal(":memory:")
    _closed(j, "A:US", "2026-09-01", -0.05)
    lines = journal_csv(j).strip().splitlines()
    assert lines[0].startswith("id,stock,type,setup") and "A:US" in lines[1]


# ---------------------------------------------------------------- breadth

def _series(values, end="2026-09-30"):
    import numpy as np
    import pandas as pd
    return pd.Series(np.asarray(values, dtype=float), index=pd.bdate_range(end=end, periods=len(values)))


def test_breadth_counts_stocks_above_their_averages():
    import numpy as np
    from fireplanner.trading.breadth import breadth_of
    up = _series(np.linspace(50, 100, 260))                     # above every average
    down = _series(np.linspace(100, 50, 260))                   # below every average
    young = _series(np.linspace(50, 60, 60))                    # too short for the 200-day
    pct, counted, as_of = breadth_of({"A": up, "B": down, "C": young})
    assert pct[20] == pytest.approx(2 / 3) and pct[200] == pytest.approx(0.5)
    assert counted == 3 and as_of == "2026-09-30"


def test_breadth_fetches_a_few_per_cycle_and_backs_off_for_pacing():
    import numpy as np
    import pandas as pd
    from fireplanner.trading.breadth import MarketBreadth
    from fireplanner.trading.broker import PacingDeferred
    calls = []

    def bars(text):
        calls.append(text)
        if text == "BAD:SEHK":
            raise LookupError("no such stock")
        if len(calls) == 7:
            raise PacingDeferred("slow down")
        return pd.DataFrame({"close": _series(np.linspace(50, 100, 260))})

    mb = MarketBreadth({"US": ["A", "B", "C", "D"], "SEHK": ["700", "BAD", "5"]}, bars, per_cycle=2)
    day = date(2026, 9, 30)
    assert mb.step(day) == 2 and mb.reading("US").score is None      # 2 of 4: under 60% coverage
    mb.step(day)
    us = mb.reading("US")
    assert us.counted == 4 and us.score == pytest.approx(1.0)
    mb.step(day)                                                      # 700, then BAD fails
    assert "BAD" in mb._failed["SEHK"]
    assert mb.step(day) == 0 and mb.pending() == 1                    # deferred, not failed
    mb.step(day)
    assert mb.pending() == 0 and mb.reading("SEHK").counted == 2


def test_narrow_breadth_pulls_the_weather_down():
    from fireplanner.trading.breadth import MarketBreadth
    from fireplanner.trading.weather import band_label
    base = WeatherReading("US", "SPY", "Constructive", 0.65, 1.0, "2026-09-30", {})
    mw = MarketWeather(RULES, {"US": "SPY"}, lambda s: None,
                       MarketBreadth({"US": ["A"]}, lambda s: None))
    mw._regime = {"US": base}
    mw.breadth._closes["US"] = {"A": _series([100.0] * 199 + [50.0])}   # below every average
    mw.rescale(RULES)
    r = mw.readings["US"]
    assert r.breadth["score"] == 0 and r.regime_label == "Constructive"
    assert r.score == pytest.approx(0.75 * 0.65) and r.label == band_label(0.4875) == "Neutral"


# ---------------------------------------------------------------- anchored VWAP

def _chart(n=200, gap_at=40, gap=0.06, brk_at=15):
    import numpy as np
    import pandas as pd
    close = np.full(n, 100.0)
    close[:-brk_at] = np.linspace(80, 100, n - brk_at) + np.sin(np.arange(n - brk_at)) * 0.5
    close[-brk_at:] = np.linspace(106, 112, brk_at)
    idx = pd.bdate_range(end="2026-09-30", periods=n)
    df = pd.DataFrame({"open": close, "high": close * 1.005, "low": close * 0.995, "close": close,
                       "volume": 1_000_000.0}, index=idx)
    i = n - gap_at
    df.iloc[i, 0] = df["close"].iloc[i - 1] * (1 + gap)
    df.iloc[i, 1] = max(df.iloc[i, 0], df.iloc[i, 3]) * 1.01
    df.iloc[i, 4] = 4_000_000.0
    return df


def test_avwap_is_the_volume_weighted_price_since_the_anchor():
    import pandas as pd
    from fireplanner.trading.avwap import avwap_since
    idx = pd.bdate_range("2026-09-01", periods=3)
    df = pd.DataFrame({"high": [10, 20, 30], "low": [10, 20, 30], "close": [10, 20, 30],
                       "volume": [1, 1, 2]}, index=idx)
    assert avwap_since(df, idx[1]) == pytest.approx((20 + 60) / 3)
    assert avwap_since(df, idx[0]) == pytest.approx((10 + 20 + 60) / 4)


def test_anchors_find_the_gap_the_breakout_leg_and_a_pullback():
    from fireplanner.trading.avwap import anchored_vwaps, find_breakout_day, find_gap_day, pullback_level
    df = _chart()
    day, g = find_gap_day(df)
    assert day == df.index[-40] and g > 0.05
    assert find_breakout_day(df) == df.index[-15]            # the start of the leg, not yesterday
    anchors = anchored_vwaps(df)
    kinds = {a.kind for a in anchors}
    assert {"gap", "breakout"} <= kinds
    brk = next(a for a in anchors if a.kind == "breakout")
    assert 0 < brk.distance < 0.05
    pb = pullback_level(anchors, float(df["close"].iloc[-1]), 0.02)
    assert pb is not None and pb.kind == "breakout"
    # a known earnings date replaces the gap search
    with_date = anchored_vwaps(df, earnings_day=df.index[-40].date())
    assert any(a.kind == "earnings" for a in with_date) and not any(a.kind == "gap" for a in with_date)


def test_avwap_on_real_ibkr_snapshots():
    from fireplanner.data import SnapshotProvider
    from fireplanner.trading.avwap import anchored_vwaps
    sp = SnapshotProvider("data/snapshots")
    ftnt = anchored_vwaps(sp.history("FTNT").tail(260))
    gap = next(a for a in ftnt if a.kind == "gap")
    assert gap.label == "Gap up 30 Jul" and gap.distance < 0      # FTNT closed under its gap-up VWAP


def test_the_check_shows_vwap_levels_and_pullback_limits(demo):
    _, state = demo
    hood = state["demo_checks"]["HOOD:US"]
    assert hood["avwap"]["pullback"]["kind"] == "breakout"
    assert any(c["title"].startswith("Pullback level: Breakout") for c in hood["checks"])
    coin = state["demo_checks"]["COIN:US"]
    assert any(c["level"] == "warn" and c["title"].startswith("Below its") for c in coin["checks"])
    wl = {w["key"]: w for w in state["watchlist"]}
    assert wl["META:US"]["avwap"]["pullback"] is not None
    assert wl["NVDA:US"]["avwap"]["pullback"] is None               # extended: nothing within reach


def test_buying_at_the_pullback_level_moves_the_stop_down_with_it():
    broker, journal, svc = make()
    broker.add(us("META", 44), 742.0, 0.022)
    broker.set_bars("META", _chart(gap_at=40, brk_at=15).assign(
        **{c: lambda d, c=c: d[c] * 742.0 / 112.0 for c in ("open", "high", "low", "close")}))
    svc.cycle()
    res = svc.check("META")
    pb = res["avwap"]["pullback"]
    at = svc.check("META", limit=round(pb["avwap"], 2))
    assert any(c["title"].startswith("Buying at the") for c in at["checks"])
    assert at["plan"]["stop"] < res["plan"]["stop"]


def test_demo_weather_blends_breadth(demo):
    _, state = demo
    w = state["weather"]
    assert w["TSEJ"]["breadth"]["pct50"] < 0.4 and w["TSEJ"]["label"] == "Defensive"
    assert w["US"]["breadth"]["score"] > 0.6 and w["US"]["label"] == "Risk-On"
    assert all(x["breadth"]["counted"] == x["breadth"]["basket"] for x in w.values())


# ---------------------------------------------------------------- hosting

def test_a_snapshot_of_the_running_dashboard_is_one_static_file(tmp_path):
    from fireplanner.cli import main
    from fireplanner.trading.server import make_server
    broker, journal, svc = make()
    nv = broker.add(us(), 190.0, 0.036)
    buy(broker, svc, nv, 190.0)
    httpd = make_server(svc, "127.0.0.1", 0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    out = tmp_path / "site" / "index.html"
    try:
        assert main(["trade", "snapshot", "--url", f"http://127.0.0.1:{httpd.server_address[1]}",
                     "-o", str(out)]) == 0
    finally:
        httpd.shutdown()
    page = out.read_text()
    assert "const LIVE = false;" in page and '"NVDA:US"' in page
    assert 'content="noindex, nofollow"' in page
    assert "http://" not in page.split("<script>")[0] and "<link" not in page     # nothing loaded from elsewhere


def test_the_demo_page_says_it_is_simulated(demo):
    from fireplanner.trading.server import render_dashboard
    _, state = demo
    page = render_dashboard(state)
    assert state["mode"] == "sim" and "every position, price, date and index series on this page is simulated" in page


# ---------------------------------------------------------------- live IBKR connection

class _Evt:
    def __iadd__(self, fn):
        return self

    def __isub__(self, fn):
        return self


class _FakeIB:
    accounts = ["DU1234567"]

    def __init__(self):
        self.up = False
        self.execDetailsEvent, self.orderStatusEvent, self.errorEvent = _Evt(), _Evt(), _Evt()

    def connect(self, host, port, clientId, readonly=False, account=""):
        self.up = True

    def managedAccounts(self):
        return list(self.accounts)

    def isConnected(self):
        return self.up

    def disconnect(self):
        self.up = False

    def reqMarketDataType(self, t):
        pass

    def reqExecutions(self):
        return []


def test_a_live_account_behind_a_paper_port_is_refused(monkeypatch):
    import ib_async
    from fireplanner.trading.ib_broker import LiveAccountRefused, mask_account
    monkeypatch.setattr(ib_async, "IB", type("LiveIB", (_FakeIB,), {"accounts": ["U7654321"]}))
    b = IBBroker(port=7497)                                  # a paper port, but a live account
    with pytest.raises(LiveAccountRefused, match="LIVE account"):
        b.connect()
    assert not b.connected
    monkeypatch.setattr(ib_async, "IB", _FakeIB)
    ok = IBBroker(port=4004)                                 # the Docker relay's paper port
    ok.connect()
    st = ok.status()
    assert st["connected"] and st["account_type"] == "paper" and st["account"] == mask_account("DU1234567")
    assert "1234567" not in st["account"]


def test_the_docker_relay_ports_are_classified():
    assert IBBroker(port=4004).mode == "paper"
    with pytest.raises(ValueError, match="LIVE"):
        IBBroker(port=4003)


def test_doctor_runs_every_check_without_ordering():
    from fireplanner.trading.doctor import PROBES, run_doctor
    broker = SimBroker(clock=T0)
    for i, (mkt, sym) in enumerate(PROBES.items()):
        ccy = {"US": "USD", "LSE": "GBP", "SEHK": "HKD", "SGX": "SGD", "TSEJ": "JPY"}[mkt]
        broker.add(Instrument(sym, mkt, 900 + i, ccy, 100 if mkt in ("SGX", "TSEJ", "SEHK") else 1), 50.0, 0.02)
    broker.set_bars("SPY", _chart())
    findings = run_doctor(broker, RULES, {"enabled": False, "weather": {"US": "SPY", "TSEJ": "1306:JP"}})
    titles = [f.title for f in findings]
    assert "Simulated broker" in titles and "The gateway accepts orders" in titles
    assert sum(t.endswith("prices come through") for t in titles) == 5
    assert any(f.title == "Market-weather index not found" and "1306:JP" in f.detail for f in findings)
    assert not broker.open_orders()                          # the doctor never orders


def test_the_snapshot_carries_the_connection():
    broker, journal, svc = make()
    state = svc.cycle()
    assert state["connection"]["connected"] and state["connection"]["account_type"] == "sim"


def test_page_has_collapsible_cards_and_relative_api_paths():
    from fireplanner.trading.server import render_dashboard
    page = render_dashboard({})
    assert 'id="fold-all"' in page and 'id="unfold-all"' in page and "button.fold" in page
    assert '"/api/' not in page and 'api("api/state")' in page      # works behind /desk/ too
