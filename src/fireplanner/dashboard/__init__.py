"""Dashboard: assemble a payload, render either page — or both as one file."""

from .build import DashboardPayload, add_decision, build_payload
from .desk import render_desk
from .render import render_html
from .single import render_single

__all__ = [
    "DashboardPayload", "add_decision", "build_payload",
    "render_desk", "render_html", "render_single",
]
