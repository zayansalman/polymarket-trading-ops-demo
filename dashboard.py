"""Local Gradio dashboard for BTC 5-minute paper trading."""
from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from html import escape
from typing import Any

import gradio as gr

from btc_bot.controller import get_status, request_start, request_stop
from btc_bot.history import load_btc_history_stats
from btc_bot.paper import load_paper_summary
from config import (
    BTC_CHAINLINK_STREAM_URL,
    DATA_DIR,
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
    DB_PATH,
)
from db import connect
from logging_setup import get_logger
from btc_bot.backtest import format_report

log = get_logger("dashboard")


CSS = """
:root {
  --ink: #15130f;
  --muted: #756f63;
  --paper: #fbf7ec;
  --card: rgba(255, 252, 244, 0.92);
  --line: rgba(31, 27, 20, 0.12);
  --green: #0f7b4f;
  --red: #b43b2a;
  --amber: #b17214;
  --coal: #1d1b16;
}
.gradio-container {
  background:
    radial-gradient(circle at 18% 10%, rgba(255, 190, 87, 0.26), transparent 28rem),
    radial-gradient(circle at 92% 5%, rgba(46, 124, 93, 0.18), transparent 26rem),
    linear-gradient(140deg, #f8efe0 0%, #fffdf7 52%, #ece0c7 100%);
  color: var(--ink);
  font-family: "Avenir Next", "Segoe UI", sans-serif;
}
.hero, .panel, .metric, .note, .position-card {
  border: 1px solid var(--line);
  background: var(--card);
  border-radius: 22px;
  box-shadow: 0 18px 50px rgba(49, 39, 21, 0.08);
}
.hero {
  padding: 28px 30px;
  margin-bottom: 18px;
}
.hero h1 {
  margin: 0 0 8px;
  font-family: Georgia, "Times New Roman", serif;
  letter-spacing: -0.04em;
  font-size: clamp(2.1rem, 5vw, 4.8rem);
  line-height: 0.95;
}
.hero p {
  max-width: 900px;
  margin: 0;
  color: var(--muted);
  font-size: 1.05rem;
}
.grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(210px, 1fr));
  gap: 14px;
}
.metric {
  padding: 16px;
}
.metric .label {
  color: var(--muted);
  font-size: 0.78rem;
  letter-spacing: 0.08em;
  text-transform: uppercase;
}
.metric .value {
  font-family: Georgia, "Times New Roman", serif;
  font-size: 2rem;
  line-height: 1.1;
  margin-top: 8px;
}
.metric .hint {
  color: var(--muted);
  margin-top: 6px;
  font-size: 0.9rem;
}
.panel {
  padding: 18px;
  margin: 12px 0;
}
.badge {
  display: inline-flex;
  align-items: center;
  border-radius: 999px;
  padding: 6px 10px;
  font-size: 0.82rem;
  font-weight: 700;
  letter-spacing: 0.02em;
}
.badge.ok { background: rgba(15, 123, 79, 0.13); color: var(--green); }
.badge.warn { background: rgba(177, 114, 20, 0.16); color: var(--amber); }
.badge.stop { background: rgba(180, 59, 42, 0.13); color: var(--red); }
.note {
  padding: 14px 16px;
  color: var(--muted);
}
.positions {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(260px, 1fr));
  gap: 12px;
}
.position-card {
  padding: 14px;
}
.position-card b {
  color: var(--coal);
}
.mono {
  font-family: "SFMono-Regular", "Cascadia Code", monospace;
  font-size: 0.92em;
}
.positive { color: var(--green); }
.negative { color: var(--red); }
button.primary, button.secondary {
  border-radius: 14px !important;
}
"""


def _run(coro):
    return asyncio.run(coro)


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


def _activity_markdown() -> str:
    rows = _run(_load_feed())
    if not rows:
        return "_No BTC bot activity yet. Press Start to begin paper trading._"
    lines = ["### BTC Activity Feed"]
    for row in rows:
        stamp = _fmt_relative(row["created_at"])
        event = row["event_type"].replace("_", " ")
        lines.append(f"- `{stamp}` **{event}** - {row['message']}")
    return "\n".join(lines)


def _overview_html() -> str:
    status = _run(get_status())
    paper = _run(load_paper_summary())
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
        f"<p>{escape(status.detail)}</p>"
        "</div>"
    )


def _status_markdown() -> str:
    status = _run(get_status())
    paper = _run(load_paper_summary())
    return (
        "### Controller\n"
        f"- State: **{status.state.upper()}**\n"
        f"- Mode: **{status.mode}**\n"
        f"- Updated: `{status.updated_at or 'not yet'}`\n"
        f"- Risk: **{paper.risk_state}**\n"
        f"- Detail: {status.detail}\n"
        "\n"
        "Start runs the paper loop only. Stop prevents new entries immediately "
        "and force-closes any open simulated position."
    )


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


def _paper_html() -> str:
    paper = _run(load_paper_summary())
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


def _history_markdown() -> str:
    stats = load_btc_history_stats()
    if not stats.found:
        return (
            "### Historical Trade Baseline\n"
            f"Optional CSV not found at `{BTC_HISTORY_CSV_PATH}`. The bot still runs; "
            "the CSV only helps explain why the lab sizes paper trades at $1-$5."
        )
    return (
        "### Historical Trade Baseline\n"
        f"- Source: `{stats.path}`\n"
        f"- BTC rows: **{stats.btc_rows}** of {stats.total_rows}\n"
        f"- Buys / sells / redeems: **{stats.buys} / {stats.sells} / {stats.redeems}**\n"
        f"- Average buy size: **${stats.buy_usdc_avg:.2f}**\n"
        f"- Median buy size: **${stats.buy_usdc_median:.2f}**\n"
        f"- Share of buys sized $1-$5: **{stats.one_to_five_buy_share:.0%}**"
    )


