"""Shared fixtures.

``account_secrets`` is the important one: it is the list a redaction test
asserts the *absence* of, so it is only as good as its formatting. The pages
render money with ``fmt(v, 0)`` — thousands separators, rounded to the nearest
dollar — and an earlier version of this list truncated instead. ``78,463``
never appeared on any page, so asserting it was missing proved nothing about
whether ``78,464`` was there. Every form below is one the renderer actually
emits, verified by the companion test that the *unredacted* build contains them.
"""

from __future__ import annotations

import pytest


@pytest.fixture(scope="module")
def account_secrets(payload):
    """Every figure that must never reach a published page.

    Depends on a module-level ``payload`` fixture defined by the test file that
    asks for this one.
    """
    summary = payload.account["summary"]
    secrets = []
    for key in ("net_liquidation", "gross_position_value", "total_cash_value"):
        value = summary.get(key)
        if value:
            secrets.append(f"{float(value):,.0f}")   # as the pages render it
    if payload.positions is not None:
        secrets += [str(s) for s in payload.positions["symbol"].head(8)]
    return secrets
