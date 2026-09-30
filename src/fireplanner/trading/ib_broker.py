"""The IBKR adapter: TWS or IB Gateway through ``ib_async``.

Safety rails, all on by default:

* **Paper unless told otherwise.** Ports 4002 (Gateway), 7497 (TWS) and 4004
  (the Docker image's relay) are paper. A live port (4001, 7496, 4003) is
  refused unless the config says ``trading.allow_live: true``.
* **The account decides, not the port.** Ports are just settings in TWS, so on
  connecting the adapter reads the account id: paper accounts start with "D"
  (DU..., DF...). A live account behind a "paper" port is refused the same way.
* **The gateway itself must allow orders.** IB Gateway's *Read-Only API*
  setting blocks every order. `orders_allowed` checks it without placing one,
  and ``fireplanner trade doctor`` reports it.
* **One client id for the guardian.** IBKR only lets the client that placed an
  order modify it. Keep ``client_id`` fixed so a restarted guardian can still
  move its own stops.

Market data is requested as type 4 ("delayed frozen"). IBKR serves live data
instead wherever you hold a subscription, and something rather than nothing
where you don't. The guardian's orders live at IBKR either way, so a stop
still triggers if this process is down; only the ratchet and the dashboard
need prices.
"""

from __future__ import annotations

import math
import time
from collections import deque
from datetime import date, datetime, timezone

from .broker import BrokerFill, BrokerOrder, BrokerPosition, CashView, OrderSpec, PacingDeferred
from .markets import MARKETS, Instrument, detect_inverse
from .rules import TradingRules

__all__ = ["IBBroker", "LiveAccountRefused", "PAPER_PORTS", "LIVE_PORTS", "market_for", "mask_account"]


class LiveAccountRefused(ValueError):
    """Connected to a live account without ``trading.allow_live``."""


def mask_account(acct: str) -> str:
    return f"{acct[:2]}•••{acct[-4:]}" if acct and len(acct) > 6 else acct or ""


def is_paper_account(acct: str) -> bool:
    return bool(acct) and acct.upper().startswith("D")

PAPER_PORTS = {4002, 7497, 4004}
LIVE_PORTS = {4001, 7496, 4003}
_ACTIVE = {"PendingSubmit", "ApiPending", "PreSubmitted", "Submitted"}
_US_PRIMARY = ("NASDAQ", "NYSE", "ARCA", "AMEX", "BATS", "NYSEARCA")
# Forex pairs IBKR quotes, and whether the quote is USD per unit (else units per USD).
_FX_PAIRS = {"GBP": ("GBPUSD", True), "HKD": ("USDHKD", False), "SGD": ("USDSGD", False),
             "JPY": ("USDJPY", False), "EUR": ("EURUSD", True)}


def _num(x) -> float | None:
    """IBKR's 'unset' is DBL_MAX; missing ticks are NaN. Both mean no value."""
    if x is None:
        return None
    try:
        x = float(x)
    except (TypeError, ValueError):
        return None
    if math.isnan(x) or math.isinf(x) or x >= 1e300 or x <= 0:
        return None
    return x


def market_for(currency: str, exchange: str = "", primary: str = "") -> str:
    """Which of the five markets a contract belongs to."""
    for ex in (exchange, primary):
        ex = (ex or "").upper()
        if ex in ("LSE", "LSEETF"):
            return "LSE"
        if ex == "SEHK":
            return "SEHK"
        if ex == "SGX":
            return "SGX"
        if ex in ("TSEJ", "TSE.JPN"):
            return "TSEJ"
    return {"USD": "US", "GBP": "LSE", "GBX": "LSE", "HKD": "SEHK", "SGD": "SGX", "JPY": "TSEJ"}.get(
        (currency or "").upper(), "US")


