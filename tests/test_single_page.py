"""The combined page: both dashboards in one file, hosted at one URL.

A merged document has failure modes the separate pages cannot have — duplicated
element ids, colliding chart handles, a tab strip that hides content from anyone
without JavaScript. Each of those is silent: the page still renders, it is just
wrong in a way you would only notice by using it. So each is asserted here
against the rendered bytes.
"""

from __future__ import annotations

import collections
import json
import re

import pandas as pd
import pytest

from fireplanner.dashboard import build_payload, render_single
from fireplanner.dashboard.build import add_decision
from fireplanner.data import SnapshotProvider
from fireplanner.publish import publish_site


@pytest.fixture(scope="module")
def payload():
    p = build_payload(SnapshotProvider("data/snapshots"), watchlist=["SPY"])
    return add_decision(p, reference_date=pd.Timestamp("2026-08-13"))


@pytest.fixture(scope="module")
def page(payload):
    return render_single(payload)


# ---------------------------------------------------------------- one document

def test_it_is_one_document(page):
    """Not two pages concatenated — one head, one body, one footer."""
    assert page.count("<main") == 1
    assert page.count("</main>") == 1
    # SVG carries its own <title> elements for accessibility; the document title
    # is the one at position zero.
    assert page.startswith("<title>")
    assert page.count("<footer") == 1


def test_both_pages_are_present(page):
    # the decision half
    assert "Today's instruction" in page
    assert 'id="panel-decision"' in page
    # the analysis half
    assert "Market regime" in page
    assert "price against its trend anchors" in page
    assert 'id="panel-analysis"' in page


def test_no_duplicate_element_ids(page):
    """The merge's sharpest edge.

    ``getElementById`` returns the first match, so a duplicated id silently
    wires the wrong element: two staleness banners and only one would ever be
    updated; two charts sharing a tooltip id and one would show the other's
    numbers.
    """
    ids = re.findall(r'\bid="([^"]+)"', page)
    dupes = [i for i, n in collections.Counter(ids).items() if n > 1]
    assert not dupes, f"duplicate element ids: {dupes}"


def test_exactly_one_staleness_banner(page):
    assert page.count('id="stale-banner"') == 1
    assert "does not refresh itself" in page


def test_no_external_requests(page):
    """The whole point of a self-contained file: it works from a file:// URL
    and cannot be broken by a CDN, a font host, or a strict CSP."""
    assert not re.search(r'(?:src|href)\s*=\s*["\']https?://', page)
    assert "fetch(" not in page
    assert "//cdn" not in page


# ---------------------------------------------------------------- tabs

def test_tabs_are_wired_to_their_panels(page):
    for pid in ("decision", "analysis"):
        assert f'id="tab-{pid}"' in page
        assert f'aria-controls="panel-{pid}"' in page
        assert f'id="panel-{pid}"' in page
    assert 'role="tablist"' in page
    assert page.count('role="tabpanel"') == 2


def test_the_first_tab_is_the_decision(page):
    """Whoever opens this wants the instruction, not the indicator gallery."""
    first_tab = page.index('class="tab"')
    assert page.index('id="tab-decision"') < page.index('id="tab-analysis"')
    assert page.index('id="panel-decision"') < page.index('id="panel-analysis"')
    assert page[first_tab:first_tab + 400].count('aria-selected="true"') == 1


def test_readable_without_javascript(page):
    """Progressive enhancement, not a dependency.

    Panels must never carry ``hidden`` in the markup — that would hide half the
    document from anyone with scripting off, and from a printout. Hiding is done
    by a CSS rule gated on a class only a script can add.
    """
    for panel in re.findall(r"<section class=\"tabpanel\"[^>]*>", page):
        assert "hidden" not in panel, panel
    assert ".fp-js .tabpanel:not([data-active])" in page
    assert '.fp-js .tabnav { display: flex; }' in page
    # and the tab strip itself is invisible until the script can drive it
    assert re.search(r"\.tabnav \{[^}]*display: none", page)


def test_printing_shows_everything(page):
    block = page[page.index("@media print"):]
    block = block[: block.index("}", block.index(".tabpanel"))]
    assert "display: block !important" in block


def test_a_payload_without_a_decision_is_refused(payload):
    bare = build_payload(SnapshotProvider("data/snapshots"), watchlist=["SPY"])
    with pytest.raises(ValueError, match="no decision"):
        render_single(bare)


# ---------------------------------------------------------------- publishing

def test_single_publish_writes_one_page(payload, tmp_path):
    manifest = publish_site(payload, out_dir=tmp_path / "site", single=True)
    assert set(p.name for p in (tmp_path / "site").iterdir()) == {"index.html", "status.json"}
    assert manifest["single"] is True
    assert manifest["files"]["index.html"] > 100_000


def test_switching_layout_sweeps_the_old_files(payload, tmp_path):
    """A file left behind keeps resolving, and keeps serving a frozen signal."""
    out = tmp_path / "site"
    publish_site(payload, out_dir=out, single=False)
    assert (out / "signal_desk.html").exists()

    manifest = publish_site(payload, out_dir=out, single=True)
    assert not (out / "signal_desk.html").exists()
    assert not (out / "dashboard.html").exists()
    assert sorted(manifest["removed"]) == ["dashboard.html", "signal_desk.html"]


def test_status_json_records_the_layout(payload, tmp_path):
    publish_site(payload, out_dir=tmp_path / "site", single=True)
    status = json.loads((tmp_path / "site" / "status.json").read_text())
    assert status["single_page"] is True


def test_redacted_single_page_leaks_no_account_figures(payload, account_secrets, tmp_path):
    """This is the file that goes on a domain. It cannot be un-published."""
    publish_site(payload, out_dir=tmp_path / "pub", single=True, redact=True)
    blob = (tmp_path / "pub" / "index.html").read_text()
    blob += (tmp_path / "pub" / "status.json").read_text()

    leaked = [s for s in account_secrets if s in blob]
    assert not leaked, f"redacted single page leaked: {leaked}"
    assert not re.findall(r"\$[0-9]{1,3}(?:,[0-9]{3})+", blob)


def test_unredacted_single_page_does_contain_them(payload, account_secrets, tmp_path):
    """Guard against the test above passing because the data was never there."""
    publish_site(payload, out_dir=tmp_path / "priv", single=True, redact=False)
    blob = (tmp_path / "priv" / "index.html").read_text()
    missing = [s for s in account_secrets if s not in blob]
    assert not missing, f"not real account figures, so redacting them proves nothing: {missing}"


# ---------------------------------------------------------------- layout

def test_wide_tables_scroll_inside_their_own_box(page):
    """Otherwise the widest table sets the width of the page and a phone gets a
    document that slides sideways — measured at 408px against a 390px viewport."""
    assert ".tablewrap { overflow-x: auto;" in page
    tables = [m.start() for m in re.finditer(r"<table\b", page)]
    assert tables, "no tables on the page — this test would pass vacuously"
    unwrapped = [
        page[max(0, i - 60):i + 30]
        for i in tables
        if not page[:i].rstrip().endswith('<div class="tablewrap">')
    ]
    assert not unwrapped, f"tables outside a scroll container: {unwrapped}"
