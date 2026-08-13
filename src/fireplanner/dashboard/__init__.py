"""Dashboard: assemble a payload, render either page."""

from .build import DashboardPayload, add_decision, build_payload
from .desk import render_desk
from .render import render_html

__all__ = ["DashboardPayload", "add_decision", "build_payload", "render_desk", "render_html"]
