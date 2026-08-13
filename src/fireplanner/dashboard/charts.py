"""Inline-SVG chart primitives.

Self-contained by design: no chart library, no CDN, no runtime fetch. Everything
is an SVG string embedded in the page, which keeps the dashboard viewable from a
file:// URL and safe under a strict content-security policy.

Palette and mark specs follow the project's data-viz rules: thin marks, solid
hairline grid, a legend whenever there is more than one series, selective direct
labels rather than a number on every point, and a table view beside every chart.
"""

from __future__ import annotations

import html
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

__all__ = ["Series", "line_chart", "hbar_chart", "sparkline", "meter", "fmt"]


@dataclass
class Series:
    """One line on a chart."""

    name: str
    values: pd.Series
    color_role: str = "series-1"
    width: float = 2.0
    #: Draw the last value as a direct label at the line's end.
    label_end: bool = True
    dashed: bool = False


def fmt(v, digits: int = 2, suffix: str = "") -> str:
    """Format a number for display, tolerating None/NaN."""
    if v is None:
        return "—"
    try:
        f = float(v)
    except (TypeError, ValueError):
        return html.escape(str(v))
    if np.isnan(f) or np.isinf(f):
        return "—"
    if f == 0.0:
        f = 0.0  # collapse negative zero, so a rounded-away value never reads "-0.0"
    return f"{f:,.{digits}f}{suffix}"


def _nice_ticks(lo: float, hi: float, count: int = 5) -> list[float]:
    """Pick round-ish axis ticks spanning [lo, hi]."""
    if not np.isfinite(lo) or not np.isfinite(hi) or hi <= lo:
        return [lo]
    raw = (hi - lo) / max(1, count)
    mag = 10 ** np.floor(np.log10(raw))
    for mult in (1, 2, 2.5, 5, 10):
        step = mult * mag
        if raw <= step:
            break
    start = np.ceil(lo / step) * step
    ticks, t = [], start
    while t <= hi + 1e-9:
        ticks.append(round(float(t), 10))
        t += step
    return ticks


