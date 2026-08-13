"""Concrete IBKR transports plus the offline snapshot provider.

Connection cheat-sheet
----------------------
**TWS / IB Gateway (recommended).** Enable *Configure → API → Settings →
Enable ActiveX and Socket Clients*, add ``127.0.0.1`` to trusted IPs, and note
the port: 7496 live TWS, 7497 paper TWS, 4001 live Gateway, 4002 paper Gateway.
Then ``pip install "fireplanner[gateway]"``.

**Client Portal Web API.** Run the CP gateway, browse to
``https://localhost:5000`` and log in. The session dies after ~a few minutes of
inactivity, so `WebApiProvider` re-tickles ``/tickle`` on every call.

Market-data entitlements matter: without a US equities subscription IBKR returns
delayed or empty quotes. Historical daily bars are generally available either
way, which is what a non-intraday system actually needs.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pandas as pd

from .base import BarProvider, Contract, drop_incomplete_last_bar, normalize_bars

__all__ = ["SnapshotProvider", "GatewayProvider", "WebApiProvider", "get_provider"]


# --------------------------------------------------------------------------
# offline
# --------------------------------------------------------------------------

class SnapshotProvider:
    """Serves bars from committed CSVs. No network, fully deterministic.

    This is what makes the test suite and the demo dashboard reproducible: the
    CSVs in ``data/snapshots`` are real IBKR pulls frozen at a point in time.
    """

    def __init__(self, root: str | Path = "data/snapshots"):
        self.root = Path(root)
        self._account_cache: dict | None = None

    def resolve(self, symbol: str) -> Contract:
        path = self.root / f"{symbol.upper()}.csv"
        if not path.exists():
            raise LookupError(f"no snapshot for {symbol!r} at {path}")
        return Contract(symbol=symbol.upper(), con_id=0, description="snapshot")

    def history(self, symbol: str, lookback_days: int = 1260, bar_size: str = "1 day") -> pd.DataFrame:
        path = self.root / f"{symbol.upper()}.csv"
        if not path.exists():
            raise LookupError(f"no snapshot for {symbol!r} at {path}")
        df = normalize_bars(pd.read_csv(path, parse_dates=["date"]))
        return df.tail(lookback_days)

    def account(self) -> dict:
        """Return the frozen account summary + positions.

        Prefers ``account.json`` — your real account state, written by a live
        pull and deliberately gitignored — and falls back to the committed
        ``account.example.json`` so a fresh clone still renders a full dashboard
        without anyone's balances in version control.
        """
        if self._account_cache is None:
            for name in ("account.json", "account.example.json"):
                path = self.root / name
                if path.exists():
                    self._account_cache = json.loads(path.read_text())
                    break
            else:
                self._account_cache = {}
        return self._account_cache

    def symbols(self) -> list[str]:
        return sorted(p.stem for p in self.root.glob("*.csv"))


# --------------------------------------------------------------------------
# TWS / IB Gateway socket API
# --------------------------------------------------------------------------

class GatewayProvider:
    """Live bars from TWS or IB Gateway over the socket API (``ib_async``).

    Parameters mirror the TWS API defaults. ``client_id`` must be unique per
    concurrent connection — reusing one silently kicks the other session off.
    """

    #: Friendly names for the four standard ports.
    PORTS = {"tws-live": 7496, "tws-paper": 7497, "gateway-live": 4001, "gateway-paper": 4002}

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 4002,
        client_id: int = 17,
        readonly: bool = True,
        currency: str = "USD",
        exchange: str = "SMART",
    ):
        self.host, self.port, self.client_id = host, port, client_id
        self.readonly, self.currency, self.exchange = readonly, currency, exchange
        self._ib = None
        self._contracts: dict[str, Contract] = {}

    # -- connection ------------------------------------------------------
    def connect(self):
        """Open the socket connection, importing ``ib_async`` lazily."""
        if self._ib is not None and self._ib.isConnected():
            return self._ib
        try:
            from ib_async import IB
        except ImportError as exc:  # pragma: no cover - depends on optional extra
            raise ImportError(
                "GatewayProvider needs ib_async — install with: pip install 'fireplanner[gateway]'"
            ) from exc

        ib = IB()
        ib.connect(self.host, self.port, clientId=self.client_id, readonly=self.readonly)
        self._ib = ib
        return ib

    def disconnect(self) -> None:
        if self._ib is not None and self._ib.isConnected():
            self._ib.disconnect()

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, *exc):
        self.disconnect()
        return False

    # -- data ------------------------------------------------------------
    def resolve(self, symbol: str) -> Contract:
        if symbol in self._contracts:
            return self._contracts[symbol]

        from ib_async import Index, Stock

        ib = self.connect()
        # Index symbols (VIX and friends) are not SMART-routed.
        if symbol.upper() in {"VIX", "VIX3M", "VIX9D", "SPX", "NDX", "RUT"}:
            raw = Index(symbol.upper(), "CBOE", self.currency)
        else:
            raw = Stock(symbol.upper(), self.exchange, self.currency)

        details = ib.reqContractDetails(raw)
        if not details:
            raise LookupError(f"IBKR could not resolve {symbol!r}")

        d = details[0].contract
        contract = Contract(
            symbol=symbol.upper(),
            con_id=int(d.conId),
            sec_type=d.secType,
            exchange=d.exchange or self.exchange,
            currency=d.currency,
            description=details[0].longName or "",
        )
        self._contracts[symbol] = contract
        return contract

    def history(self, symbol: str, lookback_days: int = 1260, bar_size: str = "1 day") -> pd.DataFrame:
        from ib_async import Stock, Index, util

        ib = self.connect()
        contract = self.resolve(symbol)
        if contract.sec_type == "IND":
            raw = Index(contract.symbol, contract.exchange, contract.currency)
            what = "TRADES"
        else:
            raw = Stock(contract.symbol, contract.exchange, contract.currency)
            what = "TRADES"

        years = max(1, round(lookback_days / 252))
        duration = f"{years} Y" if lookback_days >= 252 else f"{lookback_days} D"

        bars = ib.reqHistoricalData(
            raw,
            endDateTime="",
            durationStr=duration,
            barSizeSetting=bar_size,
            whatToShow=what,
            useRTH=True,       # regular hours only — this is a daily-bar system
            formatDate=1,
        )
        if not bars:
            raise LookupError(f"IBKR returned no bars for {symbol!r}")
        # Never hand back a session that is still forming.
        return drop_incomplete_last_bar(normalize_bars(util.df(bars))).tail(lookback_days)

    # -- account ---------------------------------------------------------
    def positions(self) -> pd.DataFrame:
        """Current positions as a frame with symbol, quantity, and cost basis."""
        ib = self.connect()
        rows = []
        for p in ib.positions():
            rows.append(
                {
                    "symbol": p.contract.symbol,
                    "con_id": p.contract.conId,
                    "asset_class": p.contract.secType,
                    "position": float(p.position),
                    "average_price": float(p.avgCost),
                    "currency": p.contract.currency,
                }
            )
        return pd.DataFrame(rows)

    def account_summary(self) -> dict:
        """Net liquidation, buying power, and margin, keyed by tag."""
        ib = self.connect()
        return {v.tag: v.value for v in ib.accountSummary()}


# --------------------------------------------------------------------------
# Client Portal Web API
# --------------------------------------------------------------------------

class WebApiProvider:
    """Bars from the Client Portal Web API.

    The CP gateway serves a self-signed certificate on localhost, so
    ``verify=False`` is the documented local configuration. Point ``base_url`` at
    a properly-certified host and set ``verify=True`` if you front it with TLS.
    """

    PERIOD_BY_DAYS = [(365 * 5, "5y"), (365 * 2, "2y"), (365, "1y"), (180, "6m"), (90, "3m"), (30, "1m")]

    def __init__(self, base_url: str = "https://localhost:5000/v1/api", verify: bool = False, timeout: int = 20):
        self.base_url = base_url.rstrip("/")
        self.verify, self.timeout = verify, timeout
        self._contracts: dict[str, Contract] = {}
        self._session = None

    def _sess(self):
        if self._session is None:
            try:
                import requests
            except ImportError as exc:  # pragma: no cover - optional extra
                raise ImportError(
                    "WebApiProvider needs requests — install with: pip install 'fireplanner[web]'"
                ) from exc
            if not self.verify:
                import urllib3

                urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
            self._session = requests.Session()
        return self._session

    def _get(self, path: str, **params):
        r = self._sess().get(f"{self.base_url}{path}", params=params, verify=self.verify, timeout=self.timeout)
        r.raise_for_status()
        return r.json()

    def tickle(self) -> dict:
        """Keep the brokerage session alive; CP logs you out when idle."""
        r = self._sess().post(f"{self.base_url}/tickle", verify=self.verify, timeout=self.timeout)
        r.raise_for_status()
        return r.json()

    def resolve(self, symbol: str) -> Contract:
        if symbol in self._contracts:
            return self._contracts[symbol]

        results = self._get("/iserver/secdef/search", symbol=symbol.upper(), name=False)
        exact = [r for r in results if str(r.get("symbol", "")).upper() == symbol.upper()]
        if not exact:
            raise LookupError(f"CP API could not resolve {symbol!r}")

        top = exact[0]
        sections = {s.get("secType") for s in top.get("sections", [])}
        contract = Contract(
            symbol=symbol.upper(),
            con_id=int(top["conid"]),
            sec_type="IND" if "IND" in sections and "STK" not in sections else "STK",
            description=top.get("description", "") or top.get("companyName", ""),
        )
        self._contracts[symbol] = contract
        return contract

    def history(self, symbol: str, lookback_days: int = 1260, bar_size: str = "1 day") -> pd.DataFrame:
        self.tickle()
        contract = self.resolve(symbol)
        period = next((p for days, p in self.PERIOD_BY_DAYS if lookback_days >= days), "1m")

        payload = self._get(
            "/iserver/marketdata/history",
            conid=contract.con_id,
            period=period,
            bar="1d",
            outsideRth=False,
        )
        rows = payload.get("data", [])
        if not rows:
            raise LookupError(f"CP API returned no bars for {symbol!r}")

        df = pd.DataFrame(rows).rename(
            columns={"t": "date", "o": "open", "h": "high", "l": "low", "c": "close", "v": "volume"}
        )
        # CP returns epoch milliseconds.
        df["date"] = pd.to_datetime(df["date"], unit="ms")
        return drop_incomplete_last_bar(normalize_bars(df)).tail(lookback_days)

    def positions(self, account_id: str | None = None) -> pd.DataFrame:
        self.tickle()
        if account_id is None:
            accounts = self._get("/portfolio/accounts")
            account_id = accounts[0]["accountId"]
        rows = self._get(f"/portfolio/{account_id}/positions/0")
        return pd.DataFrame(rows)


# --------------------------------------------------------------------------
# factory
# --------------------------------------------------------------------------

def get_provider(kind: str | None = None, **kwargs) -> BarProvider:
    """Build a provider by name.

    Falls back to the offline snapshot provider so that a clean checkout runs
    end-to-end with no broker connection at all.
    """
    kind = (kind or os.environ.get("FIREPLANNER_PROVIDER") or "snapshot").lower()
    if kind == "snapshot":
        return SnapshotProvider(**kwargs)
    if kind in {"gateway", "tws", "ib"}:
        return GatewayProvider(**kwargs)
    if kind in {"web", "cpapi", "portal"}:
        return WebApiProvider(**kwargs)
    raise ValueError(f"unknown provider {kind!r}; expected snapshot|gateway|web")
