"""The live dashboard: a small web server next to the trading loop.

Standard library only. It serves one page and a JSON API:

==========================  ==============================================
``GET  /``                  the dashboard
``GET  /api/state``         the latest snapshot (positions, buckets, ...)
``GET  /api/journal.csv``   every closed trade, for a spreadsheet
``POST /api/check``         run the pre-trade check for a symbol
``POST /api/enter``         send a buy (re-checked on the loop first)
``POST /api/add``           add to a winner
``POST /api/exit``          sell a whole position now
``POST /api/unlock``        unlock a stock after two strikes
``POST /api/pause``         pause new buys; ``/api/resume``; ``/api/kill``
``POST /api/watch``         add a ticker to the watchlist; ``/api/unwatch``
``POST /api/bucket``        confirm a stock's type
``POST /api/earnings``      set or clear an earnings date
``POST /api/sector``        group a stock under a sector or theme
``POST /api/settings``      change an adjustable rule (time stop weeks...)
``POST /api/note``          set a trade's setup tag and note
==========================  ==============================================

It binds to 127.0.0.1 by default. To reach it from your phone, put it behind
the Caddy proxy in ``deploy/`` (TLS + a password) and set a token as well:
``FIREPLANNER_DASH_TOKEN``. With a token set, every API call must send
``Authorization: Bearer <token>``; open the page once as ``/?token=...``.

POST bodies must be JSON. A browser cannot send JSON to another site without
a CORS preflight, which this server never answers, so a malicious page cannot
submit orders through your logged-in browser.
"""

from __future__ import annotations

import hmac
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources

__all__ = ["render_dashboard", "make_server", "serve"]


def _template() -> str:
    return resources.files(__package__).joinpath("dashboard_page.html").read_text(encoding="utf-8")


def render_dashboard(state: dict | None, live: bool = False) -> str:
    """The page, with a snapshot embedded. ``live`` makes it poll the API."""
    data = json.dumps(state or {}, default=str).replace("</", "<\\/")
    return (_template()
            .replace("/*__STATE__*/null", data)
            .replace("/*__LIVE__*/false", "true" if live else "false"))


def make_server(service, host: str = "127.0.0.1", port: int = 8765, token: str = "",
                timeout: float = 45.0) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        server_version = "FirePlanner"

        def log_message(self, fmt, *args):  # quiet
            return

        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, code: int, obj) -> None:
            self._send(code, json.dumps(obj, default=str).encode(), "application/json")

        def _authorised(self) -> bool:
            if not token:
                return True
            got = self.headers.get("Authorization", "")
            return got.startswith("Bearer ") and hmac.compare_digest(got[7:], token)

        def do_GET(self):
            path = self.path.split("?", 1)[0]
            if path in ("/", "/index.html"):
                # With a token set, the page carries no data: it fetches it with the token.
                page = render_dashboard({} if token else service.snapshot(), live=True).encode()
                self._send(200, page, "text/html; charset=utf-8")
            elif path == "/api/state":
                if not self._authorised():
                    return self._json(401, {"error": "token required"})
                self._json(200, service.snapshot())
            elif path == "/api/journal.csv":
                if not self._authorised():
                    return self._json(401, {"error": "token required"})
                self._send(200, journal_csv(service.journal).encode(), "text/csv; charset=utf-8")
            elif path == "/favicon.ico":
                self.send_response(204)
                self.end_headers()
            elif path == "/healthz":
                self._json(200, {"ok": service.last_error is None, "cycles": service.cycles})
            else:
                self._json(404, {"error": "not found"})

        def do_POST(self):
            if not self._authorised():
                return self._json(401, {"error": "token required"})
            if self.headers.get("Content-Type", "").split(";")[0].strip() != "application/json":
                return self._json(415, {"error": "send JSON"})
            try:
                length = min(int(self.headers.get("Content-Length", "0")), 64_000)
                body = json.loads(self.rfile.read(length) or b"{}")
            except (ValueError, json.JSONDecodeError):
                return self._json(400, {"error": "invalid JSON"})
            routes = {
                "/api/check": ("check", ("symbol", "bucket", "limit", "add")),
                "/api/enter": ("enter", ("symbol", "bucket", "limit", "expect_qty", "setup", "note")),
                "/api/add": ("add_to", ("key", "limit", "expect_qty")),
                "/api/exit": ("exit_position", ("key",)),
                "/api/unlock": ("unlock", ("key",)),
                "/api/pause": ("pause", ("note",)),
                "/api/resume": ("resume", ()),
                "/api/kill": ("kill", ()),
                "/api/watch": ("watch_add", ("symbol", "lot_size", "note")),
                "/api/unwatch": ("watch_remove", ("key",)),
                "/api/bucket": ("set_bucket", ("key", "bucket")),
                "/api/earnings": ("set_earnings", ("key", "date")),
                "/api/sector": ("set_sector", ("key", "sector")),
                "/api/settings": ("set_settings", ("time_stop_weeks", "time_stop_min_gain",
                                                   "earnings_warn_days", "risk_per_trade_usd")),
                "/api/note": ("set_trade_note", ("trade_id", "setup", "note")),
            }
            route = routes.get(self.path.split("?", 1)[0])
            if route is None:
                return self._json(404, {"error": "not found"})
            name, allowed = route
            kwargs = {k: body[k] for k in allowed if body.get(k) not in (None, "")}
            if name in ("set_earnings", "set_trade_note"):   # an empty value clears
                kwargs.update({k: body[k] for k in allowed if k in body and body[k] in ("", None) and k != "key"})
            try:
                result = service.submit(name, **kwargs).result(timeout=timeout)
            except TimeoutError:
                return self._json(504, {"error": "The trading loop didn't answer in time. Is IBKR connected?"})
            except (LookupError, ValueError, KeyError) as exc:
                return self._json(400, {"error": str(exc).strip("'\"")})
            except Exception as exc:
                return self._json(500, {"error": f"{type(exc).__name__}: {exc}"})
            self._json(200, result)

    return ThreadingHTTPServer((host, port), Handler)


def journal_csv(journal) -> str:
    import csv
    import io

    from .stats import trade_r

    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["id", "stock", "type", "setup", "opened", "closed", "days", "entry", "result_pct",
                "result_usd", "r_multiple", "stop_out", "adds", "best_pct", "worst_pct", "note"])
    for t in journal.closed_trades():
        r = trade_r(t)
        w.writerow([t.id, t.key, t.bucket, t.setup, t.opened_at, t.closed_at, t.days_held, round(t.entry, 6),
                    round(t.realized_pct or 0, 5), round(t.realized_usd or 0, 2), "" if r is None else round(r, 3),
                    t.strike, t.adds, round(t.high_water / t.entry - 1, 5),
                    round((t.low_water or t.entry) / t.entry - 1, 5), t.note])
    return buf.getvalue()


def serve(service, host: str = "127.0.0.1", port: int = 8765, token: str = "") -> ThreadingHTTPServer:
    """Start the server on a daemon thread and return it."""
    httpd = make_server(service, host, port, token)
    threading.Thread(target=httpd.serve_forever, name="dashboard", daemon=True).start()
    return httpd