def _brief_markdown() -> str:
    return (
        "### System Brief\n"
        "This is a local BTC 5-minute binary fair-value strategy lab. It is useful "
        "as a personal paper bot and as a compact example of trading-system "
        "discipline: market discovery, feed labeling, confidence-based sizing, "
        "one-position risk control, structured event logs, and a dashboard kill "
        "switch.\n\n"
        "This build does not sign or submit live orders. The active workflow is simple: "
        "**Start** begins simulated BTC 5m trading, and **Stop** halts new entries "
        "and closes any open simulated position."
    )


def _scorecard_markdown() -> str:
    return (
        "### Trading Systems Scorecard\n"
        "- Scope: BTC 5-minute Up/Down markets only.\n"
        "- Operator control: Start, Stop, Refresh, and activity feed.\n"
        "- Risk: one open paper position, bounded $1-$5 sizing, target/stop/time exits.\n"
        "- Feed discipline: public BTC fallback is labeled; Chainlink Streams is the intended reference.\n"
        "- Auditability: ticks, simulated positions, exits, config state, and notifications persist to SQLite.\n"
        "- Failure visibility: market/feed/loop errors surface in logs and dashboard state."
    )


def _settings_markdown() -> str:
    return (
        "### BTC 5m Paper Rules\n"
        f"- Market scope: BTC Up/Down 5-minute windows only.\n"
        f"- Paper sizing: **${BTC_PAPER_MIN_TRADE_USD:.0f}-${BTC_PAPER_MAX_TRADE_USD:.0f}** by confidence.\n"
        f"- Tick cadence: **{BTC_PAPER_TICK_SECONDS:.0f}s**.\n"
        f"- Minimum confidence: **{BTC_PAPER_MIN_CONFIDENCE:.0%}**.\n"
        f"- Minimum edge: **{BTC_PAPER_ENTRY_EDGE_MIN:.3f}**.\n"
        f"- Target / stop return: **{BTC_PAPER_TARGET_RETURN:.0%} / {BTC_PAPER_STOP_RETURN:.0%}**.\n"
        f"- Time exit: **{BTC_PAPER_TIME_EXIT_SECONDS}s**.\n"
        f"- Settlement-aware reference target: {BTC_CHAINLINK_STREAM_URL}.\n\n"
        "Required local env vars are optional for paper mode except path overrides. "
        "No private key is used by this build."
    )


def _backtest_markdown() -> str:
    report_path = DATA_DIR / "backtests" / "latest.json"
    if not report_path.exists():
        return (
            "### BTC 5m Binary Fair Value Backtest\n"
            "No local report yet. Run:\n\n"
            "```bash\n"
            "./.venv/bin/python tools/backtest_btc_strategy.py\n"
            "```"
        )
    report = json.loads(report_path.read_text(encoding="utf-8"))
    return format_report(report)


def _btc_views() -> tuple[str, str, str, str, str]:
    return (
        _overview_html(),
        _status_markdown(),
        _paper_html(),
        _history_markdown(),
        _activity_markdown(),
    )


def _btc_start_views() -> tuple[str, str, str, str, str]:
    try:
        _run(request_start())
    except Exception as e:  # noqa: BLE001
        log.exception("btc.start_failed", error=str(e))
    return _btc_views()


def _btc_stop_views() -> tuple[str, str, str, str, str]:
    try:
        _run(request_stop())
    except Exception as e:  # noqa: BLE001
        log.exception("btc.stop_failed", error=str(e))
    return _btc_views()


def build_ui() -> gr.Blocks:
    initial = _btc_views()
    with gr.Blocks(title="BTC 5m Binary Fair Value", css=CSS) as app:
        gr.HTML(
            """
            <div class='hero'>
              <h1>BTC 5m Binary Fair Value</h1>
              <p>Local paper-trading dashboard for Polymarket BTC Up/Down 5-minute markets.
              Resolution-aware fair-value signal, bounded paper sizing, and operator controls.</p>
            </div>
            """
        )
        with gr.Tab("Overview"):
            overview = gr.HTML(value=initial[0])
            gr.Markdown(value=_brief_markdown())
            gr.Markdown(value=_scorecard_markdown())
            history = gr.Markdown(value=initial[3])
        with gr.Tab("BTC 5m"):
            with gr.Row():
                start_btn = gr.Button("Start BTC Paper Bot", variant="primary", scale=2)
                stop_btn = gr.Button("Stop", variant="stop", scale=1)
                refresh_btn = gr.Button("Refresh", variant="secondary", scale=1)
            status = gr.Markdown(value=initial[1])
            paper = gr.HTML(value=initial[2])
        with gr.Tab("Activity"):
            activity = gr.Markdown(value=initial[4])
        with gr.Tab("Backtest"):
            backtest = gr.Markdown(value=_backtest_markdown())
            refresh_backtest = gr.Button("Refresh backtest report", variant="secondary")
            refresh_backtest.click(fn=_backtest_markdown, outputs=[backtest])
        with gr.Tab("Settings"):
            gr.Markdown(value=_settings_markdown())

        outputs = [overview, status, paper, history, activity]
        start_btn.click(fn=_btc_start_views, outputs=outputs)
        stop_btn.click(fn=_btc_stop_views, outputs=outputs)
        refresh_btn.click(fn=_btc_views, outputs=outputs)
        timer = gr.Timer(5.0)
        timer.tick(fn=_btc_views, outputs=outputs)
    return app


def launch() -> None:
    build_ui().launch(
        server_name=DASHBOARD_SERVER_NAME,
        server_port=DASHBOARD_SERVER_PORT,
        show_api=False,
    )
