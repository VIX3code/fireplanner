"""Static publishing, and the guarantee that redaction actually redacts.

The redaction test is the one that matters. A page published to a domain with
the account figures still in it cannot be un-published, so this asserts on the
rendered bytes rather than on the payload — the payload being clean is not the
claim; the HTML being clean is.
"""

from __future__ import annotations

import json
import pathlib
import re

import pandas as pd
import pytest

from fireplanner.dashboard import build_payload
from fireplanner.dashboard.build import add_decision
from fireplanner.data import SnapshotProvider
from fireplanner.publish import publish_site, redact_payload


@pytest.fixture(scope="module")
def payload():
    p = build_payload(SnapshotProvider("data/snapshots"), watchlist=["SPY"])
    return add_decision(p, reference_date=pd.Timestamp("2026-08-13"))


def test_publish_writes_the_expected_files(payload, tmp_path):
    manifest = publish_site(payload, out_dir=tmp_path / "site")
    for name in ("index.html", "signal_desk.html", "dashboard.html", "status.json"):
        assert (tmp_path / "site" / name).exists()
        assert manifest["files"][name] > 0


def test_published_pages_have_no_external_requests(payload, tmp_path):
    """The whole two-tier deployment rests on this being true."""
    publish_site(payload, out_dir=tmp_path / "site")
    for name in ("index.html", "signal_desk.html", "dashboard.html"):
        text = (tmp_path / "site" / name).read_text()
        assert not re.search(r'(?:src|href)\s*=\s*["\']https?://', text), name
        assert "//cdn" not in text, name


def test_redacted_pages_leak_no_account_figures(payload, account_secrets, tmp_path):
    publish_site(payload, out_dir=tmp_path / "pub", redact=True)
    blob = "".join(
        (tmp_path / "pub" / n).read_text()
        for n in ("index.html", "signal_desk.html", "dashboard.html", "status.json")
    )
    leaked = [s for s in account_secrets if s in blob]
    assert not leaked, f"redacted build leaked: {leaked}"
    # and no large currency amount of any kind survives
    assert not re.findall(r"\$[0-9]{1,3}(?:,[0-9]{3})+", blob)


def test_unredacted_build_does_contain_them(payload, account_secrets, tmp_path):
    """Guard against the test above passing because the data was never there.

    ``all``, not ``any``: with ``any`` a single matching ticker keeps this green
    while every currency figure in the list is one the renderer never emits, and
    the redaction test above quietly stops checking the numbers.
    """
    publish_site(payload, out_dir=tmp_path / "priv", redact=False)
    blob = "".join(
        (tmp_path / "priv" / n).read_text() for n in ("signal_desk.html", "dashboard.html")
    )
    missing = [s for s in account_secrets if s not in blob]
    assert not missing, f"these are not real account figures, so redacting them proves nothing: {missing}"


def test_redaction_keeps_the_signal_intact(payload):
    red = redact_payload(payload)
    assert red.decision["target_pct"] == payload.decision["target_pct"]
    assert red.decision["action"] == payload.decision["action"]
    assert red.decision["regime_label"] == payload.decision["regime_label"]
    # ...but drops everything that reveals size
    assert red.decision["equity"] is None
    assert red.decision["shares_delta"] is None
    assert red.decision["dollars_delta"] is None
    assert red.positions is None


def test_redaction_does_not_mutate_the_original(payload):
    before = dict(payload.decision)
    redact_payload(payload)
    assert payload.decision == before
    assert payload.positions is not None


def test_status_json_is_machine_readable(payload, tmp_path):
    publish_site(payload, out_dir=tmp_path / "site", redact=True)
    status = json.loads((tmp_path / "site" / "status.json").read_text())
    assert status["redacted"] is True
    assert status["decision"]["action"] in {"BUY", "SELL", "HOLD", "WAIT"}
    assert 0.0 <= status["decision"]["target_pct"] <= 1.0
    # the field a monitor would alert on
    assert pd.Timestamp(status["generated_utc"]) is not pd.NaT


# ---------------------------------------------------------------- staleness

def test_pages_carry_a_view_time_staleness_banner(payload, tmp_path):
    """A static page must be able to tell the reader it has gone stale.

    Staleness computed at build time is frozen into the file: a page generated
    today reports "1 day old" forever, including weeks later when the signal has
    moved on. The banner is therefore filled in from the viewer's own clock.
    """
    publish_site(payload, out_dir=tmp_path / "site")
    for name in ("signal_desk.html", "dashboard.html"):
        text = (tmp_path / "site" / name).read_text()
        assert 'id="stale-banner"' in text, name
        # the bar date is baked in for the client to compare against
        assert f'data-bars-through="{payload.as_of}"' in text, name
        # and the script that does the comparison ships with it
        assert "does not refresh itself" in text, name
        # no network required to work it out
        assert "fetch(" not in text, name


def test_fresh_pages_start_with_the_banner_hidden(payload, tmp_path):
    publish_site(payload, out_dir=tmp_path / "site")
    text = (tmp_path / "site" / "signal_desk.html").read_text()
    banner = text[text.index('id="stale-banner"'):]
    banner = banner[: banner.index(">") + 1]
    assert "hidden" in banner, banner


def test_generated_pages_are_not_tracked_by_git():
    """Rendered pages carry account-derived figures; they are published, not committed.

    dashboard.html was ignored from the start and signal_desk.html was not —
    an inconsistency that put position counts into four commits before it was
    caught. This asserts the pair stays in step.
    """
    import subprocess

    tracked = subprocess.run(
        ["git", "ls-files", "signal_desk.html", "dashboard.html", "site"],
        capture_output=True, text=True, cwd=pathlib.Path(__file__).resolve().parent.parent,
    ).stdout.strip()
    assert tracked == "", f"generated output is tracked in git: {tracked!r}"
