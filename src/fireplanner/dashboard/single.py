"""Both pages as one self-contained HTML file.

The two-page split exists because the questions are different: *what do I do
today* and *what is the market doing*. That split is right on a desk with two
tabs open and wrong on a phone, on a bookmark, and on a domain where "the
dashboard" should be one URL.

This module emits a single document holding both, switched by a tab strip. It
composes the same section builders the separate pages use, so there is exactly
one implementation of every card — a merged page assembled by copy-paste would
drift from its originals within a week.

Three things the merge has to get right, all of them tested:

* **One staleness banner.** The script that ages the page looks the banner up
  by element id. Two copies and only the first would ever be updated.
* **Unique chart ids.** Tooltips resolve ``#<id>-hit`` and ``#<id>-tip``
  globally; a collision would wire one chart's crosshair to another's data.
* **Readable without JavaScript.** The tab strip is progressive enhancement.
  With scripting off — or on a printout — every panel is visible and the
  document simply reads top to bottom.
"""

from __future__ import annotations

import html

from .desk import _DESK_CSS, DISCLAIMER, desk_sections, stale_banner
from .render import _CSS, _JS, dashboard_sections

__all__ = ["render_single"]


#: ``(panel id, tab label)``. The label doubles as the heading shown in place of
#: the tab strip when scripting is off.
PANELS = [
    ("decision", "Today's decision"),
    ("analysis", "Market analysis"),
]


def render_single(p, title: str = "S&P 500 Signal Desk") -> str:
    """Render the decision page and the analysis page as one document."""
    if not p.decision:
        raise ValueError("payload has no decision — build it with include_decision=True")

    d = p.decision
    bench = html.escape(p.benchmark)
    as_of = d["date"]

    tabs = "".join(
        f'<button type="button" class="tab" role="tab" id="tab-{pid}" '
        f'aria-controls="panel-{pid}" aria-selected="{"true" if i == 0 else "false"}">'
        f"{html.escape(label)}</button>"
        for i, (pid, label) in enumerate(PANELS)
    )

    def panel(pid: str, label: str, body: list[str], active: bool) -> str:
        return (
            f'<section class="tabpanel" id="panel-{pid}" role="tabpanel" '
            f'aria-labelledby="tab-{pid}"{" data-active" if active else ""}>'
            f'<h2 class="panel-title">{html.escape(label)}</h2>'
            f'{"".join(body)}</section>'
        )

    body = [
        f"""
<header class="page-head">
  <div>
    <h1>{html.escape(title)}</h1>
    <p class="sub">How much of your equity should sit in {bench} today, and why ·
       daily bars, decisions reviewed at the close · data via Interactive Brokers</p>
  </div>
  <div class="asof"><span class="asof-label">Signal as of</span><span class="asof-date">{as_of}</span></div>
</header>""",
        stale_banner(as_of, p.staleness_days),
        f'<nav class="tabnav" role="tablist" aria-label="Sections">{tabs}</nav>',
        panel("decision", PANELS[0][1], desk_sections(p, title, chrome=False), active=True),
        panel("analysis", PANELS[1][1], dashboard_sections(p, title, chrome=False), active=False),
        DISCLAIMER,
    ]

    # The ``fp-js`` class is set before the body paints, so the inactive panel is
    # never briefly visible. Doing the hiding in CSS rather than by toggling
    # ``hidden`` after load is what keeps the no-JavaScript path working: no
    # class, no rule, everything shows.
    return (
        f"<title>{html.escape(title)}</title>"
        + _CSS
        + _DESK_CSS
        + _SINGLE_CSS
        + '<script>document.documentElement.classList.add("fp-js");</script>'
        + f'<main class="viz-root">{"".join(body)}</main>'
        + _JS
        + _TAB_JS
    )


_SINGLE_CSS = """
<style>
/* Sized to its labels rather than stretched across the page: at 1120px two
   full-width buttons stop reading as tabs and start reading as a menu. */
.tabnav { display: none; gap: 4px; margin: 0 0 18px; padding: 4px;
          background: var(--surface-1); border: 1px solid var(--border);
          border-radius: 10px; width: fit-content; max-width: 100%; }
.fp-js .tabnav { display: flex; }
.tab { font: inherit; font-size: 13.5px; font-weight: 600; color: var(--text-secondary);
       background: none; border: 0; border-radius: 7px; padding: 9px 18px; cursor: pointer;
       white-space: nowrap; }
.tab:hover { color: var(--text-primary); }
.tab[aria-selected="true"] { background: var(--page); color: var(--text-primary);
                             box-shadow: inset 0 0 0 1px var(--border); }
.tab:focus-visible { outline: 2px solid var(--series-1); outline-offset: 1px; }

/* Without the class — scripting off — every panel stays visible and its
   heading names it, so the document still reads as one continuous page. */
.fp-js .tabpanel:not([data-active]) { display: none; }
.panel-title { font-size: 12px; color: var(--muted); text-transform: uppercase;
               letter-spacing: 0.08em; margin: 26px 0 12px; }
.fp-js .panel-title { display: none; }

@media print {
  .tabnav { display: none !important; }
  .tabpanel { display: block !important; }
  .panel-title { display: block !important; }
}
@media (max-width: 480px) {
  .tabnav { width: auto; }
  .tab { flex: 1; font-size: 12.5px; padding: 9px 8px; }
}
</style>
"""


_TAB_JS = """
<script>
(function () {
  var tabs = Array.prototype.slice.call(document.querySelectorAll('.tabnav .tab'));
  if (!tabs.length) return;

  function show(id, push) {
    tabs.forEach(function (t) {
      var on = t.id === 'tab-' + id;
      t.setAttribute('aria-selected', on ? 'true' : 'false');
      var panel = document.getElementById(t.getAttribute('aria-controls'));
      if (panel) {
        if (on) { panel.setAttribute('data-active', ''); }
        else { panel.removeAttribute('data-active'); }
      }
    });
    // replaceState, not a hash assignment: the tab is a view of one page, so it
    // belongs in the URL for bookmarking without filling the back button.
    if (push && window.history && history.replaceState) {
      history.replaceState(null, '', '#' + id);
    }
  }

  tabs.forEach(function (t) {
    t.addEventListener('click', function () {
      show(t.id.replace(/^tab-/, ''), true);
      window.scrollTo({top: 0, behavior: 'smooth'});
    });
  });

  // Arrow keys move between tabs, which is what a tablist is expected to do.
  document.querySelector('.tabnav').addEventListener('keydown', function (e) {
    var i = tabs.indexOf(document.activeElement);
    if (i < 0) return;
    var next = e.key === 'ArrowRight' ? i + 1 : (e.key === 'ArrowLeft' ? i - 1 : -1);
    if (next < 0 || next >= tabs.length) return;
    e.preventDefault();
    tabs[next].focus();
    show(tabs[next].id.replace(/^tab-/, ''), true);
  });

  var initial = (location.hash || '').replace(/^#/, '');
  if (initial && document.getElementById('tab-' + initial)) { show(initial, false); }
})();
</script>
"""
