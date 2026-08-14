"""Push the signal to Telegram when something actually changes.

Why this exists rather than a broker alert
------------------------------------------
IBKR alerts fire on a *price level*, intraday, against a number fixed when you
create them. The rule that matters here is none of those things: it is **two
consecutive closes below the 50-day average**, evaluated after the close,
against an average that moves every session. A price alert can only approximate
it, and drifts out of date as the average rises.

This module watches the real condition, because the model has already computed
it. It also stays quiet: a notifier that messages you every day trains you to
ignore it, so by default nothing is sent unless the decision changed or a break
was confirmed.

Setup
-----
1. Message ``@BotFather`` on Telegram, ``/newbot``, copy the token.
2. Message your new bot once, then open
   ``https://api.telegram.org/bot<TOKEN>/getUpdates`` and copy ``chat.id``.
3. Export ``TELEGRAM_BOT_TOKEN`` and ``TELEGRAM_CHAT_ID``.

No third-party dependency: the Bot API is a plain HTTPS POST.
"""

from __future__ import annotations

import html
import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

__all__ = [
    "TelegramNotifier",
    "SignalEvent",
    "detect_events",
    "format_message",
    "notify_if_changed",
    "load_env_file",
]

API = "https://api.telegram.org"


def load_env_file(path: str | Path) -> dict[str, str]:
    """Read ``KEY=VALUE`` pairs from an existing env file.

    Exists so a bot already serving another project can be reused without
    copying its token to a second place. Two copies of a secret is one more than
    necessary, and the second is the one that gets committed by accident.

    Deliberately minimal: no interpolation, no export handling beyond a leading
    ``export``, no shell semantics. Values are returned, never logged.
    """
    out: dict[str, str] = {}
    p = Path(path).expanduser()
    if not p.exists():
        raise FileNotFoundError(f"no env file at {p}")

    for raw in p.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        out[key.strip()] = value
    return out


