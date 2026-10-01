"""``fireplanner trade doctor``: is this IBKR connection ready for the Swing Desk?

Runs every check a first live session depends on, and says what to fix:

* the connection, and whether the account is paper or live;
* whether the gateway accepts orders (its Read-Only API setting), found with a
  what-if order that can't trade;
* positions, working orders and cash can be read;
* a price comes back in each of the five markets (market-data permissions);
* daily history is available (for daily ranges, VWAP levels and breadth);
* live exchange rates, and the market-weather index for each market.

Nothing here places an order.
"""

from __future__ import annotations

from dataclasses import dataclass

from .markets import MARKETS
from .rules import TradingRules

__all__ = ["Finding", "run_doctor", "PROBES"]

#: One liquid listing per market, to test that prices come back.
PROBES = {"US": "SPY", "LSE": "HSBA", "SEHK": "700", "SGX": "D05", "TSEJ": "7203"}


@dataclass
class Finding:
    level: str      # good | warn | crit | info
    title: str
    detail: str = ""


def run_doctor(broker, rules: TradingRules, settings: dict) -> list[Finding]:
    out: list[Finding] = []
    add = out.append

    # 1. connection and account
    try:
        st = broker.status()
        if hasattr(broker, "connect"):
            broker.connect()
            st = broker.status()
    except Exception as exc:
        add(Finding("crit", "Can't connect to IBKR", f"{exc}. Is TWS / IB Gateway running and logged in, with "
                    f"'Enable ActiveX and Socket Clients' on, on the host and port given?"))
        return out
    kind = st.get("account_type")
    who = f"{st.get('account', '')} via {st.get('host', '')}:{st.get('port', '')}".strip()
    if kind == "live":
        add(Finding("warn", "Connected to a LIVE account", f"{who}. trading.allow_live is on: orders use real money."))
    elif kind == "sim":
        add(Finding("info", "Simulated broker", "No IBKR connection is involved."))
    else:
        add(Finding("good", "Connected to a paper account", who))

    # 2. can it place orders?
    try:
        ok, why = broker.orders_allowed()
    except Exception as exc:
        ok, why = None, str(exc)
    enabled = bool(settings.get("enabled"))
    if ok is True:
        add(Finding("good", "The gateway accepts orders", why))
    elif ok is False:
        add(Finding("crit" if enabled else "info", "Read-Only API is on",
                    why + (" trading.enabled is true, so turn it off in IB Gateway: Configure > Settings > API > "
                           "Settings > Read-Only API." if enabled else
                           " Fine while trading.enabled is false (dry run); turn it off before enabling orders.")))
    else:
        add(Finding("warn", "Couldn't confirm the gateway accepts orders", why))
    add(Finding("info", "Orders are " + ("ON" if enabled else "off (dry run)"),
                "trading.enabled in config.yaml. The guardian logs, but doesn't send, orders while it is false."))

    # 3. account state
    try:
        pos = [p for p in broker.positions() if p.qty]
        orders = broker.open_orders()
        add(Finding("good", "Positions and orders readable",
                    f"{len(pos)} position(s), {len(orders)} working order(s)"))
        if pos and not settings.get("manage_existing"):
            add(Finding("info", f"{len(pos)} position(s) already in the account",
                        "On the guardian's first run these are left alone (no stop, no target). Choose Manage "
                        "on the dashboard for any it should protect. Anything bought afterwards is managed."))
    except Exception as exc:
        add(Finding("crit", "Can't read positions or orders", str(exc)))
    try:
        cash = broker.cash()
        if cash.available_usd is None:
            add(Finding("warn", "Available funds not reported", "The cash check before a buy will be skipped."))
        else:
            add(Finding("good", "Cash readable", f"${cash.available_usd:,.0f} available"))
    except Exception as exc:
        add(Finding("warn", "Can't read cash", str(exc)))

    # 4. prices per market
    for market, sym in PROBES.items():
        name = MARKETS[market].name
        try:
            inst = broker.instrument(sym, market)
            px = broker.quotes([inst]).get(inst.con_id)
            lot = f", lot {inst.lot_size}" if inst.lot_size > 1 else (", lot unknown" if inst.lot_size <= 0 else "")
            if px:
                add(Finding("good", f"{name}: prices come through", f"{sym} {inst.fmt(px)} {inst.currency}{lot}"))
            else:
                add(Finding("warn", f"{name}: no price for {sym}",
                            "Market data may need a subscription for this exchange. Stops still work; "
                            "sizing and the ratchet need prices."))
        except Exception as exc:
            add(Finding("warn", f"{name}: {sym} not found", str(exc)))

    # 5. history
    try:
        bars = broker.bars("SPY")
        add(Finding("good", "Daily history available", f"{len(bars)} SPY sessions"))
    except Exception as exc:
        add(Finding("crit", "No daily history", f"{exc}. Daily ranges, VWAP levels and breadth depend on it."))

    # 6. exchange rates
    for ccy in ("GBP", "HKD", "SGD", "JPY"):
        try:
            rate, live = broker.usd_per_unit(ccy)
            if not live:
                add(Finding("warn", f"{ccy} rate from the fallback table",
                            f"{rate:g} USD per {ccy}; the live rate wasn't available."))
        except Exception as exc:
            add(Finding("warn", f"No {ccy} rate", str(exc)))

    # 7. weather indexes
    missing = []
    for market, proxy in (settings.get("weather") or {}).items():
        try:
            broker.bars(proxy)
        except Exception:
            missing.append(f"{proxy} ({MARKETS[market].name if market in MARKETS else market})")
    if missing:
        add(Finding("warn", "Market-weather index not found", ", ".join(missing) + ": change it under trading.weather."))
    elif settings.get("weather"):
        add(Finding("good", "Market-weather indexes found", ", ".join(settings["weather"].values())))
    return out
