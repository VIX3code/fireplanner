"""The trade journal: every position, every exit, every alert, in one SQLite file.

The journal is what gives the system a memory. IBKR knows what you hold now;
only the journal knows that the last two AMD trades were stop-outs, what the
original stop was, or how high a stock has been since you bought it. Keep the
file on persistent storage and out of git (it holds your trading history).
"""

from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass, fields
from datetime import date, datetime, timezone
from pathlib import Path

__all__ = ["Journal", "Trade", "Exit", "now_utc"]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    key TEXT NOT NULL,
    symbol TEXT NOT NULL,
    market TEXT NOT NULL,
    con_id INTEGER NOT NULL,
    currency TEXT NOT NULL,
    bucket TEXT NOT NULL,
    entry REAL NOT NULL,
    qty INTEGER NOT NULL,
    initial_qty INTEGER NOT NULL,
    atr REAL,
    stop REAL NOT NULL,
    initial_stop REAL NOT NULL,
    target REAL NOT NULL,
    target_qty INTEGER NOT NULL,
    stop_state TEXT NOT NULL DEFAULT 'initial',
    high_water REAL NOT NULL,
    opened_at TEXT NOT NULL,
    closed_at TEXT,
    status TEXT NOT NULL DEFAULT 'open',
    exited_qty INTEGER NOT NULL DEFAULT 0,
    exit_value REAL NOT NULL DEFAULT 0,
    realized_pct REAL,
    realized_usd REAL,
    strike INTEGER,
    oca_rev INTEGER NOT NULL DEFAULT 0,
    source TEXT NOT NULL DEFAULT 'broker',
    note TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS trades_open ON trades(status, con_id);