class TelegramNotifier:
    """Minimal Telegram Bot API client.

    ``dry_run`` renders and records messages without sending, which is how the
    tests exercise this and how you can check the wiring before handing over a
    real token.
    """

    def __init__(
        self,
        token: str | None = None,
        chat_id: str | None = None,
        dry_run: bool = False,
        timeout: int = 15,
        base_url: str = API,
        thread_id: str | None = None,
        env_file: str | Path | None = None,
        source: str = "FirePlanner",
    ):
        env = load_env_file(env_file) if env_file else {}

        def pick(explicit: str | None, key: str) -> str:
            return explicit or env.get(key) or os.environ.get(key, "")

        self.token = pick(token, "TELEGRAM_BOT_TOKEN")
        self.chat_id = pick(chat_id, "TELEGRAM_CHAT_ID")
        # Forum topic, when one bot serves several purposes in one group.
        self.thread_id = pick(thread_id, "TELEGRAM_THREAD_ID")
        # Prefixes every message. Sharing a bot between systems is fine; leaving
        # the reader to guess which one just said "target cut to 40%" is not.
        self.source = source
        self.dry_run = dry_run
        self.timeout = timeout
        self.base_url = base_url.rstrip("/")
        self.sent: list[str] = []

    @property
    def configured(self) -> bool:
        return bool(self.token and self.chat_id)

    def send(self, text: str, disable_preview: bool = True) -> dict:
        """Send one message. Returns the API response, or a dry-run stub."""
        if not self.configured:
            raise RuntimeError(
                "Telegram is not configured — set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID"
            )
        if self.source:
            text = f"<b>[{html.escape(self.source)}]</b>\n{text}"

        self.sent.append(text)
        if self.dry_run:
            return {"ok": True, "dry_run": True, "text": text}

        body = {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": disable_preview,
        }
        if self.thread_id:
            body["message_thread_id"] = int(self.thread_id)
        payload = json.dumps(body).encode()
        req = urllib.request.Request(
            f"{self.base_url}/bot{self.token}/sendMessage",
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            # Telegram puts the useful reason in the body, not the status line.
            detail = exc.read().decode(errors="replace")
            raise RuntimeError(f"Telegram rejected the message ({exc.code}): {detail}") from exc


@dataclass
class SignalEvent:
    """Something worth interrupting someone for."""

    kind: str
    urgency: str  # "urgent" | "action" | "info"
    headline: str
    detail: str = ""

    def as_dict(self) -> dict:
        return {"kind": self.kind, "urgency": self.urgency, "headline": self.headline}


def detect_events(payload, previous: dict | None = None) -> list[SignalEvent]:
    """Compare today's decision against the last notified state.

    Returns only genuine changes. An unchanged signal produces an empty list,
    which is the common case and the point.
    """
    previous = previous or {}
    d = payload.decision or {}
    alloc = payload.allocation
    events: list[SignalEvent] = []

    if not d or alloc is None or alloc.empty:
        return events

    row = alloc.dropna(subset=["target"]).iloc[-1]
    target = float(d["target_pct"])
    action = str(d["action"])

    # ---- the urgent one: a confirmed break of the 50-day ------------------
    urgent_now = bool(row.get("urgent_exit", 0))
    if urgent_now and not previous.get("urgent_exit", False):
        events.append(
            SignalEvent(
                kind="break_confirmed",
                urgency="urgent",
                headline="Confirmed break of the 50-day average",
                detail=(
                    "Two consecutive closes below the 50-day. The tactical sleeve is forced flat "
                    f"and the target drops to {target * 100:.0f}%. This overrides the cooldown."
                ),
            )
        )

    # ---- a single close below is a heads-up, not yet a signal -------------
    # Both sides of this comparison come from the same frame on purpose: reading
    # the close from the allocation table and the average from the enriched one
    # invites them to drift apart and silently break the test of a break.
    bench_bars = payload.enriched[payload.benchmark]
    close = float(bench_bars["close"].iloc[-1])
    sma50 = float(bench_bars["sma50"].iloc[-1])
    if close < sma50 and not urgent_now and not previous.get("below_sma50", False):
        events.append(
            SignalEvent(
                kind="break_warning",
                urgency="action",
                headline="First close below the 50-day average",
                detail=(
                    f"Close {close:,.2f} vs 50-day {sma50:,.2f}. One more close below confirms the "
                    "break and forces the sleeve flat. Nothing has changed yet."
                ),
            )
        )

    # ---- the committed target moved --------------------------------------
    prev_target = previous.get("target_pct")
    if prev_target is not None and abs(target - float(prev_target)) > 1e-9:
        direction = "raised" if target > float(prev_target) else "cut"
        events.append(
            SignalEvent(
                kind="target_changed",
                urgency="action",
                headline=f"Target {direction} to {target * 100:.0f}%",
                detail=f"Was {float(prev_target) * 100:.0f}%.",
            )
        )

    # ---- the instruction changed shape -----------------------------------
    prev_action = previous.get("action")
    if prev_action is not None and action != prev_action and action in {"BUY", "SELL"}:
        events.append(
            SignalEvent(
                kind="action_changed",
                urgency="action",
                headline=f"Instruction is now {action}",
                detail=f"Was {prev_action}.",
            )
        )

    return events


def format_message(payload, events: list[SignalEvent]) -> str:
    """Render events plus the current state as Telegram HTML."""
    d = payload.decision or {}
    bench = html.escape(payload.benchmark)
    e = payload.enriched.get(payload.benchmark)
    last = e.iloc[-1] if e is not None else None

    icon = {"urgent": "⚠️", "action": "\U0001f514", "info": "ℹ️"}
    top = max(events, key=lambda ev: ["info", "action", "urgent"].index(ev.urgency))

    lines = [f"{icon.get(top.urgency, '')} <b>{html.escape(top.headline)}</b>"]
    if top.detail:
        lines.append(html.escape(top.detail))

    for ev in events:
        if ev is top:
            continue
        lines.append(f"\n{icon.get(ev.urgency, '')} {html.escape(ev.headline)}")
        if ev.detail:
            lines.append(html.escape(ev.detail))

    lines.append("")
    lines.append(f"<b>{bench} {d.get('date', '')}</b>")
    if last is not None:
        lines.append(
            f"close <code>{float(last['close']):,.2f}</code>  ·  "
            f"50-day <code>{float(last['sma50']):,.2f}</code>  ·  "
            f"{100 * (float(last['close']) / float(last['sma50']) - 1):+.2f}%"
        )
    lines.append(
        f"target <b>{d.get('target_pct', 0) * 100:.0f}%</b>  ·  "
        f"you hold <b>{d.get('current_pct', 0) * 100:.0f}%</b>  ·  "
        f"{html.escape(str(d.get('action', '')))}"
    )
    lines.append(
        f"regime {html.escape(str(d.get('regime_label', '')))}  ·  score {d.get('score', 0):.0f}"
    )
    return "\n".join(lines)


def _state_from(payload) -> dict:
    d = payload.decision or {}
    alloc = payload.allocation
    row = alloc.dropna(subset=["target"]).iloc[-1] if alloc is not None and not alloc.empty else {}
    e = payload.enriched.get(payload.benchmark)
    close = float(e["close"].iloc[-1]) if e is not None else None
    sma50 = float(e["sma50"].iloc[-1]) if e is not None else None
    return {
        "date": d.get("date"),
        "target_pct": d.get("target_pct"),
        "action": d.get("action"),
        "urgent_exit": bool(row.get("urgent_exit", 0)) if len(row) else False,
        "below_sma50": (close < sma50) if (close is not None and sma50 is not None) else False,
    }


@dataclass
class NotifyResult:
    events: list = field(default_factory=list)
    sent: bool = False
    message: str = ""
    reason: str = ""


def notify_if_changed(
    payload,
    notifier: TelegramNotifier | None = None,
    state_path: str | Path = ".cache/notify_state.json",
    force: bool = False,
) -> NotifyResult:
    """Send a message only when the decision changed since the last send.

    State is kept on disk so repeated runs on the same session stay silent —
    the scheduler may fire more than once a day, and a notifier that repeats
    itself is one you learn to ignore.
    """
    notifier = notifier or TelegramNotifier()
    path = Path(state_path)
    previous = {}
    if path.exists():
        try:
            previous = json.loads(path.read_text())
        except json.JSONDecodeError:
            previous = {}

    events = detect_events(payload, previous)
    current = _state_from(payload)

    if force and not events:
        events = [
            SignalEvent(
                kind="manual",
                urgency="info",
                headline="Signal status",
                detail="Sent on request; nothing has changed.",
            )
        ]

    if not events:
        # Still record state, so the first real change is detected correctly.
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(current, indent=2))
        return NotifyResult(events=[], sent=False, reason="no change since last notification")

    # Same session already notified? Stay quiet unless forced.
    if not force and previous.get("date") == current.get("date") and previous.get("notified"):
        return NotifyResult(events=events, sent=False, reason="already notified for this session")

    message = format_message(payload, events)
    if not notifier.configured:
        return NotifyResult(
            events=events, sent=False, message=message,
            reason="Telegram not configured (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID)",
        )

    notifier.send(message)
    current["notified"] = True
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(current, indent=2))
    return NotifyResult(events=events, sent=True, message=message, reason="sent")
