"""Telegram notification: fires on real changes, stays quiet otherwise.

The two failure modes are opposite and both fatal to a notifier's usefulness:
missing the one event that mattered, and crying wolf until you mute it. Both are
tested here against a synthetic 50-day break, because waiting for a real one is
not a test strategy.
"""

from __future__ import annotations

import json

import numpy as np
import pandas as pd
import pytest

from fireplanner import indicators as ind
from fireplanner.dashboard import build_payload
from fireplanner.dashboard.build import add_decision
from fireplanner.data import SnapshotProvider
from fireplanner.notify import (
    TelegramNotifier,
    detect_events,
    format_message,
    notify_if_changed,
)


@pytest.fixture(scope="module")
def payload():
    p = build_payload(SnapshotProvider("data/snapshots"), watchlist=["SPY"])
    return add_decision(p, reference_date=pd.Timestamp("2026-08-14"))


def _state(payload):
    from fireplanner.notify import _state_from

    return _state_from(payload)


# ---------------------------------------------------------------- client

def test_notifier_reports_missing_configuration():
    n = TelegramNotifier(token="", chat_id="")
    assert not n.configured
    with pytest.raises(RuntimeError, match="not configured"):
        n.send("hello")


def test_dry_run_records_without_sending():
    n = TelegramNotifier(token="t", chat_id="c", dry_run=True)
    out = n.send("hello")
    assert out["dry_run"] is True
    # the source label is prepended by default — see test_messages_are_labelled_with_their_source
    assert len(n.sent) == 1
    assert n.sent[0].endswith("hello")


# ---------------------------------------------------------------- quiet by default

def test_no_events_when_nothing_changed(payload):
    """The common case must produce silence."""
    assert detect_events(payload, previous=_state(payload)) == []


def test_notify_is_silent_and_still_records_state(payload, tmp_path):
    state = tmp_path / "state.json"
    state.write_text(json.dumps(_state(payload)))
    n = TelegramNotifier(token="t", chat_id="c", dry_run=True)
    result = notify_if_changed(payload, notifier=n, state_path=state)
    assert result.sent is False
    assert result.events == []
    assert n.sent == []
    # state is refreshed so the *next* real change is detected against today
    assert json.loads(state.read_text())["date"] == payload.decision["date"]


# ---------------------------------------------------------------- real events

def test_target_change_is_reported(payload):
    prev = _state(payload)
    prev["target_pct"] = 0.70          # yesterday the target was higher
    events = detect_events(payload, previous=prev)
    kinds = {e.kind for e in events}
    assert "target_changed" in kinds
    ev = next(e for e in events if e.kind == "target_changed")
    assert "40%" in ev.headline and ev.urgency != "info"


def test_action_change_is_reported(payload):
    prev = _state(payload)
    prev["action"] = "HOLD"
    payload.decision["action"] = "SELL"
    try:
        events = detect_events(payload, previous=prev)
        assert any(e.kind == "action_changed" for e in events)
    finally:
        payload.decision["action"] = "WAIT"


def test_first_close_below_the_50_day_warns(payload):
    """One close below is a heads-up. It must not claim the break is confirmed."""
    bench = payload.benchmark
    e = payload.enriched[bench]
    original = e["close"].iloc[-1]
    try:
        # drop the last close under the 50-day
        e.iloc[-1, e.columns.get_loc("close")] = float(e["sma50"].iloc[-1]) - 5.0
        events = detect_events(payload, previous=_state(payload) | {"below_sma50": False})
        kinds = {ev.kind for ev in events}
        assert "break_warning" in kinds
        assert "break_confirmed" not in kinds
        warn = next(ev for ev in events if ev.kind == "break_warning")
        assert "Nothing has changed yet" in warn.detail
    finally:
        e.iloc[-1, e.columns.get_loc("close")] = original


def test_confirmed_break_is_urgent(payload):
    """Two consecutive closes below is the event worth waking someone for."""
    alloc = payload.allocation
    idx = alloc.dropna(subset=["target"]).index[-1]
    original = alloc.loc[idx, "urgent_exit"]
    try:
        alloc.loc[idx, "urgent_exit"] = 1.0
        events = detect_events(payload, previous={"urgent_exit": False})
        confirmed = [e for e in events if e.kind == "break_confirmed"]
        assert confirmed, [e.kind for e in events]
        assert confirmed[0].urgency == "urgent"
        assert "overrides the cooldown" in confirmed[0].detail
    finally:
        alloc.loc[idx, "urgent_exit"] = original


def test_confirmed_break_is_not_repeated(payload):
    """Still-broken is not news; it must fire on the transition only."""
    alloc = payload.allocation
    idx = alloc.dropna(subset=["target"]).index[-1]
    original = alloc.loc[idx, "urgent_exit"]
    try:
        alloc.loc[idx, "urgent_exit"] = 1.0
        events = detect_events(payload, previous={"urgent_exit": True})
        assert not any(e.kind == "break_confirmed" for e in events)
    finally:
        alloc.loc[idx, "urgent_exit"] = original


