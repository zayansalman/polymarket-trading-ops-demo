"""FastAPI dashboard for BTC 5m Binary Fair Value trading system.

Replaces the 150MB+ Gradio dashboard with a lightweight FastAPI + Jinja2
implementation. All visual design is preserved via extracted CSS.

Endpoints:
    GET  /              — Main dashboard page (HTML)
    POST /api/start     — Start the paper bot
    POST /api/stop      — Stop the paper bot
    GET  /api/data      — Full dashboard data as JSON
    GET  /api/stream    — Server-Sent Events for live updates
"""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import UTC, datetime
from html import escape
from pathlib import Path
from typing import Any

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

# ---------------------------------------------------------------------------
# Ensure project root is on sys.path so top-level modules (config, db, …)
# can be imported when this package is run directly.
# ---------------------------------------------------------------------------
_PROJECT_ROOT = Path(__file__).resolve().parents[4]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from config import (  # type: ignore[import-untyped]
    BTC_CHAINLINK_STREAM_URL,
    BTC_HISTORY_CSV_PATH,
    BTC_PAPER_ENTRY_EDGE_MIN,
    BTC_PAPER_MAX_TRADE_USD,
    BTC_PAPER_MIN_CONFIDENCE,
    BTC_PAPER_MIN_TRADE_USD,
    BTC_PAPER_STOP_RETURN,
    BTC_PAPER_TARGET_RETURN,
    BTC_PAPER_TICK_SECONDS,
    BTC_PAPER_TIME_EXIT_SECONDS,
    DASHBOARD_SERVER_NAME,
    DASHBOARD_SERVER_PORT,
    DATA_DIR,
    DB_PATH,
)
from db import connect, init_db  # type: ignore[import-untyped]
from logging_setup import get_logger  # type: ignore[import-untyped]

log = get_logger("dashboard")

# Lazily import btc_bot modules (may not be available in test environments)
try:
    from btc_bot.controller import get_status, request_start, request_stop  # type: ignore[import-untyped]
    from btc_bot.history import load_btc_history_stats  # type: ignore[import-untyped]
    from btc_bot.paper import load_paper_summary  # type: ignore[import-untyped]
    from btc_bot.backtest import format_report  # type: ignore[import-untyped]

    _BTC_BOT_AVAILABLE = True
except Exception:
    _BTC_BOT_AVAILABLE = False
    log.warning("btc_bot modules not available; dashboard running in mock mode")

# ---------------------------------------------------------------------------
# Lifespan — init DB tables on startup
# ---------------------------------------------------------------------------

@asynccontextmanager
async def _lifespan(app: FastAPI):
    await init_db()
    yield


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

dashboard_dir = Path(__file__).parent

app = FastAPI(title="BTC 5m Binary Fair Value", lifespan=_lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount("/static", StaticFiles(directory=str(dashboard_dir / "static")), name="static")

templates = Jinja2Templates(directory=str(dashboard_dir / "templates"))

# ---------------------------------------------------------------------------
# Formatting helpers (ported from original dashboard.py)
# ---------------------------------------------------------------------------


def _money(value: float | None, signed: bool = False) -> str:
    if value is None:
        return "n/a"
    prefix = "+" if signed and value > 0 else ""
    return f"{prefix}${value:,.2f}"


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.1%}"


def _fmt_relative(ts: str | None) -> str:
    if not ts:
        return "never"
    try:
        parsed = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
    except ValueError:
        return ts
    age = max(0, int((datetime.now(UTC) - parsed).total_seconds()))
    if age < 60:
        return f"{age}s ago"
    if age < 3600:
        return f"{age // 60}m ago"
    return f"{age // 3600}h ago"


def _kpi_card(label: str, value: str, hint: str = "") -> str:
    return (
        "<div class='metric'>"
        f"<div class='label'>{escape(label)}</div>"
        f"<div class='value'>{escape(value)}</div>"
        f"<div class='hint'>{escape(hint)}</div>"
        "</div>"
    )


def _state_badge(state: str, risk_state: str) -> str:
    state_l = (state or "").lower()
    risk_l = (risk_state or "").lower()
    if state_l == "running" and risk_l.startswith("ok"):
        cls = "ok"
    elif state_l == "running":
        cls = "warn"
    elif "breach" in risk_l or "stale" in risk_l:
        cls = "stop"
    else:
        cls = "warn"
    return f"<span class='badge {cls}'>{escape(state.upper())}</span>"