def line_chart(
    series: list[Series],
    height: int = 260,
    width: int = 920,
    y_label: str = "",
    y_suffix: str = "",
    chart_id: str = "chart",
    show_legend: bool = True,
    y_digits: int = 0,
    bands: list[tuple[float, float, str]] | None = None,
) -> str:
    """Multi-series time-series line chart with a hover crosshair.

    All series share one y-axis — deliberately. A second y-scale would let the
    chart invent a correlation the data does not contain, so anything on a
    different scale belongs in its own chart or must be indexed to a common base
    before it gets here.
    """
    series = [s for s in series if s.values.notna().any()]
    if not series:
        return '<div class="chart-empty">no data</div>'

    # Right padding has to fit the longest end label, or the series name gets
    # clipped by the viewBox. Approximate the 11px label at ~6.1px per character.
    label_chars = max(
        (len(s.name) + 3 + y_digits + len(y_suffix) + 6 for s in series if s.label_end),
        default=0,
    )
    pad_l, pad_t, pad_b = 54, 14, 26
    pad_r = int(min(max(24, label_chars * 6.1 + 14), width * 0.36))
    plot_w = width - pad_l - pad_r
    plot_h = height - pad_t - pad_b

    index = series[0].values.index
    for s in series[1:]:
        index = index.union(s.values.index)
    index = index.sort_values()

    all_vals = pd.concat([s.values.reindex(index) for s in series], axis=1)
    lo = float(np.nanmin(all_vals.to_numpy(dtype=float)))
    hi = float(np.nanmax(all_vals.to_numpy(dtype=float)))
    if hi == lo:
        hi, lo = hi + 1, lo - 1
    span = hi - lo
    lo -= span * 0.06
    hi += span * 0.06

    n = len(index)
    x_of = lambda i: pad_l + (plot_w * i / max(1, n - 1))
    y_of = lambda v: pad_t + plot_h * (1.0 - (v - lo) / (hi - lo))

    parts: list[str] = [
        f'<svg viewBox="0 0 {width} {height}" class="viz" role="img" '
        f'aria-label="{html.escape(y_label or "chart")}" preserveAspectRatio="xMidYMid meet">'
    ]

    # shaded reference bands (drawn first, under everything)
    for b_lo, b_hi, role in bands or []:
        y1, y2 = y_of(min(b_hi, hi)), y_of(max(b_lo, lo))
        parts.append(
            f'<rect x="{pad_l}" y="{y1:.1f}" width="{plot_w}" height="{max(0, y2 - y1):.1f}" '
            f'fill="var(--{role})" opacity="0.10"/>'
        )

    # y grid — solid hairlines, one shade off the surface
    for t in _nice_ticks(lo, hi):
        y = y_of(t)
        if not (pad_t - 1 <= y <= pad_t + plot_h + 1):
            continue
        parts.append(
            f'<line x1="{pad_l}" y1="{y:.1f}" x2="{pad_l + plot_w}" y2="{y:.1f}" '
            f'stroke="var(--grid)" stroke-width="1"/>'
        )
        parts.append(
            f'<text x="{pad_l - 8}" y="{y + 3.5:.1f}" class="axis" text-anchor="end">'
            f"{fmt(t, y_digits)}{y_suffix}</text>"
        )

    # x labels — a handful of dates, never one per point
    step = max(1, n // 6)
    for i in range(0, n, step):
        parts.append(
            f'<text x="{x_of(i):.1f}" y="{height - 8}" class="axis" text-anchor="middle">'
            f"{index[i].strftime('%b %y')}</text>"
        )

    parts.append(
        f'<line x1="{pad_l}" y1="{pad_t + plot_h}" x2="{pad_l + plot_w}" y2="{pad_t + plot_h}" '
        f'stroke="var(--baseline)" stroke-width="1"/>'
    )

    # series paths
    for s in series:
        vals = s.values.reindex(index)
        d, pen_down = [], False
        for i, v in enumerate(vals.to_numpy(dtype=float)):
            if np.isnan(v):
                pen_down = False
                continue
            cmd = "L" if pen_down else "M"
            d.append(f"{cmd}{x_of(i):.1f},{y_of(v):.1f}")
            pen_down = True
        dash = ' stroke-dasharray="5 4"' if s.dashed else ""
        parts.append(
            f'<path d="{"".join(d)}" fill="none" stroke="var(--{s.color_role})" '
            f'stroke-width="{s.width}" stroke-linejoin="round" stroke-linecap="round"{dash}/>'
        )

        if s.label_end and vals.notna().any():
            last_i = int(np.where(vals.notna().to_numpy())[0][-1])
            last_v = float(vals.iloc[last_i])
            ly = min(max(y_of(last_v), pad_t + 8), pad_t + plot_h - 2)
            parts.append(
                f'<circle cx="{x_of(last_i):.1f}" cy="{y_of(last_v):.1f}" r="3.5" '
                f'fill="var(--{s.color_role})" stroke="var(--surface-1)" stroke-width="2"/>'
            )
            parts.append(
                f'<text x="{pad_l + plot_w + 8}" y="{ly + 3.5:.1f}" class="endlabel" '
                f'fill="var(--{s.color_role})">{html.escape(s.name)} {fmt(last_v, y_digits)}{y_suffix}</text>'
            )

    # hover layer: crosshair + tooltip, driven by the inline script
    payload = []
    for i, ts in enumerate(index):
        row = {"x": round(x_of(i), 1), "d": ts.strftime("%Y-%m-%d"), "v": []}
        for s in series:
            v = s.values.reindex(index).iloc[i]
            row["v"].append(None if pd.isna(v) else round(float(v), 4))
        payload.append(row)

    import json as _json

    parts.append(
        f'<line class="crosshair" id="{chart_id}-cross" x1="0" y1="{pad_t}" x2="0" y2="{pad_t + plot_h}" '
        f'stroke="var(--muted)" stroke-width="1" opacity="0"/>'
    )
    parts.append(
        f'<rect id="{chart_id}-hit" x="{pad_l}" y="{pad_t}" width="{plot_w}" height="{plot_h}" '
        f'fill="transparent" style="cursor:crosshair"/>'
    )
    parts.append("</svg>")

    legend = ""
    if show_legend and len(series) > 1:
        chips = "".join(
            f'<span class="chip"><i style="background:var(--{s.color_role})"></i>{html.escape(s.name)}</span>'
            for s in series
        )
        legend = f'<div class="legend">{chips}</div>'

    names = _json.dumps([s.name for s in series])
    data = _json.dumps(payload, separators=(",", ":"))
    suffix_js = _json.dumps(y_suffix)

    return (
        f'{legend}<div class="chart-wrap" data-chart="{chart_id}" '
        f"data-series='{names}' data-suffix='{suffix_js}' data-digits='{y_digits}' "
        f"data-points='{data}'>"
        f'{"".join(parts)}<div class="tooltip" id="{chart_id}-tip" hidden></div></div>'
    )


def hbar_chart(
    labels: list[str],
    values: list[float],
    width: int = 460,
    bar_h: int = 18,
    gap: int = 8,
    suffix: str = "%",
    color_role: str = "series-1",
    digits: int = 1,
) -> str:
    """Horizontal bars — one series, one color.

    A value-ramp here would double-encode length as hue and burn the only free
    channel on information the bar length already carries, so every bar shares
    slot 1.
    """
    if not labels:
        return '<div class="chart-empty">no positions</div>'

    pad_l, pad_r = 92, 66
    plot_w = width - pad_l - pad_r
    height = len(labels) * (bar_h + gap) + gap
    vmax = max([abs(v) for v in values] + [1e-9])

    parts = [
        f'<svg viewBox="0 0 {width} {height}" class="viz" role="img" '
        f'aria-label="position weights" preserveAspectRatio="xMidYMid meet">'
    ]
    for i, (lab, val) in enumerate(zip(labels, values)):
        y = gap + i * (bar_h + gap)
        w = max(1.0, plot_w * abs(val) / vmax)
        parts.append(
            f'<text x="{pad_l - 10}" y="{y + bar_h * 0.72:.1f}" class="axis" text-anchor="end">'
            f"{html.escape(lab)}</text>"
        )
        # 4px rounded data-end, anchored to the baseline
        parts.append(
            f'<rect x="{pad_l}" y="{y}" width="{w:.1f}" height="{bar_h}" rx="4" '
            f'fill="var(--{color_role})"><title>{html.escape(lab)}: {fmt(val, digits)}{suffix}</title></rect>'
        )
        parts.append(
            f'<text x="{pad_l + w + 8:.1f}" y="{y + bar_h * 0.72:.1f}" class="barval">'
            f"{fmt(val, digits)}{suffix}</text>"
        )
    parts.append("</svg>")
    return "".join(parts)


def sparkline(values: pd.Series, width: int = 132, height: int = 30, color_role: str = "series-1") -> str:
    """A bare trend line for a stat tile — no axes, no labels."""
    v = values.dropna().to_numpy(dtype=float)
    if len(v) < 2:
        return ""
    lo, hi = float(v.min()), float(v.max())
    if hi == lo:
        hi, lo = hi + 1, lo - 1
    pts = [
        f"{2 + (width - 4) * i / (len(v) - 1):.1f},{2 + (height - 4) * (1 - (val - lo) / (hi - lo)):.1f}"
        for i, val in enumerate(v)
    ]
    return (
        f'<svg viewBox="0 0 {width} {height}" class="spark" aria-hidden="true">'
        f'<polyline points="{" ".join(pts)}" fill="none" stroke="var(--{color_role})" '
        f'stroke-width="1.5" stroke-linejoin="round" stroke-linecap="round"/></svg>'
    )


def meter(value: float, label: str, status: str = "series-1") -> str:
    """A 0-1 horizontal meter used for regime and score components."""
    pct = max(0.0, min(1.0, float(value) if value is not None and not pd.isna(value) else 0.0))
    return (
        f'<div class="meter-row"><span class="meter-label">{html.escape(label)}</span>'
        f'<span class="meter-track"><span class="meter-fill" style="width:{pct * 100:.1f}%;'
        f'background:var(--{status})"></span></span>'
        f'<span class="meter-val">{pct * 100:.0f}</span></div>'
    )