CREATE INDEX IF NOT EXISTS trades_key ON trades(key, closed_at);
CREATE TABLE IF NOT EXISTS exits (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_id INTEGER NOT NULL REFERENCES trades(id),
    qty INTEGER NOT NULL,
    price REAL NOT NULL,
    kind TEXT NOT NULL,
    at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    level TEXT NOT NULL,
    kind TEXT NOT NULL,
    key TEXT,
    message TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS watchlist (
    key TEXT PRIMARY KEY,
    symbol TEXT NOT NULL,
    market TEXT NOT NULL,
    source TEXT NOT NULL,
    lot_size INTEGER,
    note TEXT NOT NULL DEFAULT '',
    added_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS bucket_choice (
    key TEXT PRIMARY KEY,
    bucket TEXT NOT NULL,
    confirmed_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS unlocks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    key TEXT NOT NULL,
    at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS earnings (
    key TEXT PRIMARY KEY,
    date TEXT,
    source TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS breaker (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    kind TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS pending (
    key TEXT PRIMARY KEY,
    setup TEXT NOT NULL DEFAULT '',
    note TEXT NOT NULL DEFAULT '',
    at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sectors (
    key TEXT PRIMARY KEY,
    sector TEXT NOT NULL,
    source TEXT NOT NULL
);
"""

#: Columns added after the first release; added in place to an older journal.
_TRADE_MIGRATIONS = {
    "setup": "TEXT NOT NULL DEFAULT ''",
    "initial_risk_usd": "REAL",
    "adds": "INTEGER NOT NULL DEFAULT 0",
    "sector": "TEXT NOT NULL DEFAULT ''",
    "low_water": "REAL",
}


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _iso(ts: datetime | None) -> str | None:
    return ts.astimezone(timezone.utc).isoformat(timespec="seconds") if ts else None


def _parse(ts: str | None) -> datetime | None:
    return datetime.fromisoformat(ts) if ts else None


@dataclass
class Trade:
    id: int
    key: str
    symbol: str
    market: str
    con_id: int
    currency: str
    bucket: str
    entry: float
    qty: int
    initial_qty: int
    atr: float | None
    stop: float
    initial_stop: float
    target: float
    target_qty: int
    stop_state: str
    high_water: float
    opened_at: str
    closed_at: str | None
    status: str
    exited_qty: int
    exit_value: float
    realized_pct: float | None
    realized_usd: float | None
    strike: int | None
    oca_rev: int
    source: str
    note: str
    setup: str = ""
    initial_risk_usd: float | None = None
    adds: int = 0
    sector: str = ""
    low_water: float | None = None

    @property
    def opened(self) -> datetime:
        return _parse(self.opened_at)

    @property
    def closed_date(self) -> date | None:
        c = _parse(self.closed_at)
        return c.date() if c else None

    @property
    def has_exits(self) -> bool:
        return self.exited_qty > 0

    @property
    def oca_group(self) -> str:
        return f"fp-{self.id}-{self.oca_rev}"

    @property
    def r_multiple(self) -> float | None:
        """Result in units of the risk taken: -1 is a full stop-out."""
        if self.realized_usd is None or not self.initial_risk_usd:
            return None
        return self.realized_usd / self.initial_risk_usd

    @property
    def days_held(self) -> int:
        end = _parse(self.closed_at) or now_utc()
        return max(0, (end.date() - self.opened.date()).days)


@dataclass
class Exit:
    id: int
    trade_id: int
    qty: int
    price: float
    kind: str
    at: str


_TRADE_FIELDS = [f.name for f in fields(Trade)]


class Journal:
    """Thread-safe wrapper over one SQLite file (``":memory:"`` for tests)."""

    def __init__(self, path: str | Path = "data/trading/journal.sqlite"):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.path = str(path)
        self._db = sqlite3.connect(self.path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock:
            self._db.executescript(_SCHEMA)
            have = {r[1] for r in self._db.execute("PRAGMA table_info(trades)")}
            for col, decl in _TRADE_MIGRATIONS.items():
                if col not in have:
                    self._db.execute(f"ALTER TABLE trades ADD COLUMN {col} {decl}")
            self._db.commit()

    def close(self) -> None:
        self._db.close()

    def _exec(self, sql: str, args=()) -> sqlite3.Cursor:
        with self._lock:
            cur = self._db.execute(sql, args)
            self._db.commit()
            return cur

    def _rows(self, sql: str, args=()) -> list[sqlite3.Row]:
        with self._lock:
            return self._db.execute(sql, args).fetchall()

    # -- trades ----------------------------------------------------------
    def open_trade(self, *, key, symbol, market, con_id, currency, bucket, entry, qty, atr,
                   stop, target, target_qty, opened_at: datetime | None = None,
                   source: str = "broker", note: str = "", setup: str = "",
                   initial_risk_usd: float | None = None, sector: str = "") -> Trade:
        cur = self._exec(
            """INSERT INTO trades (key, symbol, market, con_id, currency, bucket, entry, qty, initial_qty,
                   atr, stop, initial_stop, target, target_qty, high_water, low_water, opened_at, source, note,
                   setup, initial_risk_usd, sector)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (key, symbol, market, int(con_id), currency, bucket, float(entry), int(qty), int(qty),
             atr, float(stop), float(stop), float(target), int(target_qty), float(entry), float(entry),
             _iso(opened_at or now_utc()), source, note, setup, initial_risk_usd, sector),
        )
        return self.trade(cur.lastrowid)

    def trade(self, trade_id: int) -> Trade:
        rows = self._rows("SELECT * FROM trades WHERE id = ?", (trade_id,))
        if not rows:
            raise KeyError(trade_id)
        return Trade(**{k: rows[0][k] for k in _TRADE_FIELDS})

    def update_trade(self, trade_id: int, **changes) -> Trade:
        bad = set(changes) - set(_TRADE_FIELDS) - {"id"}
        if bad or "id" in changes:
            raise ValueError(f"cannot update {sorted(bad | ({'id'} & set(changes)))}")
        if changes:
            cols = ", ".join(f"{k} = ?" for k in changes)
            self._exec(f"UPDATE trades SET {cols} WHERE id = ?", (*changes.values(), trade_id))
        return self.trade(trade_id)

    def open_trades(self) -> list[Trade]:
        rows = self._rows("SELECT * FROM trades WHERE status = 'open' ORDER BY opened_at, id")
        return [Trade(**{k: r[k] for k in _TRADE_FIELDS}) for r in rows]

    def open_trade_for(self, con_id: int) -> Trade | None:
        rows = self._rows("SELECT * FROM trades WHERE status = 'open' AND con_id = ? ORDER BY id DESC", (con_id,))
        return Trade(**{k: rows[0][k] for k in _TRADE_FIELDS}) if rows else None

    def closed_trades(self, key: str | None = None) -> list[Trade]:
        if key:
            rows = self._rows("SELECT * FROM trades WHERE status = 'closed' AND key = ? ORDER BY closed_at, id", (key,))
        else:
            rows = self._rows("SELECT * FROM trades WHERE status = 'closed' ORDER BY closed_at, id")
        return [Trade(**{k: r[k] for k in _TRADE_FIELDS}) for r in rows]

    def add_exit(self, trade_id: int, qty: int, price: float, kind: str, at: datetime | None = None) -> Trade:
        t = self.trade(trade_id)
        qty = int(min(qty, t.qty))
        if qty <= 0:
            return t
        self._exec("INSERT INTO exits (trade_id, qty, price, kind, at) VALUES (?,?,?,?,?)",
                   (trade_id, qty, float(price), kind, _iso(at or now_utc())))
        return self.update_trade(trade_id, qty=t.qty - qty, exited_qty=t.exited_qty + qty,
                                 exit_value=t.exit_value + qty * float(price))

    def exits(self, trade_id: int) -> list[Exit]:
        rows = self._rows("SELECT * FROM exits WHERE trade_id = ? ORDER BY at, id", (trade_id,))
        return [Exit(**dict(r)) for r in rows]

    def close_trade(self, trade_id: int, *, magnifier: float, usd_per_unit: float,
                    strike_loss_pct: float, at: datetime | None = None) -> Trade:
        """Mark a fully exited trade closed and decide whether it was a strike."""
        t = self.trade(trade_id)
        if t.exited_qty <= 0:
            realized_pct, realized_usd = 0.0, 0.0
        else:
            avg_exit = t.exit_value / t.exited_qty
            realized_pct = avg_exit / t.entry - 1
            realized_usd = t.exited_qty * (avg_exit - t.entry) / magnifier * usd_per_unit
        return self.update_trade(
            trade_id, status="closed", qty=0, closed_at=_iso(at or now_utc()),
            realized_pct=realized_pct, realized_usd=realized_usd,
            strike=int(realized_pct < -strike_loss_pct),
        )

    def closed_since(self, since: datetime) -> list[Trade]:
        rows = self._rows("SELECT * FROM trades WHERE status = 'closed' AND closed_at >= ? ORDER BY closed_at, id",
                          (_iso(since),))
        return [Trade(**{k: r[k] for k in _TRADE_FIELDS}) for r in rows]

    # -- unlocks, settings, breaker ------------------------------------------
    def unlock(self, key: str, at: datetime | None = None) -> None:
        self._exec("INSERT INTO unlocks (key, at) VALUES (?, ?)", (key, _iso(at or now_utc())))

    def last_unlock(self, key: str) -> datetime | None:
        rows = self._rows("SELECT at FROM unlocks WHERE key = ? ORDER BY id DESC LIMIT 1", (key,))
        return _parse(rows[0]["at"]) if rows else None

    def set_setting(self, key: str, value) -> None:
        import json
        self._exec(
            """INSERT INTO settings (key, value, updated_at) VALUES (?,?,?)
               ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at""",
            (key, json.dumps(value), _iso(now_utc())),
        )

    def settings(self) -> dict:
        import json
        return {r["key"]: json.loads(r["value"]) for r in self._rows("SELECT key, value FROM settings")}

    def breaker_event(self, kind: str, note: str = "", at: datetime | None = None) -> None:
        self._exec("INSERT INTO breaker (at, kind, note) VALUES (?,?,?)", (_iso(at or now_utc()), kind, note))

    def breaker_events_since(self, since: datetime) -> list[dict]:
        return [dict(r) for r in self._rows("SELECT * FROM breaker WHERE at >= ? ORDER BY id", (_iso(since),))]

    def last_breaker_event(self, kinds: tuple[str, ...] = ("pause", "resume", "kill")) -> dict | None:
        marks = ",".join("?" * len(kinds))
        rows = self._rows(f"SELECT * FROM breaker WHERE kind IN ({marks}) ORDER BY id DESC LIMIT 1", kinds)
        return dict(rows[0]) if rows else None

    def set_pending(self, key: str, setup: str = "", note: str = "") -> None:
        """Remember the setup and note typed with a buy until its trade opens."""
        self._exec(
            """INSERT INTO pending (key, setup, note, at) VALUES (?,?,?,?)
               ON CONFLICT(key) DO UPDATE SET setup = excluded.setup, note = excluded.note, at = excluded.at""",
            (key, setup, note, _iso(now_utc())),
        )

    def pop_pending(self, key: str) -> dict | None:
        rows = self._rows("SELECT * FROM pending WHERE key = ?", (key,))
        if not rows:
            return None
        self._exec("DELETE FROM pending WHERE key = ?", (key,))
        return dict(rows[0])

    # -- earnings and sectors --------------------------------------------------
    def set_earnings(self, key: str, day: date | None, source: str = "manual") -> None:
        self._exec(
            """INSERT INTO earnings (key, date, source, updated_at) VALUES (?,?,?,?)
               ON CONFLICT(key) DO UPDATE SET date = excluded.date, source = excluded.source,
                                              updated_at = excluded.updated_at""",
            (key, day.isoformat() if day else None, source, _iso(now_utc())),
        )

    def earnings(self) -> dict[str, dict]:
        out = {}
        for r in self._rows("SELECT * FROM earnings"):
            out[r["key"]] = {"date": date.fromisoformat(r["date"]) if r["date"] else None,
                             "source": r["source"], "updated_at": _parse(r["updated_at"])}
        return out

    def set_sector(self, key: str, sector: str, source: str = "manual") -> None:
        self._exec(
            """INSERT INTO sectors (key, sector, source) VALUES (?,?,?)
               ON CONFLICT(key) DO UPDATE SET sector = excluded.sector, source = excluded.source
               WHERE sectors.source != 'manual' OR excluded.source = 'manual'""",
            (key, sector, source),
        )

    def sectors(self) -> dict[str, str]:
        return {r["key"]: r["sector"] for r in self._rows("SELECT key, sector FROM sectors")}

    # -- events ----------------------------------------------------------
    def log(self, level: str, kind: str, message: str, key: str | None = None, at: datetime | None = None) -> None:
        self._exec("INSERT INTO events (at, level, kind, key, message) VALUES (?,?,?,?,?)",
                   (_iso(at or now_utc()), level, kind, key, message))

    def events(self, limit: int = 30) -> list[dict]:
        rows = self._rows("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,))
        return [dict(r) for r in rows]

    # -- watchlist -------------------------------------------------------
    def watch(self, key: str, symbol: str, market: str, source: str = "manual",
              lot_size: int | None = None, note: str = "") -> None:
        self._exec(
            """INSERT INTO watchlist (key, symbol, market, source, lot_size, note, added_at)
               VALUES (?,?,?,?,?,?,?)
               ON CONFLICT(key) DO UPDATE SET lot_size = COALESCE(excluded.lot_size, watchlist.lot_size),
                                              note = CASE WHEN excluded.note = '' THEN watchlist.note ELSE excluded.note END""",
            (key, symbol, market, source, lot_size, note, _iso(now_utc())),
        )

    def unwatch(self, key: str) -> bool:
        return self._exec("DELETE FROM watchlist WHERE key = ?", (key,)).rowcount > 0

    def watchlist(self) -> list[dict]:
        return [dict(r) for r in self._rows("SELECT * FROM watchlist ORDER BY market, symbol")]

    def lot_override(self, key: str) -> int | None:
        rows = self._rows("SELECT lot_size FROM watchlist WHERE key = ?", (key,))
        return rows[0]["lot_size"] if rows and rows[0]["lot_size"] else None

    # -- stock type ------------------------------------------------------
    def confirm_bucket(self, key: str, bucket: str) -> None:
        self._exec(
            """INSERT INTO bucket_choice (key, bucket, confirmed_at) VALUES (?,?,?)
               ON CONFLICT(key) DO UPDATE SET bucket = excluded.bucket, confirmed_at = excluded.confirmed_at""",
            (key, bucket, _iso(now_utc())),
        )
        # An open position follows the confirmed type.
        self._exec("UPDATE trades SET bucket = ? WHERE key = ? AND status = 'open'", (bucket, key))

    def confirmed_bucket(self, key: str) -> str | None:
        rows = self._rows("SELECT bucket FROM bucket_choice WHERE key = ?", (key,))
        return rows[0]["bucket"] if rows else None

    def confirmed_buckets(self) -> dict[str, str]:
        return {r["key"]: r["bucket"] for r in self._rows("SELECT key, bucket FROM bucket_choice")}