def _pnl_class(value: float | None) -> str:
    if value is None or value == 0:
        return ""
    return "positive" if value > 0 else "negative"


# ---------------------------------------------------------------------------
# Async data loaders
# ---------------------------------------------------------------------------


async def _load_feed(limit: int = 18) -> list[dict[str, Any]]:
    async with connect() as db:
        async with db.execute(
            """
            SELECT created_at, event_type, message, details_json
            FROM notification_feed
            WHERE event_type = 'system_start' OR event_type LIKE 'btc_%'
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (limit,),
        ) as cur:
            return [dict(row) for row in await cur.fetchall()]


async def _get_status_safe() -> Any:
    """Get controller status, returning a mock if btc_bot is unavailable."""
    if _BTC_BOT_AVAILABLE:
        return await get_status()
    # Mock status for testing / when btc_bot is not available
    class _MockStatus:
        state = "stopped"
        mode = "paper"
        updated_at = None
        detail = "BTC 5-minute paper mode is ready. No live orders are placed by this build."
    return _MockStatus()


async def _get_paper_safe() -> Any:
    """Get paper summary, returning a mock if btc_bot is unavailable."""
    if _BTC_BOT_AVAILABLE:
        return await load_paper_summary()
    # Mock paper summary for testing
    class _MockPaper:
        risk_state = "IDLE: no ticks yet"
        open_positions = 0
        closed_positions = 0
        total_pnl_usd = 0.0
        open_exposure_usd = 0.0
        closed_notional_usd = 0.0
        win_rate = None
        avg_pnl_usd = None
        avg_hold_seconds = None
        last_signal = "none"
        last_tick_at = None
        last_window_slug = None
        last_spot_price = None
        last_fair_up_prob = None
        last_up_price = None
        last_edge = None
        last_feed_source = None
        recent_positions: list[dict[str, Any]] = []
    return _MockPaper()


# ---------------------------------------------------------------------------
# HTML generators (ported from original dashboard.py)
# ---------------------------------------------------------------------------


def _position_cards(positions: list[dict[str, Any]]) -> str:
    if not positions:
        return "<div class='note'>No paper positions yet. Start the bot to let it observe a BTC 5m window.</div>"
    cards: list[str] = []
    for pos in positions:
        pnl = pos.get("realized_pnl_usd")
        pnl_text = "open" if pnl is None else _money(float(pnl), signed=True)
        pnl_cls = _pnl_class(float(pnl)) if pnl is not None else ""
        closed = _fmt_relative(pos.get("closed_at")) if pos.get("closed_at") else "open"
        cards.append(
            "<div class='position-card'>"
            f"<b>{escape(pos['side'])}</b> "
            f"<span class='mono'>{escape(pos['window_slug'])}</span><br>"
            f"State: {escape(pos['state'])} | Opened: {_fmt_relative(pos.get('opened_at'))} | Closed: {closed}<br>"
            f"Entry: {float(pos['entry_price']):.3f} | Exit: {pos.get('exit_price') or 'n/a'} | "
            f"Notional: {_money(float(pos['notional_usd']))}<br>"
            f"PnL: <span class='{pnl_cls}'>{pnl_text}</span> | Reason: {escape(str(pos.get('exit_reason') or 'holding'))}"
            "</div>"
        )
    return "<div class='positions'>" + "\n".join(cards) + "</div>"


async def _overview_html() -> str:
    status = await _get_status_safe()
    paper = await _get_paper_safe()
    badge = _state_badge(status.state, paper.risk_state)
    pnl_cls = _pnl_class(paper.total_pnl_usd)
    return (
        "<div class='grid'>"
        f"{_kpi_card('Bot state', status.state.upper(), f'mode: {status.mode}')}"
        f"{_kpi_card('Risk state', paper.risk_state, _fmt_relative(paper.last_tick_at))}"
        f"{_kpi_card('Open exposure', _money(paper.open_exposure_usd), f'{paper.open_positions} open position(s)')}"
        f"{_kpi_card('Closed trades', str(paper.closed_positions), f'win rate {_pct(paper.win_rate)}')}"
        f"<div class='metric'><div class='label'>Paper PnL</div>"
        f"<div class='value {pnl_cls}'>{_money(paper.total_pnl_usd, signed=True)}</div>"
        f"<div class='hint'>closed notional {_money(paper.closed_notional_usd)}</div></div>"
        f"{_kpi_card('Last signal', paper.last_signal[:90], paper.last_window_slug or 'no market tick yet')}"
        "</div>"
        "<div class='panel'>"
        f"{badge} <span class='mono'>DB {escape(str(DB_PATH))}</span>"
        f"<p class='status-detail'>{escape(status.detail)}</p>"
        "</div>"
    )


async def _status_markdown() -> str:
    status = await _get_status_safe()
    paper = await _get_paper_safe()
    return (
        "<h3>Controller</h3>\n"
        "<ul>\n"
        f"<li>State: <strong>{status.state.upper()}</strong></li>\n"
        f"<li>Mode: <strong>{status.mode}</strong></li>\n"
        f"<li>Updated: <code>{status.updated_at or 'not yet'}</code></li>\n"
        f"<li>Risk: <strong>{paper.risk_state}</strong></li>\n"
        "</ul>\n"
        f"<p>{escape(status.detail)}</p>\n"
        "<p><em>Start runs the paper loop only. Stop prevents new entries immediately "
        "and force-closes any open simulated position.</em></p>"
    )


async def _paper_html() -> str:
    paper = await _get_paper_safe()
    avg_hold = "n/a" if paper.avg_hold_seconds is None else f"{paper.avg_hold_seconds:.0f}s"
    last_edge = "n/a" if paper.last_edge is None else f"{paper.last_edge:+.3f}"
    last_fair = "n/a" if paper.last_fair_up_prob is None else f"{paper.last_fair_up_prob:.1%}"
    last_up = "n/a" if paper.last_up_price is None else f"{paper.last_up_price:.3f}"
    return (
        "<div class='grid'>"
        f"{_kpi_card('Last tick', _fmt_relative(paper.last_tick_at), paper.last_feed_source or 'no feed yet')}"
        f"{_kpi_card('Spot', 'n/a' if paper.last_spot_price is None else f'${paper.last_spot_price:,.2f}', paper.last_window_slug or 'no window yet')}"
        f"{_kpi_card('Fair Up', last_fair, f'market up {last_up}')}"
        f"{_kpi_card('Edge', last_edge, f'min edge {BTC_PAPER_ENTRY_EDGE_MIN:.3f}')}"
        f"{_kpi_card('Avg PnL', _money(paper.avg_pnl_usd, signed=True), f'avg hold {avg_hold}')}"
        f"{_kpi_card('Sizing', f'${BTC_PAPER_MIN_TRADE_USD:.0f}-${BTC_PAPER_MAX_TRADE_USD:.0f}', f'min confidence {BTC_PAPER_MIN_CONFIDENCE:.0%}')}"
        "</div>"
        "<div class='panel'><h3>Recent Paper Positions</h3>"
        f"{_position_cards(paper.recent_positions)}"
        "</div>"
    )


async def _activity_html() -> str:
    try:
        rows = await _load_feed()
    except Exception:
        rows = []
    if not rows:
        return "<p><em>No BTC bot activity yet. Press Start to begin paper trading.</em></p>"
    lines = ["<h3>BTC Activity Feed</h3>", '<ul class="feed-list">']
    for row in rows:
        stamp = _fmt_relative(row["created_at"])
        event = row["event_type"].replace("_", " ")
        lines.append(f"<li><code>{escape(stamp)}</code> <strong>{escape(event)}</strong> — {escape(row['message'])}</li>")
    lines.append("</ul>")
    return "\n".join(lines)


def _history_html() -> str:
    if _BTC_BOT_AVAILABLE:
        stats = load_btc_history_stats()
    else:
        class _MockStats:
            found = False
            path = str(BTC_HISTORY_CSV_PATH)
        stats = _MockStats()

    if not stats.found:
        return (
            "<h3>Historical Trade Baseline</h3>\n"
            f"<p>Optional CSV not found at <code>{BTC_HISTORY_CSV_PATH}</code>. The bot still runs; "
            "the CSV only helps explain why the lab sizes paper trades at $1-$5.</p>"
        )
    return (
        "<h3>Historical Trade Baseline</h3>\n"
        "<ul>\n"
        f"<li>Source: <code>{stats.path}</code></li>\n"
        f"<li>BTC rows: <strong>{stats.btc_rows}</strong> of {stats.total_rows}</li>\n"
        f"<li>Buys / sells / redeems: <strong>{stats.buys} / {stats.sells} / {stats.redeems}</strong></li>\n"
        f"<li>Average buy size: <strong>${stats.buy_usdc_avg:.2f}</strong></li>\n"
        f"<li>Median buy size: <strong>${stats.buy_usdc_median:.2f}</strong></li>\n"
        f"<li>Share of buys sized $1-$5: <strong>{stats.one_to_five_buy_share:.0%}</strong></li>\n"
        "</ul>"
    )


def _brief_html() -> str:
    return (
        "<h3>System Brief</h3>\n"
        "<p>This is a local BTC 5-minute binary fair-value strategy lab. It is useful "
        "as a personal paper bot and as a compact example of trading-system "
        "discipline: market discovery, feed labeling, confidence-based sizing, "
        "one-position risk control, structured event logs, and a dashboard kill "
        "switch.</p>\n"
        "<p>This build does not sign or submit live orders. The active workflow is simple: "
        "<strong>Start</strong> begins simulated BTC 5m trading, and <strong>Stop</strong> halts new entries "
        "and closes any open simulated position.</p>"
    )


def _scorecard_html() -> str:
    return (
        "<h3>Trading Systems Scorecard</h3>\n"
        "<ul>\n"
        "<li>Scope: BTC 5-minute Up/Down markets only.</li>\n"
        "<li>Operator control: Start, Stop, Refresh, and activity feed.</li>\n"
        "<li>Risk: one open paper position, bounded $1-$5 sizing, target/stop/time exits.</li>\n"
        "<li>Feed discipline: public BTC fallback is labeled; Chainlink Streams is the intended reference.</li>\n"
        "<li>Auditability: ticks, simulated positions, exits, config state, and notifications persist to SQLite.</li>\n"
        "<li>Failure visibility: market/feed/loop errors surface in logs and dashboard state.</li>\n"
        "</ul>"
    )


def _settings_html() -> str:
    return (
        "<h3>BTC 5m Paper Rules</h3>\n"
        "<ul>\n"
        f"<li>Market scope: BTC Up/Down 5-minute windows only.</li>\n"
        f"<li>Paper sizing: <strong>${BTC_PAPER_MIN_TRADE_USD:.0f}-${BTC_PAPER_MAX_TRADE_USD:.0f}</strong> by confidence.</li>\n"
        f"<li>Tick cadence: <strong>{BTC_PAPER_TICK_SECONDS:.0f}s</strong>.</li>\n"
        f"<li>Minimum confidence: <strong>{BTC_PAPER_MIN_CONFIDENCE:.0%}</strong>.</li>\n"
        f"<li>Minimum edge: <strong>{BTC_PAPER_ENTRY_EDGE_MIN:.3f}</strong>.</li>\n"
        f"<li>Target / stop return: <strong>{BTC_PAPER_TARGET_RETURN:.0%} / {BTC_PAPER_STOP_RETURN:.0%}</strong>.</li>\n"
        f"<li>Time exit: <strong>{BTC_PAPER_TIME_EXIT_SECONDS}s</strong>.</li>\n"
        f"<li>Settlement-aware reference target: {BTC_CHAINLINK_STREAM_URL}</li>\n"
        "</ul>\n"
        "<p>Required local env vars are optional for paper mode except path overrides. "
        "No private key is used by this build.</p>"
    )


def _backtest_html() -> str:
    report_path = DATA_DIR / "backtests" / "latest.json"
    if not report_path.exists():
        return (
            "<h3>BTC 5m Binary Fair Value Backtest</h3>\n"
            "<p>No local report yet. Run:</p>\n"
            '<pre><code>./.venv/bin/python tools/backtest_btc_strategy.py</code></pre>'
        )
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
        if _BTC_BOT_AVAILABLE:
            return format_report(report)
        # Fallback rendering when btc_bot.backtest is unavailable
        baseline = report.get("baseline", {})
        current = report.get("current", {})
        best = report.get("best", {})
        lines = [
            "<h2>BTC 5m Binary Fair Value Backtest</h2>",
            "<ul>",
            f"<li>Opportunities: {report.get('opportunities', 'N/A')}</li>",
            f"<li>Method: {report.get('method', 'N/A')}</li>",
            "</ul>",
            "<h3>Results</h3>",
            "<ul>",
            f"<li>All historical buys: trades={baseline.get('trades', 'N/A')}, pnl=${baseline.get('total_pnl_usd', 0):+.2f}, roi={baseline.get('roi', 0):.1%}</li>",
            f"<li>Current defaults: trades={current.get('trades', 'N/A')}, pnl=${current.get('total_pnl_usd', 0):+.2f}, roi={current.get('roi', 0):.1%}</li>",
            f"<li>Optimized filter: trades={best.get('trades', 'N/A')}, pnl=${best.get('total_pnl_usd', 0):+.2f}, roi={best.get('roi', 0):.1%}</li>",
            "</ul>",
            "<h3>Optimized Parameters</h3>",
            "<ul>",
        ]
        for key, value in best.get("params", {}).items():
            lines.append(f"<li>{key}: {value}</li>")
        lines.append("</ul>")
        return "\n".join(lines)
    except Exception as e:
        return f"<h3>Backtest Error</h3><p>Failed to load report: {escape(str(e))}</p>"


# ---------------------------------------------------------------------------
# Aggregated data helpers
# ---------------------------------------------------------------------------


async def _get_overview_data() -> dict[str, str]:
    """Return overview data as a dict with 'html' and 'status' keys."""
    return {
        "html": await _overview_html(),
        "status": await _status_markdown(),
    }


async def _get_paper_data() -> dict[str, str]:
    return {"html": await _paper_html()}


async def _get_activity_data() -> str:
    return await _activity_html()


def _get_history_data() -> str:
    return _history_html()


def _get_backtest_data() -> str:
    return _backtest_html()


def _get_settings_data() -> str:
    return _settings_html()


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request) -> Any:
    """Main dashboard page."""
    overview = await _get_overview_data()
    status = overview["status"]
    paper = await _get_paper_data()
    activity = await _get_activity_data()
    history = _get_history_data()
    backtest = _get_backtest_data()
    settings = _get_settings_data()

    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "overview": overview,
            "status": status,
            "paper": paper,
            "activity": activity,
            "history": history,
            "backtest": backtest,
            "settings": settings,
            "brief": _brief_html(),
            "scorecard": _scorecard_html(),
        },
    )


@app.post("/api/start")
async def api_start() -> dict[str, str]:
    """Start the paper bot."""
    try:
        if _BTC_BOT_AVAILABLE:
            status = await request_start()
            return {"status": status.state, "detail": status.detail}
        return {"status": "mock_running", "detail": "Mock start — btc_bot not available"}
    except Exception as e:
        log.exception("btc.start_failed", error=str(e))
        return {"status": "error", "detail": f"Start failed: {e}"}


@app.post("/api/stop")
async def api_stop() -> dict[str, str]:
    """Stop the paper bot."""
    try:
        if _BTC_BOT_AVAILABLE:
            status = await request_stop()
            return {"status": status.state, "detail": status.detail}
        return {"status": "mock_stopped", "detail": "Mock stop — btc_bot not available"}
    except Exception as e:
        log.exception("btc.stop_failed", error=str(e))
        return {"status": "error", "detail": f"Stop failed: {e}"}


@app.get("/api/data")
async def api_data() -> dict[str, Any]:
    """Get current dashboard data as JSON."""
    return {
        "overview": await _get_overview_data(),
        "paper": await _get_paper_data(),
        "activity": await _get_activity_data(),
        "history": _get_history_data(),
        "backtest": _get_backtest_data(),
    }


@app.get("/api/stream")
async def api_stream(request: Request) -> StreamingResponse:
    """Server-Sent Events for real-time updates (5-second interval)."""
    async def event_generator():
        while True:
            if await request.is_disconnected():
                break
            try:
                data = {
                    "overview": await _get_overview_data(),
                    "paper": await _get_paper_data(),
                    "activity": await _get_activity_data(),
                    "history": _get_history_data(),
                    "backtest": _get_backtest_data(),
                }
                yield f"data: {json.dumps(data)}\n\n"
            except Exception as e:
                log.warning("sse_error", error=str(e))
                yield f"data: {json.dumps({'error': str(e)})}\n\n"
            await asyncio.sleep(5)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
        },
    )


# ---------------------------------------------------------------------------
# Launch helper
# ---------------------------------------------------------------------------

def launch() -> None:
    """Run the dashboard with uvicorn."""
    import uvicorn
    uvicorn.run(
        "btc_5m_fv.ops.dashboard.app:app",
        host=DASHBOARD_SERVER_NAME,
        port=DASHBOARD_SERVER_PORT,
        log_level="info",
    )
