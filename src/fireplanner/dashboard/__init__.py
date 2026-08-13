"""Dashboard: assemble a payload, render a self-contained HTML page."""

from .build import DashboardPayload, build_payload
from .render import render_html

__all__ = ["DashboardPayload", "build_payload", "render_html"]