class IBBroker:
    def __init__(self, host: str = "127.0.0.1", port: int = 4002, client_id: int = 23,
                 allow_live: bool = False, account: str = "", rules: TradingRules | None = None,
                 lot_sizes: dict | None = None):
        if port in LIVE_PORTS and not allow_live:
            raise ValueError(
                f"Port {port} is a LIVE trading port. The guardian runs on paper (4002 / 7497) until "
                f"trading.allow_live is set to true in config.yaml.")
        self.host, self.port, self.client_id, self.account = host, port, client_id, account
        self.allow_live = allow_live
        self.mode = "live" if port in LIVE_PORTS else "paper"
        self.accounts: list[str] = []
        self.rules = rules or TradingRules()
        self.lot_sizes = {str(k).upper(): int(v) for k, v in (lot_sizes or {}).items()}
        self._ib = None
        self._contracts: dict[int, object] = {}
        self._inst: dict[int, Instrument] = {}
        self._by_key: dict[str, Instrument] = {}
        self._trades: dict[int, object] = {}
        self._fx: dict[str, tuple[float, float]] = {}
        self._bars_cache: dict[str, tuple[date, object]] = {}
        # IBKR allows about 60 historical requests per 10 minutes; keep a margin.
        self.hist_limit, self.hist_window = 50, 600.0
        self._hist: deque = deque()
        self.dirty = False

    # -- connection ------------------------------------------------------
    def connect(self):
        if self._ib is not None and self._ib.isConnected():
            return self._ib
        try:
            from ib_async import IB
        except ImportError as exc:  # pragma: no cover - optional extra
            raise ImportError("The trade manager needs ib_async: pip install 'fireplanner[gateway]'") from exc
        ib = IB()
        ib.connect(self.host, self.port, clientId=self.client_id, readonly=False, account=self.account)
        accounts = [a for a in (ib.managedAccounts() or []) if a]
        mine = [self.account] if self.account else accounts
        live = any(not is_paper_account(a) for a in mine)
        if live and not self.allow_live:
            ib.disconnect()
            raise LiveAccountRefused(
                f"Connected to a LIVE account ({', '.join(mask_account(a) for a in mine)}) on port {self.port}. "
                f"The guardian stays on paper until trading.allow_live is set to true in config.yaml.")
        self.accounts, self.mode = accounts, ("live" if live else "paper")
        ib.reqMarketDataType(4)
        ib.execDetailsEvent += self._on_exec
        ib.orderStatusEvent += self._on_status
        ib.reqExecutions()
        self._ib = ib
        return ib

    def disconnect(self) -> None:
        if self._ib is not None and self._ib.isConnected():
            self._ib.disconnect()

    @property
    def connected(self) -> bool:
        return self._ib is not None and self._ib.isConnected()

    def status(self) -> dict:
        """For the dashboard: is the socket up, and which account is this."""
        acct = self.account or (self.accounts[0] if self.accounts else "")
        return {"broker": "IBKR", "connected": self.connected, "host": self.host, "port": self.port,
                "client_id": self.client_id, "account": mask_account(acct),
                "account_type": self.mode, "accounts": len(self.accounts)}

    def orders_allowed(self) -> tuple[bool | None, str]:
        """Whether the gateway accepts orders, found with a what-if order that can't execute.

        IB Gateway's Read-Only API setting rejects every order request with error
        321; a what-if order is one, but it is never sent to an exchange.
        """
        from ib_async import LimitOrder, Stock

        ib = self.connect()
        errors: list[tuple[int, str]] = []

        def on_error(req_id, code, msg, *rest):
            errors.append((int(code), str(msg)))

        contract = Stock("SPY", "SMART", "USD")
        ib.qualifyContracts(contract)
        order = LimitOrder("BUY", 1, 1.00)          # far below the market; what-if never trades anyway
        if self.account:
            order.account = self.account
        ib.errorEvent += on_error
        try:
            state = ib.whatIfOrder(contract, order)
        except Exception as exc:
            errors.append((0, str(exc)))
            state = None
        finally:
            ib.errorEvent -= on_error
        if any(code == 321 or "read-only" in msg.lower() for code, msg in errors):
            return False, "The gateway's Read-Only API setting is on: every order would be rejected."
        if state is not None and getattr(state, "initMarginChange", ""):
            return True, "Orders are accepted (checked with a what-if order, nothing was sent)."
        return None, "Couldn't tell: " + ("; ".join(m for _, m in errors[:2]) or "no answer to the what-if order")

    def _on_exec(self, *_):
        self.dirty = True

    def _on_status(self, *_):
        self.dirty = True

    def pop_dirty(self) -> bool:
        d, self.dirty = self.dirty, False
        return d

    def now(self) -> datetime:
        return datetime.now(timezone.utc)

    def wait(self, seconds: float) -> None:
        self.connect().sleep(seconds)

    # -- instruments -----------------------------------------------------
    def _increments(self, details, exchange: str) -> tuple:
        ib = self.connect()
        rules = (details.marketRuleIds or "").split(",")
        exchanges = (details.validExchanges or "").split(",")
        rule = None
        for ex, r in zip(exchanges, rules):
            if ex == exchange and r:
                rule = r
                break
        if rule is None and rules and rules[0]:
            rule = rules[0]
        if not rule:
            return ()
        try:
            incs = ib.reqMarketRule(int(rule)) or []
            return tuple(sorted((float(p.lowEdge), float(p.increment)) for p in incs))
        except Exception:
            return ()

    def _build(self, details, symbol: str, market: str, lot_override: int | None) -> Instrument:
        c = details.contract
        m = MARKETS[market]
        lot = lot_override or self.lot_sizes.get(f"{symbol}:{market}")
        if not lot:
            step = max(_num(getattr(details, "sizeIncrement", None)) or 0,
                       _num(getattr(details, "minSize", None)) or 0)
            lot = int(step) if step and step > 1 else m.default_lot
        # As reported by IBKR. The market default only fills a missing field; an
        # entry is still cross-checked against IBKR's own valuation before it is
        # sent (see preview_cost_usd), so a wrong unit blocks the order.
        magnifier = _num(getattr(details, "priceMagnifier", None)) or m.default_magnifier
        inverse, lev = detect_inverse(details.longName or "")
        inst = Instrument(symbol=symbol, market=market, con_id=int(c.conId), currency=c.currency or m.currency,
                          lot_size=int(lot), price_magnifier=float(magnifier),
                          increments=self._increments(details, c.exchange or m.exchange),
                          description=details.longName or "", inverse=inverse, leverage=lev,
                          sector=(getattr(details, "category", "") or getattr(details, "industry", "") or ""))
        self._contracts[inst.con_id] = c
        self._inst[inst.con_id] = inst
        self._by_key[inst.key] = inst
        return inst

    def instrument(self, symbol: str, market: str, lot_size: int | None = None) -> Instrument:
        key = f"{symbol}:{market}"
        if key in self._by_key and not lot_size:
            return self._by_key[key]
        from ib_async import Stock

        ib = self.connect()
        m = MARKETS[market]
        details = ib.reqContractDetails(Stock(symbol, m.exchange, m.currency))
        if not details:
            raise LookupError(f"IBKR could not find {symbol} on {m.name}")
        if market == "US" and len(details) > 1:
            details.sort(key=lambda d: (d.contract.primaryExchange not in _US_PRIMARY, d.contract.primaryExchange))
        d = details[0]
        c = d.contract
        if market == "US":
            c.exchange = "SMART"
        return self._build(d, symbol, market, lot_size)

    def instrument_for(self, position: BrokerPosition) -> Instrument:
        if position.con_id in self._inst:
            return self._inst[position.con_id]
        from ib_async import Contract

        ib = self.connect()
        details = ib.reqContractDetails(Contract(conId=position.con_id))
        if not details:
            raise LookupError(f"IBKR could not resolve contract {position.con_id}")
        d = details[0]
        c = d.contract
        market = market_for(c.currency, c.exchange, c.primaryExchange)
        if market == "US":
            c.exchange = "SMART"
        return self._build(d, c.symbol, market, None)

    # -- account ---------------------------------------------------------
    def positions(self) -> list[BrokerPosition]:
        ib = self.connect()
        out = []
        for p in ib.positions(self.account or ""):
            c = p.contract
            if c.secType not in ("STK", "ETF"):
                continue
            market = market_for(c.currency, c.exchange, c.primaryExchange)
            inst = self._inst.get(int(c.conId))
            magnifier = inst.price_magnifier if inst else MARKETS[market].default_magnifier
            # avgCost is per share in currency units; orders are priced in quote units.
            out.append(BrokerPosition(con_id=int(c.conId), symbol=c.symbol, market=market, currency=c.currency,
                                      qty=float(p.position), avg_cost=float(p.avgCost) * magnifier))
        return out

    def open_orders(self) -> list[BrokerOrder]:
        ib = self.connect()
        ib.reqAllOpenOrders()
        out = []
        self._trades = {}
        for t in ib.openTrades():
            o, s, c = t.order, t.orderStatus, t.contract
            if s.status not in _ACTIVE:
                continue
            oid = int(o.orderId or o.permId)
            self._trades[oid] = t
            out.append(BrokerOrder(
                order_id=oid, con_id=int(c.conId), action=o.action, order_type=o.orderType,
                qty=float(o.totalQuantity), stop_price=_num(o.auxPrice), limit_price=_num(o.lmtPrice),
                tif=o.tif or "", oca_group=o.ocaGroup or "", order_ref=o.orderRef or "",
                status=s.status, filled=float(s.filled or 0),
            ))
        return out

    def fills(self) -> list[BrokerFill]:
        ib = self.connect()
        out = []
        for f in ib.fills():
            e = f.execution
            side = "BUY" if e.side in ("BOT", "BUY") else "SELL"
            t = e.time if isinstance(e.time, datetime) else f.time
            if t.tzinfo is None:
                t = t.replace(tzinfo=timezone.utc)
            out.append(BrokerFill(exec_id=e.execId, con_id=int(f.contract.conId), side=side,
                                  qty=float(e.shares), price=float(e.price), time=t, order_ref=e.orderRef or ""))
        return out

    def quotes(self, instruments: list[Instrument]) -> dict[int, float]:
        ib = self.connect()
        contracts = [self._contracts[i.con_id] for i in instruments if i.con_id in self._contracts]
        if not contracts:
            return {}
        out = {}
        for tk in ib.reqTickers(*contracts):
            px = _num(tk.marketPrice()) or _num(tk.last) or _num(tk.close)
            if px is not None:
                out[int(tk.contract.conId)] = px
        return out

    def daily_atr(self, inst: Instrument) -> float | None:
        """14-day average daily range from the same daily bars the AVWAP uses (one request a day)."""
        from ..indicators.core import natr

        bars = self.bars(inst.symbol if inst.market == "US" else inst.key)   # may raise PacingDeferred
        series = natr(bars, 14).dropna()
        return float(series.iloc[-1]) / 100.0 if len(series) else None

    def _take_hist_slot(self) -> None:
        now = time.monotonic()
        while self._hist and now - self._hist[0] > self.hist_window:
            self._hist.popleft()
        if len(self._hist) >= self.hist_limit:
            raise PacingDeferred("IBKR pacing: historical data deferred to a later cycle")
        self._hist.append(now)

    _INDICES = {"VIX": "CBOE", "VIX3M": "CBOE", "VIX9D": "CBOE", "SPX": "CBOE"}

    def bars(self, symbol_text: str, days: int = 520):
        """Daily bars, cached for the day. Raises `PacingDeferred` rather than break IBKR's pacing."""
        from ib_async import Index, util

        from ..data.base import drop_incomplete_last_bar, normalize_bars
        from .markets import parse_symbol

        key = symbol_text.upper()
        hit = self._bars_cache.get(key)
        if hit and hit[0] == date.today():
            return hit[1]
        ib = self.connect()
        if key in self._INDICES:
            contract = Index(key, self._INDICES[key], "USD")
            ib.qualifyContracts(contract)
        else:
            sym, mkt = parse_symbol(key)
            contract = self._contracts[self.instrument(sym, mkt).con_id]
        self._take_hist_slot()
        years = max(1, round(days / 252))
        raw = ib.reqHistoricalData(contract, endDateTime="", durationStr=f"{years} Y", barSizeSetting="1 day",
                                   whatToShow="TRADES", useRTH=True, formatDate=1)
        if not raw:
            raise LookupError(f"IBKR returned no bars for {symbol_text}")
        frame = drop_incomplete_last_bar(normalize_bars(util.df(raw))).tail(days)
        self._bars_cache[key] = (date.today(), frame)
        return frame

    def usd_per_unit(self, currency: str) -> tuple[float, bool]:
        currency = currency.upper()
        if currency == "USD":
            return 1.0, True
        hit = self._fx.get(currency)
        if hit and time.time() - hit[1] < 600:
            return hit[0], True
        pair = _FX_PAIRS.get(currency)
        if pair:
            from ib_async import Forex

            try:
                ib = self.connect()
                [tk] = ib.reqTickers(Forex(pair[0]))
                px = _num(tk.midpoint()) or _num(tk.marketPrice()) or _num(tk.close)
                if px:
                    rate = px if pair[1] else 1.0 / px
                    self._fx[currency] = (rate, time.time())
                    return rate, True
            except Exception:
                pass
        return float(self.rules.fx_fallback.get(currency, 1.0)), False

    def cash(self) -> CashView:
        ib = self.connect()
        available = None
        for v in ib.accountSummary(self.account or ""):
            if v.tag == "AvailableFunds" and v.currency and v.currency != "BASE":
                amount = _num(v.value)
                if amount is not None:
                    rate, _ = self.usd_per_unit(v.currency)
                    available = amount * rate
                    break
        by_ccy = {}
        for v in ib.accountValues(self.account or ""):
            if v.tag == "CashBalance" and v.currency and v.currency != "BASE":
                try:
                    by_ccy[v.currency] = float(v.value)
                except ValueError:
                    pass
        return CashView(available_usd=available, by_currency=by_ccy)

    # -- orders ------------------------------------------------------------
    def place(self, spec: OrderSpec) -> int:
        from ib_async import Order

        ib = self.connect()
        contract = self._contracts.get(spec.con_id)
        if contract is None:
            raise LookupError(f"contract {spec.con_id} was never resolved")
        o = Order(action=spec.action, orderType=spec.order_type, totalQuantity=spec.qty, tif=spec.tif,
                  ocaGroup=spec.oca_group, ocaType=spec.oca_type if spec.oca_group else 0,
                  orderRef=spec.order_ref, outsideRth=spec.outside_rth, transmit=True)
        if self.account:
            o.account = self.account
        if spec.stop_price is not None:
            o.auxPrice = spec.stop_price
        if spec.limit_price is not None:
            o.lmtPrice = spec.limit_price
        trade = ib.placeOrder(contract, o)
        ib.sleep(0.5)
        status = trade.orderStatus.status
        if status in ("Cancelled", "Inactive", "ApiCancelled"):
            msgs = "; ".join(e.message for e in trade.log if e.message)
            raise RuntimeError(f"IBKR rejected the {spec.order_ref or spec.order_type} order: {msgs or status}")
        self._trades[int(trade.order.orderId)] = trade
        return int(trade.order.orderId)

    def modify(self, order: BrokerOrder, *, qty=None, stop_price=None, limit_price=None) -> None:
        ib = self.connect()
        trade = self._trades.get(order.order_id)
        if trade is None:
            raise LookupError(f"order {order.order_id} is not one this client can modify")
        o = trade.order
        if qty is not None:
            o.totalQuantity = order.filled + qty
        if stop_price is not None:
            o.auxPrice = stop_price
        if limit_price is not None:
            o.lmtPrice = limit_price
        ib.placeOrder(trade.contract, o)

    def preview_cost_usd(self, spec: OrderSpec) -> float | None:
        """IBKR's own valuation of a buy, in USD, from a what-if order.

        In a cash account a purchase needs 100% initial margin, so the change in
        initial margin is the order's value. It is independent of this module's
        price-unit handling, which is the point: the service refuses an entry
        whose planned cost disagrees with it.
        """
        from ib_async import Order

        ib = self.connect()
        contract = self._contracts.get(spec.con_id)
        if contract is None:
            return None
        o = Order(action=spec.action, orderType=spec.order_type, totalQuantity=spec.qty, tif=spec.tif)
        if spec.limit_price is not None:
            o.lmtPrice = spec.limit_price
        if self.account:
            o.account = self.account
        try:
            state = ib.whatIfOrder(contract, o)
        except Exception:
            return None
        value = _num(getattr(state, "initMarginChange", None))
        if value is None:
            return None
        base = None
        for v in ib.accountSummary(self.account or ""):
            if v.tag == "AvailableFunds" and v.currency and v.currency != "BASE":
                base = v.currency
                break
        rate, _ = self.usd_per_unit(base or "USD")
        return value * rate

    def cancel(self, order: BrokerOrder) -> None:
        ib = self.connect()
        trade = self._trades.get(order.order_id)
        if trade is None:
            raise LookupError(f"order {order.order_id} is not one this client can cancel")
        ib.cancelOrder(trade.order)
