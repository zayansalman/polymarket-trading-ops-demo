"""FastAPI dashboard for BTC 5m Binary Fair Value trading system.

Replaces the Gradio dashboard with a lightweight FastAPI + Jinja2
implementation. Eliminates the 150MB+ Gradio dependency while
preserving the exact same visual design.
"""

from __future__ import annotations

from btc_5m_fv.ops.dashboard.app import app, launch

__all__ = ["app", "launch"]
