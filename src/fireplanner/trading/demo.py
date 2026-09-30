"""A realistic sample book, produced by running the real code on the simulator.

Nothing here is drawn by hand: every position was bought through the pre-trade
check, filled by `SimBroker`, protected by the guardian and moved by a price
path. The journal's history comes from real round trips (winners, stop-outs, a
scratch), the NVDA add went through the add check, and the market weather is
the regime model run on price series. So the demo page is also an end-to-end
test of the whole loop, and ``tests/test_trading`` asserts on it.

All tickers, prices, dates and index series are illustrative, not market data.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd

from .breadth import DEFAULT_BASKETS
from .earnings import EarningsCalendar
from .journal import Journal
from .markets import Instrument, detect_inverse
from .rules import TradingRules
from .service import TradingService
from .sim import SimBroker

__all__ = ["build_demo", "DEMO_TODAY"]

DEMO_TODAY = datetime(2026, 9, 30, 15, 0, tzinfo=timezone.utc)

# key, con_id, currency, lot, magnifier, description, atr, sector
_LISTINGS = [
    ("JNJ:US", 11, "USD", 1, 1, "Johnson & Johnson", 0.012, "Pharmaceuticals"),
    ("WMT:US", 14, "USD", 1, 1, "Walmart", 0.014, "Retail"),
    ("HSBA:LSE", 15, "GBP", 1, 100, "HSBC Holdings", 0.015, "Banks"),
    ("D05:SGX", 16, "SGD", 100, 1, "DBS Group", 0.012, "Banks"),
    ("MSFT:US", 21, "USD", 1, 1, "Microsoft", 0.021, "Software"),
    ("GOOGL:US", 22, "USD", 1, 1, "Alphabet", 0.023, "Internet"),
    ("AMZN:US", 23, "USD", 1, 1, "Amazon", 0.024, "Internet"),
    ("7203:TSEJ", 24, "JPY", 100, 1, "Toyota Motor", 0.022, "Auto Manufacturers"),
    ("9988:SEHK", 25, "HKD", 100, 1, "Alibaba Group", 0.031, "Internet"),
    ("NVDA:US", 31, "USD", 1, 1, "NVIDIA", 0.036, "Semiconductors"),
    ("ZS:US", 32, "USD", 1, 1, "Zscaler", 0.039, "Software"),
    ("PLTR:US", 33, "USD", 1, 1, "Palantir", 0.052, "Software"),
    ("AMD:US", 34, "USD", 1, 1, "Advanced Micro Devices", 0.043, "Semiconductors"),
    ("MP:US", 41, "USD", 1, 1, "MP Materials", 0.061, "Mining"),
    ("COIN:US", 42, "USD", 1, 1, "Coinbase", 0.048, "Finance"),
    ("HOOD:US", 43, "USD", 1, 1, "Robinhood", 0.056, "Finance"),
    ("META:US", 44, "USD", 1, 1, "Meta Platforms", 0.022, "Internet"),
    ("HD:US", 45, "USD", 1, 1, "Home Depot", 0.014, "Retail"),
    ("700:SEHK", 46, "HKD", 100, 1, "Tencent Holdings", 0.021, "Internet"),
    ("SH:US", 47, "USD", 1, 1, "ProShares Short S&P500", 0.009, "Funds"),
    ("SQQQ:US", 48, "USD", 1, 1, "ProShares UltraPro Short QQQ", 0.047, "Funds"),
    ("AVGO:US", 51, "USD", 1, 1, "Broadcom", 0.030, "Semiconductors"),
    ("LLY:US", 52, "USD", 1, 1, "Eli Lilly", 0.018, "Pharmaceuticals"),
    ("COST:US", 53, "USD", 1, 1, "Costco", 0.013, "Retail"),
    ("TSM:US", 54, "USD", 1, 1, "Taiwan Semiconductor", 0.028, "Semiconductors"),
    ("SMCI:US", 55, "USD", 1, 1, "Super Micro Computer", 0.060, "Computer Hardware"),
    ("V:US", 56, "USD", 1, 1, "Visa", 0.015, "Finance"),
    ("CRWD:US", 57, "USD", 1, 1, "CrowdStrike", 0.032, "Software"),
]

# Closed round trips: key, type, setup, [(day, price), ...]; the first point is the buy.
_HISTORY = [
    ("LLY:US", "steady", "Pullback", [("2026-07-08", 780.0), ("2026-07-15", 744.0)]),
    ("AVGO:US", "core", "Breakout", [("2026-07-06", 300.0), ("2026-07-20", 346.0), ("2026-07-28", 360.0),
                                     ("2026-08-04", 327.0)]),
    ("TSM:US", "core", "Breakout", [("2026-08-03", 200.0), ("2026-08-10", 211.0), ("2026-08-14", 199.4)]),
    ("COST:US", "steady", "Pullback", [("2026-07-14", 900.0), ("2026-08-05", 991.0), ("2026-08-12", 1000.0),
                                       ("2026-08-20", 960.0)]),
    ("SMCI:US", "volatile", "Earnings gap", [("2026-08-18", 40.0), ("2026-08-21", 37.9)]),
    ("MP:US", "volatile", "Breakout", [("2026-09-09", 64.0), ("2026-09-11", 60.4)]),
    ("V:US", "steady", "Base", [("2026-09-01", 340.0), ("2026-09-10", 375.0), ("2026-09-12", 380.0),
                                ("2026-09-16", 362.0)]),
    ("MP:US", "volatile", "Pullback", [("2026-09-14", 62.5), ("2026-09-24", 58.9)]),
    ("CRWD:US", "core", "Breakout", [("2026-09-08", 400.0), ("2026-09-18", 461.0), ("2026-09-21", 480.0),
                                     ("2026-09-23", 433.0)]),
    ("COIN:US", "volatile", "Trend", [("2026-09-18", 350.0), ("2026-09-22", 331.0)]),
    ("AMD:US", "volatile", "Pullback", [("2026-09-21", 168.0), ("2026-09-25", 159.2)]),
]

# Open positions: key, confirmed type (None = left as suggested), setup, days ago, entry, high since, last
_BOOK = [
    ("JNJ:US", "steady", "Pullback", 33, 158.40, 164.00, 160.10),
    ("WMT:US", "steady", "Breakout", 18, 96.30, 106.90, 104.20),
    ("HSBA:LSE", "steady", "Pullback", 9, 1012.0, 1048.0, 1041.0),
    ("D05:SGX", "steady", "Base", 7, 49.20, 50.10, 49.86),
    ("MSFT:US", "core", "Pullback", 14, 505.00, 528.00, 521.40),
    ("GOOGL:US", "core", "Breakout", 21, 238.00, 255.00, 252.30),
    ("AMZN:US", "core", "Pullback", 4, 226.50, 229.00, 221.80),
    ("7203:TSEJ", None, "Base", 10, 2905.0, 3010.0, 2987.0),
    ("9988:SEHK", "core", "Breakout", 6, 152.30, 158.00, 155.60),
    ("NVDA:US", "volatile", "Breakout", 11, 178.00, 193.00, 190.40),
    ("ZS:US", "volatile", "Pullback", 6, 288.00, 295.00, 279.50),
    ("PLTR:US", "volatile", "Trend", 9, 172.00, 191.00, 186.90),
    ("AMD:US", "volatile", "Pullback", 2, 162.00, 164.00, 158.40),
]

_WATCH_ONLY = {"HOOD:US": 118.40, "META:US": 742.00, "HD:US": 402.50, "700:SEHK": 612.0,
               "SH:US": 38.20, "SQQQ:US": 15.10, "MP:US": 61.20, "COIN:US": 336.00}

_EARNINGS = {"NVDA:US": "2026-10-02", "ZS:US": "2026-10-06", "HOOD:US": "2026-10-05", "MSFT:US": "2026-10-28",
             "META:US": "2026-10-28", "AMD:US": "2026-10-27", "9988:SEHK": "2026-11-13", "JNJ:US": "2026-10-14"}

# Weather: proxy -> (start level, [(sessions, daily drift), ...], daily noise, seed), ending on DEMO_TODAY.
_PATHS = {
    "SPY": (520.0, [(380, 0.0006), (60, -0.0006), (90, 0.0012)], 0.004, 15),
    "RSP": (165.0, [(380, 0.0005), (60, -0.0005), (90, 0.0011)], 0.004, 16),
    "ISF:LN": (780.0, [(430, 0.0004), (90, -0.0003)], 0.004, 12),
    "2800:HK": (18.0, [(300, 0.0002), (220, 0.0011)], 0.006, 14),
    "ES3:SG": (3.20, [(430, 0.0004), (90, 0.0001)], 0.004, 13),
    "1306:JP": (2600.0, [(470, 0.0006), (80, -0.0009)], 0.004, 11),
}


def _at(day: str, hour: int = 15) -> datetime:
    return datetime.fromisoformat(day).replace(hour=hour, tzinfo=timezone.utc)


def _bars(close: np.ndarray, end: datetime) -> pd.DataFrame:
    idx = pd.bdate_range(end=end.date(), periods=len(close))
    c = pd.Series(close, index=idx)
    o = c.shift(1).fillna(c.iloc[0])
    return pd.DataFrame({"open": o, "high": np.maximum(o, c) * 1.004, "low": np.minimum(o, c) * 0.996,
                         "close": c, "volume": 1_000_000.0}, index=pd.DatetimeIndex(idx, name="date"))


def _weather_bars(broker: SimBroker) -> None:
    for sym, (start, legs, noise, seed) in _PATHS.items():
        rng = np.random.default_rng(seed)
        rets = np.concatenate([np.full(n, drift) for n, drift in legs]) + rng.normal(0, noise, sum(n for n, _ in legs))
        broker.set_bars(sym, _bars(start * np.exp(np.cumsum(rets)), DEMO_TODAY))
    n = sum(k for k, _ in _PATHS["SPY"][1])
    rng = np.random.default_rng(17)
    vix = np.clip(14.5 + np.cumsum(rng.normal(0, 0.3, n)) * 0.25, 11, 40)
    vix[-150:-90] += np.linspace(0, 5, 60)       # the dip in the spring
    broker.set_bars("VIX", _bars(vix, DEMO_TODAY))
    broker.set_bars("VIX3M", _bars(vix + 1.8, DEMO_TODAY))


# Share of each breadth basket in an uptrend over the last few months.
_BREADTH_UP = {"US": 0.72, "LSE": 0.50, "SEHK": 0.68, "SGX": 0.70, "TSEJ": 0.25}
_PROXY = {"US": "SPY", "LSE": "ISF:LN", "SEHK": "2800:HK", "SGX": "ES3:SG", "TSEJ": "1306:JP"}

# Chart shape per stock for the anchored-VWAP levels: sessions since the earnings gap, its size,
# sessions since the breakout, and how the last month went.
_SHAPES = {
    "HOOD:US": (38, 0.09, 14, "pullback"), "META:US": (46, 0.06, 18, "pullback"),
    "HD:US": (35, 0.05, 20, "pullback"), "COIN:US": (30, -0.07, None, "broken"),
    "ZS:US": (25, 0.05, 12, "broken"), "SQQQ:US": (None, 0.0, None, "trend"),
}


def _breadth_bars(broker: SimBroker) -> None:
    for market, syms in DEFAULT_BASKETS.items():
        base = broker.bars(_PROXY[market])["close"].to_numpy()
        base = base / base[0]
        rng = np.random.default_rng(100 + len(market))
        n_up = round(_BREADTH_UP[market] * len(syms))
        for i, sym in enumerate(syms):
            drift = np.zeros(len(base))
            drift[-70:] = 0.0025 if i < n_up else -0.0025
            idio = np.cumsum(drift + rng.normal(0, 0.006, len(base)))
            text = sym if market == "US" else f"{sym}:{market}"
            broker.set_bars(text, _bars(100.0 * base * np.exp(idio), DEMO_TODAY))


def _stock_bars(broker: SimBroker, key: str, last: float, atr: float) -> None:
    """A daily chart ending at ``last``, with an earnings gap and a breakout where the shape says."""
    seed = sum(ord(c) * (i + 1) for i, c in enumerate(key))
    gap_at, gap, brk_at, kind = _SHAPES.get(key, (35 + seed % 20, 0.05, 10 + seed % 12, "trend"))
    n = 260
    rng = np.random.default_rng(seed)
    rets = rng.normal(0.0008, atr * 0.4, n)
    vol = np.full(n, 1_000_000.0) * rng.uniform(0.7, 1.3, n)
    if brk_at:
        rets[-brk_at - 25:-brk_at] = rng.normal(0, atr * 0.2, 25)      # a quiet base...
        rets[-brk_at] = 2.0 * atr                                       # ...then the breakout
        vol[-brk_at] *= 2.5
        post = slice(n - brk_at + 1, n)
        if kind == "trend":
            rets[post] += 0.3 * atr
        elif kind == "pullback":
            rets[n - brk_at + 1:n - 6] += 0.45 * atr                    # runs up...
            rets[-6:] = rng.normal(-0.15 * atr, 0.05 * atr, 6)          # ...and eases back toward support
        elif kind == "broken":
            rets[n - brk_at + 1:n - 10] += 0.15 * atr
    if kind == "broken":
        rets[-10:] = rng.normal(-0.35 * atr, 0.1 * atr, 10)             # slipping under it
    close = np.exp(np.cumsum(rets))
    close = close / close[-1] * last
    frame = _bars(close, DEMO_TODAY)
    if gap_at:
        i = n - gap_at
        frame.iloc[i, frame.columns.get_loc("open")] = frame["close"].iloc[i - 1] * (1 + gap)
        frame.iloc[i, frame.columns.get_loc("high")] = max(frame["open"].iloc[i], frame["close"].iloc[i]) * 1.01
        frame.iloc[i, frame.columns.get_loc("low")] = min(frame["open"].iloc[i], frame["close"].iloc[i]) * 0.99
        vol[i] *= 3.5
    frame["volume"] = vol
    sym, mkt = key.split(":")
    broker.set_bars(sym if mkt == "US" else key, frame)


def build_demo(rules: TradingRules | None = None, journal_path: str = ":memory:") -> tuple[TradingService, dict]:
    rules = rules or TradingRules()
    broker = SimBroker(cash_usd=130_000.0, clock=_at("2026-07-01"))
    by_key: dict[str, Instrument] = {}
    for key, cid, ccy, lot, mag, name, atr, sector in _LISTINGS:
        sym, mkt = key.split(":")
        inverse, lev = detect_inverse(name)
        inst = Instrument(sym, mkt, cid, ccy, lot, float(mag), description=name, inverse=inverse,
                          leverage=lev, sector=sector)
        by_key[key] = broker.add(inst, 100.0, atr)
    _weather_bars(broker)
    _breadth_bars(broker)

    journal = Journal(journal_path)
    service = TradingService(
        broker, journal, rules, orders_enabled=True,
        seed_watchlist=[k for k, *_ in _LISTINGS if k not in {h[0] for h in _HISTORY} or k in _WATCH_ONLY],
        weather_proxies={"US": "SPY", "LSE": "ISF:LN", "SEHK": "2800:HK", "SGX": "ES3:SG", "TSEJ": "1306:JP"},
        earnings=EarningsCalendar(journal, api_key=""))

    def buy(key, bucket, setup, when, price):
        broker.clock = when
        broker._price[by_key[key].con_id] = price
        service.cycle()
        r = service.enter(key, bucket=bucket, limit=price, setup=setup)
        if not r.get("ok"):
            raise RuntimeError(f"demo buy of {key} refused: {r['message']}")
        service.cycle()

    def move(key, price, when):
        broker.clock = when
        broker.set_price(by_key[key].con_id, price)
        service.cycle()

    events = []
    for key, bucket, setup, path in _HISTORY:
        (d0, p0), rest = path[0], path[1:]
        events.append((_at(d0), "buy", key, bucket, setup, p0))
        events += [(_at(d), "move", key, None, None, p) for d, p in rest]
    for key, bucket, setup, days, entry, high, last in _BOOK:
        when = DEMO_TODAY - timedelta(days=days, hours=5)
        events.append((when, "buy", key, bucket, setup, entry))
        events.append((when + timedelta(days=max(1, days // 2)), "move", key, None, None, high))
        events.append((DEMO_TODAY - timedelta(minutes=30), "move", key, None, None, last))
    events.append((DEMO_TODAY - timedelta(days=2), "add", "NVDA:US", None, None, 190.40))
    for when, kind, key, bucket, setup, price in sorted(events, key=lambda e: (e[0], e[1] != "move")):
        if kind == "buy":
            buy(key, bucket, setup, when, price)
        elif kind == "move":
            move(key, price, when)
        else:
            broker.clock = when
            broker._price[by_key[key].con_id] = price
            service.cycle()
            r = service.add_to(key, limit=price)
            if not r.get("ok"):
                raise RuntimeError(f"demo add to {key} refused: {r['message']}")
            service.cycle()

    for key, price in _WATCH_ONLY.items():
        broker._price[by_key[key].con_id] = price
    for key, day in _EARNINGS.items():
        journal.set_earnings(key, datetime.fromisoformat(day).date(), source="manual")
    for key, cid, ccy, lot, mag, name, atr, sector in _LISTINGS:
        _stock_bars(broker, key, broker._price[cid], atr)
    for key in _WATCH_ONLY:
        journal.watch(key, *key.split(":"), source="config")

    broker.clock = DEMO_TODAY
    service._avwap.clear()                       # levels from the finished charts
    for _ in range(40):                          # let breadth and VWAP levels fill in
        service.cycle()
        if service.weather.breadth.pending() == 0 and len(service._avwap) >= len(journal.watchlist()):
            break
    state = service.cycle()
    checks = {}
    for key in ("HOOD:US", "COIN:US", "MP:US", "META:US", "HD:US", "700:SEHK", "SH:US", "SQQQ:US"):
        res = service.check(key)
        checks[key] = res
        if key.endswith(":US"):
            checks[key.split(":")[0]] = res
    state["demo_checks"] = checks
    return service, state
