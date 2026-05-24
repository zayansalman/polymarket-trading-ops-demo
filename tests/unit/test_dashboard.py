"""Unit tests for the FastAPI dashboard.

Covers:
- FastAPI app creation and basic routes
- GET / returns HTML with correct title and structure
- /api/data returns JSON with expected keys
- /api/start and /api/stop endpoints respond correctly
- Static CSS file is served
- SSE endpoint is accessible
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


# ---------------------------------------------------------------------------
# App Creation
# ---------------------------------------------------------------------------


class TestAppCreation:
    def test_app_has_title(self):
        assert app.title == "BTC 5m Binary Fair Value"

    def test_app_has_routes(self):
        routes = {r.path for r in app.routes}
        assert "/" in routes
        assert "/api/start" in routes
        assert "/api/stop" in routes
        assert "/api/data" in routes
        assert "/api/stream" in routes
        assert "/static" in routes


# ---------------------------------------------------------------------------
# GET /
# ---------------------------------------------------------------------------


class TestDashboardPage:
    def test_get_root_returns_html(self, client: TestClient):
        response = client.get("/")
        assert response.status_code == 200
        assert "text/html" in response.headers["content-type"]

    def test_html_contains_title(self, client: TestClient):
        response = client.get("/")
        assert "BTC 5m Binary Fair Value" in response.text

    def test_html_contains_hero(self, client: TestClient):
        response = client.get("/")
        assert "hero" in response.text
        assert "Local paper-trading dashboard" in response.text

    def test_html_contains_all_tabs(self, client: TestClient):
        response = client.get("/")
        text = response.text
        assert "Overview" in text
        assert "BTC 5m" in text
        assert "Activity" in text
        assert "Backtest" in text
        assert "Settings" in text

    def test_html_contains_tab_content_divs(self, client: TestClient):
        response = client.get("/")
        text = response.text
        assert 'id="tab-overview"' in text
        assert 'id="tab-btc5m"' in text
        assert 'id="tab-activity"' in text
        assert 'id="tab-backtest"' in text
        assert 'id="tab-settings"' in text

    def test_html_links_css(self, client: TestClient):
        response = client.get("/")
        assert '/static/style.css' in response.text

    def test_html_links_js(self, client: TestClient):
        response = client.get("/")
        assert '/static/dashboard.js' in response.text

    def test_html_contains_buttons(self, client: TestClient):
        response = client.get("/")
        text = response.text
        assert "Start BTC Paper Bot" in text
        assert "Stop" in text
        assert "Refresh" in text
        assert "Refresh backtest report" in text

    def test_overview_has_metric_cards(self, client: TestClient):
        response = client.get("/")
        text = response.text
        assert "Bot state" in text
        assert "Risk state" in text

    def test_settings_has_paper_rules(self, client: TestClient):
        response = client.get("/")
        assert "BTC 5m Paper Rules" in response.text

    def test_brief_section_present(self, client: TestClient):
        response = client.get("/")
        assert "System Brief" in response.text

    def test_scorecard_section_present(self, client: TestClient):
        response = client.get("/")
        assert "Trading Systems Scorecard" in response.text


# ---------------------------------------------------------------------------
# Static Files
# ---------------------------------------------------------------------------


class TestStaticFiles:
    def test_css_served(self, client: TestClient):
        response = client.get("/static/style.css")
        assert response.status_code == 200
        assert "text/css" in response.headers["content-type"]

    def test_css_has_root_variables(self, client: TestClient):
        response = client.get("/static/style.css")
        assert "--ink:" in response.text
        assert "--paper:" in response.text
        assert "--green:" in response.text
        assert "--red:" in response.text

    def test_css_has_component_classes(self, client: TestClient):
        response = client.get("/static/style.css")
        text = response.text
        assert ".hero {" in text
        assert ".metric {" in text
        assert ".grid {" in text
        assert ".badge {" in text
        assert ".panel {" in text
        assert ".positions {" in text
        assert ".position-card {" in text

    def test_js_served(self, client: TestClient):
        response = client.get("/static/dashboard.js")
        assert response.status_code == 200
        assert "javascript" in response.headers["content-type"]

    def test_js_has_tab_switching(self, client: TestClient):
        response = client.get("/static/dashboard.js")
        assert "showTab" in response.text

    def test_js_has_sse_connection(self, client: TestClient):
        response = client.get("/static/dashboard.js")
        assert "EventSource" in response.text

    def test_js_has_button_handlers(self, client: TestClient):
        response = client.get("/static/dashboard.js")
        text = response.text
        assert "handleStart" in text
        assert "handleStop" in text
        assert "handleRefresh" in text


# ---------------------------------------------------------------------------
# API Endpoints
# ---------------------------------------------------------------------------


class TestApiData:
    def test_api_data_returns_json(self, client: TestClient):
        response = client.get("/api/data")
        assert response.status_code == 200
        assert response.headers["content-type"] == "application/json"

    def test_api_data_has_expected_keys(self, client: TestClient):
        response = client.get("/api/data")
        data = response.json()
        assert "overview" in data
        assert "paper" in data
        assert "activity" in data
        assert "history" in data
        assert "backtest" in data

    def test_api_data_overview_has_html(self, client: TestClient):
        response = client.get("/api/data")
        overview = response.json()["overview"]
        assert "html" in overview
        assert "status" in overview
        # Should contain rendered HTML
        assert "metric" in overview["html"] or "Bot state" in overview["html"]


class TestApiStart:
    def test_start_returns_json(self, client: TestClient):
        response = client.post("/api/start")
        assert response.status_code == 200
        assert response.headers["content-type"] == "application/json"

    def test_start_has_status_and_detail(self, client: TestClient):
        response = client.post("/api/start")
        data = response.json()
        assert "status" in data
        assert "detail" in data


class TestApiStop:
    def test_stop_returns_json(self, client: TestClient):
        response = client.post("/api/stop")
        assert response.status_code == 200
        assert response.headers["content-type"] == "application/json"

    def test_stop_has_status_and_detail(self, client: TestClient):
        response = client.post("/api/stop")
        data = response.json()
        assert "status" in data
        assert "detail" in data


# ---------------------------------------------------------------------------
# SSE Endpoint
# ---------------------------------------------------------------------------


class TestApiStream:
    """SSE endpoint tests.

    Note: We verify the route exists and has correct configuration,
    but do not test actual streaming since the endpoint runs an
    infinite loop that would block the TestClient.
    """

    def test_stream_route_exists(self):
        routes = {r.path for r in app.routes}
        assert "/api/stream" in routes

    def test_stream_is_get_method(self):
        for route in app.routes:
            if getattr(route, "path", None) == "/api/stream":
                assert "GET" in route.methods
                return
        pytest.fail("/api/stream route not found")