# ---------------------------------------------------------------- message

def test_message_leads_with_the_most_urgent_event(payload):
    from fireplanner.notify import SignalEvent

    events = [
        SignalEvent("target_changed", "action", "Target cut to 40%"),
        SignalEvent("break_confirmed", "urgent", "Confirmed break of the 50-day average"),
    ]
    msg = format_message(payload, events)
    assert msg.splitlines()[0].endswith("</b>")
    assert "Confirmed break" in msg.splitlines()[0]


def test_message_carries_the_numbers_needed_to_act(payload):
    from fireplanner.notify import SignalEvent

    msg = format_message(payload, [SignalEvent("x", "info", "Status")])
    for token in ("target", "you hold", "50-day", "regime"):
        assert token in msg
    assert payload.decision["date"] in msg


def test_message_escapes_html(payload):
    from fireplanner.notify import SignalEvent

    msg = format_message(payload, [SignalEvent("x", "info", "<script>alert(1)</script>")])
    assert "<script>" not in msg
    assert "&lt;script&gt;" in msg


# ---------------------------------------------------------------- delivery

def test_sends_once_per_session(payload, tmp_path):
    state = tmp_path / "state.json"
    prev = _state(payload)
    prev["target_pct"] = 0.70          # force a real change
    state.write_text(json.dumps(prev))

    n = TelegramNotifier(token="t", chat_id="c", dry_run=True)
    first = notify_if_changed(payload, notifier=n, state_path=state)
    assert first.sent and len(n.sent) == 1

    # The second run sees state matching today, so it finds nothing to report.
    # Either reason is acceptable; what matters is that no second message goes out.
    second = notify_if_changed(payload, notifier=n, state_path=state)
    assert second.sent is False
    assert len(n.sent) == 1


def test_force_sends_even_with_no_change(payload, tmp_path):
    state = tmp_path / "state.json"
    state.write_text(json.dumps(_state(payload)))
    n = TelegramNotifier(token="t", chat_id="c", dry_run=True)
    result = notify_if_changed(payload, notifier=n, state_path=state, force=True)
    assert result.sent and len(n.sent) == 1


def test_unconfigured_returns_the_message_without_raising(payload, tmp_path):
    """A missing token must not crash a scheduled run."""
    state = tmp_path / "state.json"
    prev = _state(payload)
    prev["target_pct"] = 0.70
    state.write_text(json.dumps(prev))

    result = notify_if_changed(
        payload, notifier=TelegramNotifier(token="", chat_id=""), state_path=state
    )
    assert result.sent is False
    assert "not configured" in result.reason
    assert result.message  # still rendered, so it can be logged


def test_corrupt_state_file_is_survivable(payload, tmp_path):
    state = tmp_path / "state.json"
    state.write_text("{ not json")
    n = TelegramNotifier(token="t", chat_id="c", dry_run=True)
    result = notify_if_changed(payload, notifier=n, state_path=state)
    assert isinstance(result.events, list)


# ---------------------------------------------------------------- shared bot

def test_credentials_can_come_from_an_existing_env_file(tmp_path):
    """Reuse a bot from another project without copying its token."""
    env = tmp_path / "other-project.env"
    env.write_text(
        "# daily market update\n"
        "export TELEGRAM_BOT_TOKEN='123:abc'\n"
        'TELEGRAM_CHAT_ID="-1001234567890"\n'
        "TELEGRAM_THREAD_ID=42\n"
        "UNRELATED=ignored\n"
    )
    n = TelegramNotifier(env_file=env, dry_run=True)
    assert n.configured
    assert n.chat_id == "-1001234567890"
    assert n.thread_id == "42"


def test_explicit_arguments_beat_the_env_file(tmp_path):
    env = tmp_path / ".env"
    env.write_text("TELEGRAM_BOT_TOKEN=from_file\nTELEGRAM_CHAT_ID=from_file\n")
    n = TelegramNotifier(token="explicit", env_file=env, dry_run=True)
    assert n.token == "explicit"
    assert n.chat_id == "from_file"


def test_missing_env_file_is_an_explicit_error(tmp_path):
    with pytest.raises(FileNotFoundError):
        TelegramNotifier(env_file=tmp_path / "nope.env")


def test_messages_are_labelled_with_their_source():
    """One bot, two systems — the reader must not have to guess which spoke."""
    n = TelegramNotifier(token="t", chat_id="c", dry_run=True, source="FirePlanner")
    n.send("Target cut to 40%")
    assert n.sent[0].startswith("<b>[FirePlanner]</b>")
    assert "Target cut to 40%" in n.sent[0]


def test_source_label_is_escaped():
    n = TelegramNotifier(token="t", chat_id="c", dry_run=True, source="<b>x</b>")
    n.send("hi")
    assert "&lt;b&gt;x&lt;/b&gt;" in n.sent[0]


def test_source_can_be_disabled():
    n = TelegramNotifier(token="t", chat_id="c", dry_run=True, source="")
    n.send("plain")
    assert n.sent[0] == "plain"
