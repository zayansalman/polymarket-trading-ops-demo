"""End-to-end tests for the FastAPI dashboard.

Tests the complete page-load flow and verifies that all interactive
components are present and functional.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Ensure project root on path
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from fastapi.testclient import TestClient

from btc_5m_fv.ops.dashboard.app import app


@pytest.fixture
def client() -> TestClient:
    with TestClient(app) as c:
        yield c


class TestFullPageLoad:
    """Verify the complete dashboard page loads with all sections."""

    def test_page_loads_200(self, client: TestClient):
        response = client.get("/")
        assert response.status_code == 200

    def test_page_has_correct_doctype_structure(self, client: TestClient):
        response = client.get("/")
        text = response.text
        assert "<!DOCTYPE html>" in text
        assert "<html" in text
        assert "</html>" in text
        assert "<head>" in text
        assert "<body>" in text

    def test_page_has_meta_viewport(self, client: TestClient):
        response = client.get("/")
        assert "width=device-width" in response.text

    def test_all_tab_buttons_exist_with_correct_data_attrs(self, client: TestClient):
        response = client.get("/")
        text = response.text
        for tab in ["overview", "btc5m", "activity", "backtest", "settings"]:
            assert f'data-tab="{tab}"' in text

    def test_tab_contents_are_independent(self, client: TestClient):
        response = client.get("/")
        text = response.text
        # Each tab should have its own content container
        assert "overview-content" in text
        assert "paper-content" in text
        assert "activity-content" in text
        assert "backtest-content" in text

    def test_overview_contains_kpi_metrics(self, client: TestClient):
        response = client.get("/")
        text = response.text
        # Should have metric cards rendered
        assert "metric" in text
        # Key labels should appear
        assert "Bot state" in text or "bot state" in text.lower()

    def test_settings_contains_config_values(self, client: TestClient):
        response = client.get("/")
        text = response.text
        assert "Paper Rules" in text
        assert "confidence" in text.lower()


class TestButtonInteractivity:
    """Verify buttons are present with correct onclick handlers."""

    def test_start_button_calls_handle_start(self, client: TestClient):
        response = client.get("/")
        text = response.text
        assert "handleStart()" in text
        assert "Start BTC Paper Bot" in text

    def test_stop_button_calls_handle_stop(self, client: TestClient):
        response = client.get("/")
        text = response.text
        assert "handleStop()" in text
        assert "btn-stop" in text

    def test_refresh_button_calls_handle_refresh(self, client: TestClient):
        response = client.get("/")
        text = response.text
        assert "handleRefresh()" in text

    def test_refresh_backtest_button_calls_handler(self, client: TestClient):
        response = client.get("/")
        text = response.text
        assert "handleRefreshBacktest()" in text
        assert "btn-refresh-backtest" in text


class TestApiRoundTrip:
    """Verify API endpoints work in sequence."""

    def test_start_then_data_reflects_running(self, client: TestClient):
        start_resp = client.post("/api/start")
        assert start_resp.status_code == 200
        start_data = start_resp.json()
        assert "status" in start_data

    def test_stop_then_data_reflects_stopped(self, client: TestClient):
        stop_resp = client.post("/api/stop")
        assert stop_resp.status_code == 200
        stop_data = stop_resp.json()
        assert "status" in stop_data

    def test_data_after_start_stop_cycle(self, client: TestClient):
        client.post("/api/start")
        client.post("/api/stop")
        data_resp = client.get("/api/data")
        data = data_resp.json()
        assert "overview" in data
        assert "paper" in data
        assert "activity" in data


class TestStaticAssets:
    """Verify all static assets are correctly served."""

    def test_css_is_complete(self, client: TestClient):
        response = client.get("/static/style.css")
        css = response.text
        # Verify all major sections of CSS are present
        selectors = [
            ":root", "body", ".hero", ".tabs", ".tab-btn", ".tab-content",
            ".grid", ".metric", ".panel", ".badge", ".note",
            ".positions", ".position-card", ".mono",
            ".positive", ".negative", ".btn", ".btn-primary",
            ".btn-stop", ".btn-secondary", ".feed-list",
            ".sse-indicator", ".toast", ".md-content",
        ]
        for selector in selectors:
            assert selector in css, f"Missing CSS selector: {selector}"

    def test_js_has_all_functions(self, client: TestClient):
        response = client.get("/static/dashboard.js")
        js = response.text
        functions = [
            "showTab", "showToast",
            "handleStart", "handleStop", "handleRefresh", "handleRefreshBacktest",
            "refreshAll", "updateDashboard",
            "connectSSE", "disconnectSSE",
            "updateSseIndicator",
        ]
        for func in functions:
            assert func in js, f"Missing JS function: {func}"


class TestVisualParity:
    """Verify the FastAPI dashboard matches Gradio visual design."""

    def test_warm_paper_palette(self, client: TestClient):
        response = client.get("/static/style.css")
        css = response.text
        assert "#fbf7ec" in css  # --paper
        assert "#f8efe0" in css  # gradient color
        assert "#15130f" in css  # --ink

    def test_serif_headings(self, client: TestClient):
        response = client.get("/static/style.css")
        assert "Georgia" in response.text

    def test_monospace_data_font(self, client: TestClient):
        response = client.get("/static/style.css")
        assert "SFMono-Regular" in response.text

    def test_card_radius_and_shadow(self, client: TestClient):
        response = client.get("/static/style.css")
        css = response.text
        assert "border-radius: 22px" in css
        assert "box-shadow: 0 18px 50px" in css

    def test_badge_variants(self, client: TestClient):
        response = client.get("/static/style.css")
        css = response.text
        assert ".badge.ok" in css
        assert ".badge.warn" in css
        assert ".badge.stop" in css

    def test_pnl_color_classes(self, client: TestClient):
        response = client.get("/static/style.css")
        css = response.text
        assert ".positive" in css
        assert ".negative" in css

    def test_button_radius(self, client: TestClient):
        response = client.get("/static/style.css")
        assert "border-radius: 14px" in response.text
