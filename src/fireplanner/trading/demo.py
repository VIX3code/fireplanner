"""A realistic sample book, produced by running the real code on the simulator.

Nothing here is drawn by hand: every position below was bought through the
pre-trade check, filled by `SimBroker`, protected by the guardian, and moved
by a price path. The strike history comes from real stop-outs. So the demo
page is also an end-to-end test of the whole loop, and ``tests/test_trading``
asserts on it.

All tickers, prices and dates are illustrative, not market data.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from .journal import Journal
from .markets import Instrument
from .rules import TradingRules
from .service import TradingService
from .sim import SimBroker

__all__ = ["build_demo", "DEMO_TODAY"]

DEMO_TODAY = datetime(2026, 9, 30, 15, 0, tzinfo=timezone.utc)

# key, con_id, currency, lot, magnifier, description, atr
_LISTINGS = [
    ("JNJ:US", 11, "USD", 1, 1, "Johnson & Johnson", 0.012),
    ("KO:US", 12, "USD", 1, 1, "Coca-Cola", 0.011),
    ("PG:US", 13, "USD", 1, 1, "Procter & Gamble", 0.012),
    ("WMT:US", 14, "USD", 1, 1, "Walmart", 0.014),
    ("HSBA:LSE", 15, "GBP", 1, 100, "HSBC Holdings", 0.015),
    ("D05:SGX", 16, "SGD", 100, 1, "DBS Group", 0.012),
    ("MSFT:US", 21, "USD", 1, 1, "Microsoft", 0.021),
    ("GOOGL:US", 22, "USD", 1, 1, "Alphabet", 0.023),
    ("AMZN:US", 23, "USD", 1, 1, "Amazon", 0.024),
    ("7203:TSEJ", 24, "JPY", 100, 1, "Toyota Motor", 0.022),
    ("9988:SEHK", 25, "HKD", 100, 1, "Alibaba Group", 0.031),
    ("NVDA:US", 31, "USD", 1, 1, "NVIDIA", 0.036),
    ("ZS:US", 32, "USD", 1, 1, "Zscaler", 0.039),
    ("PLTR:US", 33, "USD", 1, 1, "Palantir", 0.052),
    ("AMD:US", 34, "USD", 1, 1, "Advanced Micro Devices", 0.043),
    ("MP:US", 41, "USD", 1, 1, "MP Materials", 0.061),
    ("COIN:US", 42, "USD", 1, 1, "Coinbase", 0.048),
    ("HOOD:US", 43, "USD", 1, 1, "Robinhood", 0.056),
    ("META:US", 44, "USD", 1, 1, "Meta Platforms", 0.022),
    ("HD:US", 45, "USD", 1, 1, "Home Depot", 0.014),
    ("700:SEHK", 46, "HKD", 100, 1, "Tencent Holdings", 0.021),
    ("SH:US", 47, "USD", 1, 1, "ProShares Short S&P500", 0.009),
    ("SQQQ:US", 48, "USD", 1, 1, "ProShares UltraPro Short QQQ", 0.047),
]

# key, confirmed bucket (None = leave as suggested), days ago, entry, high since, last
_BOOK = [
    ("JNJ:US", "steady", 12, 158.40, 164.00, 163.10),
    ("KO:US", "steady", 8, 68.20, 69.60, 69.05),
    ("PG:US", None, 5, 162.50, 163.40, 160.90),
    ("WMT:US", "steady", 18, 96.30, 106.90, 104.20),
    ("HSBA:LSE", "steady", 9, 1012.0, 1048.0, 1041.0),
    ("D05:SGX", "steady", 7, 49.20, 50.10, 49.86),
    ("MSFT:US", "core", 14, 505.00, 528.00, 521.40),
    ("GOOGL:US", "core", 21, 238.00, 255.00, 252.30),
    ("AMZN:US", "core", 4, 226.50, 229.00, 221.80),
    ("7203:TSEJ", None, 10, 2905.0, 3010.0, 2987.0),
    ("9988:SEHK", "core", 6, 152.30, 158.00, 155.60),
    ("NVDA:US", "volatile", 11, 178.00, 193.00, 190.40),
    ("ZS:US", "volatile", 6, 288.00, 295.00, 279.50),
    ("PLTR:US", "volatile", 9, 172.00, 191.00, 186.90),
]

# key, list of (day, buy price, exit price, exit day) — stop-outs that build the strike board
_HISTORY = [
    ("MP:US", [("2026-09-10", 64.00, 59.90, "2026-09-11"), ("2026-09-14", 62.50, 58.70, "2026-09-24")]),
    ("COIN:US", [("2026-09-18", 350.00, 331.00, "2026-09-22")]),
    ("AMD:US", [("2026-09-21", 168.00, 159.20, "2026-09-25")]),
]
# AMD is bought again after its stop-out: the one allowed re-entry.
_REENTRY = ("AMD:US", "volatile", 2, 162.00, 164.00, 158.40)

_WATCH_ONLY = {"HOOD:US": 118.40, "META:US": 742.00, "HD:US": 402.50, "700:SEHK": 612.0,
               "SH:US": 38.20, "SQQQ:US": 15.10, "MP:US": 61.20, "COIN:US": 336.00}


def _at(day: str, hour: int = 15) -> datetime:
    return datetime.fromisoformat(day).replace(hour=hour, tzinfo=timezone.utc)


def build_demo(rules: TradingRules | None = None, journal_path: str = ":memory:") -> tuple[TradingService, dict]:
    rules = rules or TradingRules()
    broker = SimBroker(cash_usd=120_000.0, clock=_at("2026-09-01"))
    by_key: dict[str, Instrument] = {}
    for key, cid, ccy, lot, mag, name, atr in _LISTINGS:
        sym, mkt = key.split(":")
        from .markets import detect_inverse
        inverse, lev = detect_inverse(name)
        inst = Instrument(sym, mkt, cid, ccy, lot, float(mag), description=name, inverse=inverse, leverage=lev)
        by_key[key] = broker.add(inst, 100.0, atr)

    journal = Journal(journal_path)
    service = TradingService(broker, journal, rules, orders_enabled=True,
                             seed_watchlist=[k for k, *_ in _LISTINGS])

    def buy(key, bucket, when, price):
        broker.clock = when
        broker._price[by_key[key].con_id] = price
        service.cycle()
        r = service.enter(key, bucket=bucket, limit=price)
        if not r.get("ok"):
            raise RuntimeError(f"demo buy of {key} refused: {r['message']}")
        service.cycle()

    def move(key, price, when):
        broker.clock = when
        broker.set_price(by_key[key].con_id, price)
        service.cycle()

    for key, trips in _HISTORY:
        journal.confirm_bucket(key, "volatile")
        for day, entry, exit_px, exit_day in trips:
            buy(key, "volatile", _at(day), entry)
            move(key, exit_px, _at(exit_day))

    opens = sorted(_BOOK + [_REENTRY], key=lambda r: -r[2])
    for key, bucket, days, entry, high, last in opens:
        when = DEMO_TODAY - timedelta(days=days, hours=5)
        if bucket:
            journal.confirm_bucket(key, bucket)
        buy(key, bucket, when, entry)
        move(key, high, when + timedelta(days=max(1, days // 2)))
    for key, bucket, days, entry, high, last in opens:
        move(key, last, DEMO_TODAY - timedelta(minutes=30))
    for key, price in _WATCH_ONLY.items():
        broker._price[by_key[key].con_id] = price

    broker.clock = DEMO_TODAY
    state = service.cycle()
    checks = {}
    for key in ("HOOD:US", "COIN:US", "MP:US", "META:US", "HD:US", "700:SEHK", "SH:US", "SQQQ:US"):
        res = service.check(key)
        checks[key] = res
        if key.endswith(":US"):
            checks[key.split(":")[0]] = res
    state["demo_checks"] = checks
    return service, state
