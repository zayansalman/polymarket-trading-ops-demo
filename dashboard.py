"""Local dashboard for the narrowed project scope.

Active scope:
  1. BTC 5-minute paper trader: monitor, start/stop, ledger, risk controls.
  2. Weather bets: research-only side module, manually executed by the user.
"""
from __future__ import annotations

import asyncio
import json
from html import escape
from datetime import datetime, timezone

import gradio as gr

from btc_bot.controller import get_status, request_start, request_stop
from btc_bot.history import load_btc_history_stats
from btc_bot.paper import load_paper_summary
from calibrator import load_calibrator, refit_calibrator
from config import (
    BTC_BOT_MODE,
    BTC_FIXED_TRADE_SIZE_USD,
    DASHBOARD_SERVER_NAME,
    DASHBOARD_SERVER_PORT,
    MY_POLYMARKET_PROXY_ADDRESS,
)
from db import connect
from logging_setup import get_logger
from model_eval import compute_metrics, load_resolved_recs, resolve_pending_recs
from my_portfolio import (
    MyPortfolioSummary,
    MyPosition,
    RecComparison,
    RecPerformance,
    days_until_resolution,
    fetch_rec_performance,
)
from polymarket_client import PolymarketClient, parse_polymarket_url

log = get_logger("dashboard")


CSS = """
.gradio-container { max-width: 1180px !important; margin: 0 auto; }
.big-title { font-size: 28px !important; font-weight: 800; margin: 0; letter-spacing: -0.02em; }
.subtitle { color: #4b5563; font-size: 14px; margin-top: 5px; max-width: 820px; }
.note { color: #666; font-size: 13px; }
.demo-hero {
  border: 1px solid #d8e1d5;
  border-radius: 18px;
  padding: 18px 20px;
  background:
    radial-gradient(circle at top left, rgba(187, 247, 208, .55), transparent 34%),
    linear-gradient(135deg, #f8faf5 0%, #f8fbff 100%);
  margin: 8px 0 16px;
}
.pill {
  display: inline-block;
  background: #132a13;
  color: white;
  border-radius: 999px;
  padding: 4px 10px;
  font-size: 11px;
  font-weight: 700;
  margin: 8px 6px 0 0;
}
"""


def _run(coro):
    return asyncio.run(coro)


def _fmt_relative(iso_str: str | None) -> str:
    if not iso_str:
        return "never"
    try:
        ts = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
    except ValueError:
        return iso_str
    delta = datetime.now(timezone.utc) - ts
    secs = int(delta.total_seconds())
    if secs < 60:
        return f"{secs}s ago"
    if secs < 3600:
        return f"{secs // 60}m ago"
    if secs < 86400:
        return f"{secs // 3600}h ago"
    return f"{secs // 86400}d ago"


def _kpi_card(label: str, value: str, subtle: str = "", color: str = "#222") -> str:
    return (
        "<div style='flex:1;min-width:160px;border:1px solid #e5e7eb;"
        "border-radius:12px;padding:14px 16px;background:#fff;'>"
        f"<div style='font-size:11px;color:#6b7280;text-transform:uppercase;"
        f"letter-spacing:.06em;'>{label}</div>"
        f"<div style='font-size:25px;font-weight:700;color:{color};margin-top:4px;'>{value}</div>"
        f"<div style='font-size:12px;color:#6b7280;margin-top:2px;'>{subtle}</div>"
        "</div>"
    )


def _btc_state_color(state: str) -> str:
    if state == "running":
        return "#10b981"
    if state == "not_ready":
        return "#f59e0b"
    if state == "error":
        return "#ef4444"
    return "#6b7280"


async def _load_feed(limit: int = 12) -> list[dict]:
    async with connect() as db:
        async with db.execute(
            "SELECT created_at, event_type, headline FROM notification_feed "
            "WHERE event_type = 'system_start' "
            "OR event_type LIKE 'weather_%' "
            "OR event_type LIKE 'btc_%' "
            "ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]


async def _weather_counts() -> dict:
    async with connect() as db:
        async with db.execute(
            "SELECT COUNT(*) AS n FROM recommendations WHERE source = 'weather'"
        ) as cur:
            weather_recs = (await cur.fetchone())["n"]
        async with db.execute(
            "SELECT COUNT(*) AS n FROM recommendations "
            "WHERE source = 'weather' AND hit IS NOT NULL"
        ) as cur:
            resolved_weather = (await cur.fetchone())["n"]
        async with db.execute(
            "SELECT requested_at FROM analysis_requests "
            "WHERE mode = 'weather' ORDER BY requested_at DESC LIMIT 1"
        ) as cur:
            row = await cur.fetchone()
            last_analysis = row["requested_at"] if row else None
    return {
        "weather_recs": weather_recs,
        "resolved_weather": resolved_weather,
        "last_analysis": last_analysis,
    }


def _home_html() -> str:
    counts = _run(_weather_counts())
    btc = _run(get_status())
    return (
        "<div style='display:flex;gap:12px;flex-wrap:wrap;margin:10px 0 16px;'>"
        f"{_kpi_card('Weather recs logged', str(counts['weather_recs']), 'manual review only')}"
        f"{_kpi_card('Resolved weather recs', str(counts['resolved_weather']), 'used for calibration')}"
        f"{_kpi_card('Last weather analysis', _fmt_relative(counts['last_analysis']))}"
        f"{_kpi_card('BTC bot', btc.state.upper(), btc.mode, _btc_state_color(btc.state))}"
        "</div>"
    )


def _activity_markdown() -> str:
    rows = _run(_load_feed())
    if not rows:
        return "_No activity yet. Run a weather scan to populate the feed._"
    lines = ["### Recent activity", ""]
    for r in rows:
        lines.append(f"- `{_fmt_relative(r['created_at']):>7}` {r['event_type']}: {r['headline']}")
    return "\n".join(lines)


def _pnl_color(pnl: float) -> str:
    if pnl > 0.01:
        return "#10b981"
    if pnl < -0.01:
        return "#ef4444"
    return "#6b7280"


def _position_card(p: MyPosition) -> str:
    outcome_color = "#10b981" if p.outcome_index == 0 else "#ef4444"
    status = "REDEEMABLE" if p.redeemable else "OPEN"
    status_color = "#2563eb" if p.redeemable else "#6b7280"
    pnl = p.unrealized_pnl_usd + p.realized_pnl_usd
    days_left = days_until_resolution(p.end_date)
    if days_left is None:
        resolve_text = "resolution date unknown"
    elif days_left < 0:
        resolve_text = f"ended {-days_left}d ago"
    elif days_left == 0:
        resolve_text = "resolves today"
    else:
        resolve_text = f"{days_left}d to resolution"
    slug = p.event_slug or p.slug
    link = (
        f"<a href='https://polymarket.com/event/{slug}' target='_blank'>open on Polymarket</a>"
        if slug else ""
    )
    title = p.title[:120] + ("..." if len(p.title) > 120 else "")
    return (
        "<div style='border:1px solid #e5e7eb;border-radius:12px;padding:14px 16px;"
        "margin-bottom:10px;background:#fff;'>"
        "<div style='display:flex;justify-content:space-between;gap:12px;align-items:baseline;'>"
        f"<div><span style='background:{outcome_color};color:white;padding:3px 9px;"
        f"border-radius:6px;font-size:11px;font-weight:700;'>HOLDING {p.outcome.upper()}</span> "
        f"<b>{title}</b></div>"
        f"<span style='background:{status_color};color:white;padding:3px 9px;"
        f"border-radius:6px;font-size:11px;font-weight:700;'>{status}</span>"
        "</div>"
        f"<div style='margin-top:8px;color:#374151;'>Entry {p.avg_price:.3f} -> now "
        f"{p.cur_price:.3f}; value ${p.current_value_usd:.2f}; "
        f"<b style='color:{_pnl_color(pnl)}'>PnL ${pnl:+.2f}</b></div>"
        f"<div style='margin-top:6px;color:#6b7280;font-size:12px;'>{resolve_text}"
        f"{' · ' + link if link else ''}</div>"
        "</div>"
    )


def _portfolio_summary_md(summary: MyPortfolioSummary) -> str:
    if summary.total_positions == 0:
        return f"_No open positions for `{MY_POLYMARKET_PROXY_ADDRESS}`._"
    return (
        f"**{summary.total_positions}** open positions · "
        f"**${summary.total_invested_usd:.2f}** invested · "
        f"**${summary.total_current_value_usd:.2f}** current value · "
        f"**${summary.total_pnl_usd:+.2f}** total PnL · "
        f"**{summary.redeemable_count}** redeemable"
    )


def _rec_card(c: RecComparison) -> str:
    rec = c.rec
    badge_color = "#10b981" if c.status == "TAKEN" else "#f59e0b"
    question = (rec.market_question or rec.market_slug or "unknown market")[:120]
    if c.status == "TAKEN" and c.matched_position is not None:
        p = c.matched_position
        pnl = p.unrealized_pnl_usd + p.realized_pnl_usd
        line = (
            f"Taken: {rec.outcome.upper()} @ {p.avg_price:.3f}, now {p.cur_price:.3f}; "
            f"<b style='color:{_pnl_color(pnl)}'>${pnl:+.2f}</b>"
        )
    elif c.hypothetical_pnl_usd is not None:
        pnl = c.hypothetical_pnl_usd
        line = (
            f"Skipped: rec was {rec.outcome.upper()} @ {rec.rec_price:.3f}; "
            f"hypothetical $1 PnL <b style='color:{_pnl_color(pnl)}'>${pnl:+.2f}</b>"
        )
    else:
        line = f"Skipped: rec was {rec.outcome.upper()} @ {rec.rec_price:.3f}; no live price."
    return (
        "<div style='border:1px solid #e5e7eb;border-radius:12px;padding:12px 14px;"
        "margin-bottom:8px;background:#fff;'>"
        f"<span style='background:{badge_color};color:white;padding:3px 9px;"
        f"border-radius:6px;font-size:11px;font-weight:700;'>{c.status}</span> "
        f"<b>{question}</b>"
        f"<div style='margin-top:7px;color:#374151;'>{line}</div>"
        f"<div style='margin-top:5px;color:#6b7280;font-size:12px;'>"
        f"{rec.source} · edge {rec.edge_pp if rec.edge_pp is not None else 'n/a'}pp · "
        f"{_fmt_relative(rec.created_at)}</div>"
        "</div>"
    )


def _rec_summary_md(perf: RecPerformance) -> str:
    if perf.total_recs == 0:
        return "_No weather recommendations logged yet._"
    return (
        f"**{perf.total_recs}** recs · **{perf.taken_count}** taken · "
        f"**{perf.skipped_count}** skipped · taken PnL **${perf.taken_pnl_usd:+.2f}** · "
        f"skipped hypothetical **${perf.skipped_pnl_hypothetical_usd:+.2f}**"
    )


def _portfolio_views() -> tuple[str, str, str, str]:
    if not MY_POLYMARKET_PROXY_ADDRESS:
        empty = "<div class='note'>Set MY_POLYMARKET_PROXY_ADDRESS in .env.</div>"
        return "_Wallet not configured._", empty, "", empty
    try:
        async def _go() -> tuple[MyPortfolioSummary, RecPerformance]:
            async with PolymarketClient() as client:
                return await fetch_rec_performance(client, MY_POLYMARKET_PROXY_ADDRESS)

        summary, perf = _run(_go())
    except Exception as e:  # noqa: BLE001
        log.error("portfolio.failed", error=str(e))
        return f"_Failed to fetch portfolio: {e}_", "", "", ""
    positions_html = (
        "\n".join(_position_card(p) for p in summary.positions)
        if summary.positions else "<div class='note'>No open positions.</div>"
    )
    weather_comparisons = [c for c in perf.comparisons if c.rec.source == "weather"]
    weather_perf = RecPerformance(
        comparisons=weather_comparisons,
        total_recs=len(weather_comparisons),
        taken_count=sum(1 for c in weather_comparisons if c.status == "TAKEN"),
        skipped_count=sum(1 for c in weather_comparisons if c.status == "SKIPPED"),
        taken_pnl_usd=sum(
            (c.matched_position.unrealized_pnl_usd + c.matched_position.realized_pnl_usd)
            for c in weather_comparisons
            if c.matched_position is not None
        ),
        skipped_pnl_hypothetical_usd=sum(
            c.hypothetical_pnl_usd or 0.0 for c in weather_comparisons
        ),
    )
    recs = weather_comparisons[:40]
    recs_html = "\n".join(_rec_card(c) for c in recs) if recs else "<div class='note'>No weather recs yet.</div>"
    return _portfolio_summary_md(summary), positions_html, _rec_summary_md(weather_perf), recs_html


def _resolve_first_binary_market(event_or_market: dict) -> dict | None:
    if isinstance(event_or_market.get("markets"), list):
        for market in event_or_market["markets"]:
            outcomes = market.get("outcomes")
            if isinstance(outcomes, str):
                try:
                    outcomes = json.loads(outcomes)
                except Exception:  # noqa: BLE001
                    outcomes = None
            if isinstance(outcomes, list) and len(outcomes) == 2:
                return market
        return event_or_market["markets"][0] if event_or_market["markets"] else None
    return event_or_market


async def _resolve_market(url: str) -> dict:
    slug = parse_polymarket_url(url)
    if not slug:
        raise ValueError("Could not parse a Polymarket slug from that URL.")
    async with PolymarketClient() as client:
        event = await client.get_event_by_slug(slug)
        if event:
            market = _resolve_first_binary_market(event)
            if market:
                market.setdefault("event_title", event.get("title") or event.get("name"))
                market.setdefault("event_slug", event.get("slug"))
                return market
        market = await client.get_market_by_slug(slug)
        if market:
            return market
    raise ValueError(f"No Polymarket market found for slug '{slug}'.")


def _extract_yes_price(market: dict) -> float | None:
    prices = market.get("outcomePrices")
    if isinstance(prices, str):
        try:
            prices = json.loads(prices)
        except Exception:  # noqa: BLE001
            return None
    if not isinstance(prices, list) or not prices:
        return None
    try:
        return float(prices[0])
    except (TypeError, ValueError):
        return None


async def _do_weather_analysis(url: str, focus: str) -> dict:
    from analyzer_weather import analyze, persist_request

    market = await _resolve_market(url)
    question = market.get("question") or market.get("event_title") or market.get("title") or ""
    market_price = _extract_yes_price(market)
    market_id = market.get("conditionId") or market.get("id")
    resolution = market.get("description") or market.get("resolutionSource") or ""
    report = await analyze(
        market_question=question,
        market_price=market_price,
        resolution_rules=resolution if resolution else None,
        focus_areas=focus.strip() or None,
    )
    await persist_request(url, market_id, question, market_price, report, focus.strip() or None)
    return {"question": question, "market_price": market_price, "report": report}


def _render_weather_report(bundle: dict) -> tuple[str, str]:
    report = bundle["report"]
    price = bundle["market_price"]
    price_str = f"{price:.3f} ({price * 100:.1f}%)" if price is not None else "n/a"
    gap = report.gap_points
    gap_str = f"{gap:+.1f} pts" if gap is not None else "n/a"
    gap_color = "#10b981" if gap is not None and gap > 0 else "#ef4444" if gap is not None else "#6b7280"
    summary = (
        "<div style='display:flex;gap:12px;flex-wrap:wrap;margin:10px 0 14px;'>"
        f"{_kpi_card('Market YES', price_str)}"
        f"{_kpi_card('Synth probability', f'{report.synthesized_prob * 100:.1f}%')}"
        f"{_kpi_card('Gap', gap_str, 'synth minus market', gap_color)}"
        f"{_kpi_card('Confidence', report.confidence)}"
        "</div>"
        f"<div class='note'><b>Market:</b> {bundle['question']}<br>"
        f"<b>Location:</b> {report.bundle.location_resolved}<br>"
        f"<b>Historical samples:</b> {len(report.bundle.historical_values)} · "
        f"<b>Ensemble members:</b> {len(report.bundle.ensemble_values)} · "
        f"<b>LLM calls:</b> {report.llm_calls}</div>"
    )
    return summary, report.markdown


def _bucket_to_english(kind: str, temp: int) -> str:
    if kind == "above":
        return f">={temp}C"
    if kind == "below":
        return f"<={temp}C"
    return f"exactly {temp}C"


def _weather_rec_card(rec: dict, rank: int) -> str:
    side = rec["side"]
    side_color = "#10b981" if side == "YES" else "#ef4444"
    edge_pp = rec["gap"] * 100
    location = rec["location"].split(",")[0].strip()
    bucket = _bucket_to_english(rec["bucket_kind"], rec["bucket_temp"])
    event_url = rec.get("event_url") or ""
    link = f"<a href='{event_url}' target='_blank'>open on Polymarket</a>" if event_url else ""
    return (
        "<div style='border:1px solid #e5e7eb;border-radius:12px;padding:15px 16px;"
        "margin-bottom:10px;background:#fff;'>"
        "<div style='display:flex;justify-content:space-between;gap:12px;align-items:baseline;'>"
        f"<div><span style='background:{side_color};color:white;padding:3px 9px;"
        f"border-radius:6px;font-size:11px;font-weight:700;'>#{rank} BET {side}</span> "
        f"<b>{location} max temp {bucket}</b></div>"
        f"<span style='color:#6b7280;font-size:12px;'>{rec['confidence']}</span>"
        "</div>"
        f"<div style='margin-top:8px;color:#374151;'>Market {rec['market_yes'] * 100:.1f}% vs "
        f"model {rec['synth_prob'] * 100:.1f}% -> edge <b>{edge_pp:+.1f} pts</b></div>"
        f"<div style='margin-top:6px;color:#6b7280;font-size:12px;'>"
        f"{rec['target_date']} · {rec['days_out']}d out · historical {rec['hist_prob'] * 100:.1f}% · "
        f"ensemble {rec['ens_prob'] * 100:.1f}%{' · ' + link if link else ''}</div>"
        "</div>"
    )


def scan_weather_callback(min_gap: float, max_events: int, top_n: int = 15):
    from weather_scanner import scan_weather_section

    try:
        result = _run(scan_weather_section(max_events=int(max_events), min_abs_gap=float(min_gap)))
    except Exception as e:  # noqa: BLE001
        log.exception("weather_scan.failed", error=str(e))
        return "", f"**Error:** {e}"
    recs = result["recommendations"]
    summary = result["summary"]
    status = (
        f"Scanned **{summary['events_scanned']}** events in {summary['elapsed_s']}s · "
        f"found **{len(recs)}** weather gaps · showing top **{min(top_n, len(recs))}**."
    )
    if not recs:
        return "<div class='note'>No weather gaps exceeded the threshold.</div>", status
    cards = [_weather_rec_card(rec, i + 1) for i, rec in enumerate(recs[:top_n])]
    return "\n".join(cards), status


def analyze_weather_callback(url: str, focus: str):
    url = (url or "").strip()
    if not url:
        return "", "", "Paste a Polymarket weather URL first."
    try:
        bundle = _run(_do_weather_analysis(url, focus or ""))
    except Exception as e:  # noqa: BLE001
        log.exception("weather_analysis.failed", error=str(e), url=url)
        return "", "", f"**Error:** {e}"
    summary, full = _render_weather_report(bundle)
    return summary, full, "Done."


async def _load_analysis_history(limit: int = 15) -> list[dict]:
    async with connect() as db:
        async with db.execute(
            "SELECT requested_at, mode, polymarket_url, market_question, market_price, verdict "
            "FROM analysis_requests ORDER BY requested_at DESC LIMIT ?",
            (limit,),
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]


def _analysis_history_md() -> str:
    rows = _run(_load_analysis_history())
    if not rows:
        return "_No analyses yet._"
    lines = ["### Recent analyses", ""]
    for row in rows:
        question = (row["market_question"] or row["polymarket_url"])[:100]
        price = f"{row['market_price']:.2f}" if row["market_price"] is not None else "n/a"
        lines.append(f"- `{_fmt_relative(row['requested_at']):>7}` {row['mode']} · price {price} · {question}")
    return "\n".join(lines)


async def _do_resolve_recs() -> str:
    try:
        async with PolymarketClient() as client:
            counts = await resolve_pending_recs(client)
    except Exception as e:  # noqa: BLE001
        return f"Resolve failed: {e}"
    return (
        f"Checked {counts['checked']} markets; newly resolved {counts['newly_resolved']}; "
        f"still open {counts['still_open']}."
    )


async def _do_refit_calibrator() -> str:
    try:
        cal = await refit_calibrator()
    except Exception as e:  # noqa: BLE001
        return f"Refit failed: {e}"
    if cal.fit_note == "insufficient_data":
        return f"Need 30 resolved weather recs to fit; currently have {cal.total_samples}."
    return (
        f"Calibrator {cal.fit_note}; samples {cal.total_samples}; "
        f"Brier {cal.brier_before:.3f} -> {cal.brier_after:.3f}."
    )


def _performance_views() -> tuple[str, str]:
    try:
        metrics = _run(compute_metrics(limit=2000))
        recent = _run(load_resolved_recs(limit=25))
        cal = _run(load_calibrator())
    except Exception as e:  # noqa: BLE001
        return f"<div style='color:#ef4444'>Failed: {e}</div>", ""
    summary = (
        "<div style='display:flex;gap:12px;flex-wrap:wrap;margin:10px 0 14px;'>"
        f"{_kpi_card('Resolved recs', str(metrics.total_resolved), f'{metrics.total_unresolved} pending')}"
        f"{_kpi_card('Hit rate', f'{metrics.hit_rate * 100:.1f}%')}"
        f"{_kpi_card('Brier', f'{metrics.brier_score:.3f}', 'lower is better')}"
        f"{_kpi_card('Hypothetical PnL', f'${metrics.total_realized_pnl_usd:+.2f}', '$1 per rec')}"
        "</div>"
        f"<div class='note'>Calibrator: {cal.fit_note}; samples {cal.total_samples}; "
        f"last fit {_fmt_relative(cal.fit_at)}.</div>"
    )
    if not recent:
        return summary, "<div class='note'>No resolved recommendations yet.</div>"
    cards = []
    for row in recent:
        hit = row.get("hit") == 1
        color = "#10b981" if hit else "#ef4444"
        label = "WIN" if hit else "LOSS"
        pnl = float(row.get("realized_pnl_usd") or 0.0)
        q = (row.get("market_question") or "")[:120]
        cards.append(
            "<div style='border:1px solid #e5e7eb;border-radius:12px;padding:12px 14px;"
            "margin-bottom:8px;background:#fff;'>"
            f"<span style='background:{color};color:white;padding:3px 9px;"
            f"border-radius:6px;font-size:11px;font-weight:700;'>{label}</span> "
            f"<b>{q}</b>"
            f"<div style='margin-top:6px;color:#374151;'>Bet {row.get('outcome')} @ "
            f"{float(row.get('rec_price') or 0):.3f}; PnL ${pnl:+.2f}</div>"
            "</div>"
        )
    return summary, "\n".join(cards)


def _btc_status_md() -> str:
    status = _run(get_status())
    paper = _run(load_paper_summary())
    updated = _fmt_relative(status.updated_at)
    return (
        f"**State:** `{status.state}`  \n"
        f"**Mode:** `{status.mode}`  \n"
        f"**Updated:** {updated}  \n"
        f"**Open paper positions:** `{paper.open_positions}`  \n"
        f"**Closed paper trades:** `{paper.closed_positions}`  \n"
        f"**Paper PnL:** `${paper.total_pnl_usd:+.2f}`  \n\n"
        f"{status.detail}"
    )


def _btc_paper_html() -> str:
    paper = _run(load_paper_summary())
    win_rate = "n/a" if paper.win_rate is None else f"{paper.win_rate:.0%}"
    avg_pnl = "n/a" if paper.avg_pnl_usd is None else f"${paper.avg_pnl_usd:+.2f}"
    avg_hold = "n/a" if paper.avg_hold_seconds is None else f"{paper.avg_hold_seconds:.0f}s"
    cards = (
        "<div style='display:flex;gap:10px;flex-wrap:wrap;margin:8px 0 12px;'>"
        f"{_kpi_card('Open paper positions', str(paper.open_positions))}"
        f"{_kpi_card('Open exposure', f'${paper.open_exposure_usd:.0f}', 'simulated notional')}"
        f"{_kpi_card('Closed trades', str(paper.closed_positions), f'win rate {win_rate}')}"
        f"{_kpi_card('Total paper PnL', f'${paper.total_pnl_usd:+.2f}', '', _pnl_color(paper.total_pnl_usd))}"
        f"{_kpi_card('Avg trade', avg_pnl, f'avg hold {avg_hold}')}"
        f"{_kpi_card('Last tick', _fmt_relative(paper.last_tick_at), paper.last_window_slug or '')}"
        "</div>"
    )
    risk_color = "#10b981" if paper.risk_state == "OK" else "#f59e0b"
    risk = (
        "<div style='border:1px solid #e5e7eb;border-radius:12px;padding:12px 14px;background:#fff;margin-bottom:10px;'>"
        f"<b>Risk and operations state</b><br><span style='color:{risk_color};font-weight:700'>{escape(paper.risk_state)}</span>"
        f"<div class='note'>Closed notional ${paper.closed_notional_usd:.0f}; max one paper position per 5-minute market; Stop force-closes open paper positions.</div>"
        "</div>"
    )
    signal = (
        "<div style='border:1px solid #e5e7eb;border-radius:12px;padding:12px 14px;background:#fff;margin-bottom:10px;'>"
        f"<b>Latest signal</b><br>{escape(paper.last_signal)}"
    )
    if paper.last_spot_price is not None:
        signal += (
            f"<div class='note'>Spot ${paper.last_spot_price:,.2f} · "
            f"Up {paper.last_up_price:.3f} · fair Up {paper.last_fair_up_prob:.3f} · "
            f"edge {paper.last_edge:+.3f}</div>"
        )
    if paper.last_feed_source:
        signal += f"<div class='note'>Feed: {escape(paper.last_feed_source)}</div>"
    signal += "</div>"
    rows = []
    for p in paper.recent_positions:
        pnl = p.get("realized_pnl_usd")
        pnl_text = "open" if pnl is None else f"${float(pnl):+.2f}"
        color = "#6b7280" if pnl is None else _pnl_color(float(pnl))
        rows.append(
            "<div style='border:1px solid #e5e7eb;border-radius:12px;padding:10px 12px;background:#fff;margin-bottom:8px;'>"
            f"<b>{escape(str(p.get('side')))}</b> "
            f"<span style='color:#6b7280'>{escape(str(p.get('window_slug')))}</span>"
            f"<div>Entry {float(p.get('entry_price') or 0):.3f}"
            f"{' -> ' + format(float(p.get('exit_price')), '.3f') if p.get('exit_price') is not None else ''} · "
            f"size ${float(p.get('notional_usd') or 0):.0f} · "
            f"<b style='color:{color}'>{pnl_text}</b></div>"
            f"<div class='note'>{escape(str(p.get('entry_reason') or ''))}"
            f"{' · ' + escape(str(p.get('exit_reason'))) if p.get('exit_reason') else ''}</div>"
            "</div>"
        )
    return cards + risk + signal + ("".join(rows) if rows else "<div class='note'>No paper trades logged yet.</div>")


def _btc_history_md() -> str:
    stats = load_btc_history_stats()
    if not stats.found:
        return f"_BTC history CSV not found at `{stats.path}`._"
    return (
        "### Your BTC history baseline\n"
        f"`{stats.btc_rows}` BTC rows from `{stats.total_rows}` exported rows · "
        f"`{stats.buys}` buys / `{stats.sells}` sells / `{stats.redeems}` redeems  \n"
        f"Buy size avg `${stats.buy_usdc_avg:.2f}`, median `${stats.buy_usdc_median:.2f}`, "
        f"range `${stats.buy_usdc_min:.2f}`-`${stats.buy_usdc_max:.2f}` · "
        f"{stats.one_to_five_buy_share:.0%} of BTC buys were in the $1-$5 band."
    )


def _btc_views() -> tuple[str, str, str]:
    return _btc_status_md(), _btc_paper_html(), _btc_history_md()


def _btc_start_views() -> tuple[str, str, str]:
    status = _run(request_start())
    return f"**State:** `{status.state}`\n\n{status.detail}", _btc_paper_html(), _btc_history_md()


def _btc_stop_views() -> tuple[str, str, str]:
    status = _run(request_stop())
    return f"**State:** `{status.state}`\n\n{status.detail}", _btc_paper_html(), _btc_history_md()


def _demo_brief_md() -> str:
    paper = _run(load_paper_summary())
    stats = load_btc_history_stats()
    win_rate = "n/a" if paper.win_rate is None else f"{paper.win_rate:.0%}"
    avg_pnl = "n/a" if paper.avg_pnl_usd is None else f"${paper.avg_pnl_usd:+.2f}"
    history = (
        f"{stats.btc_rows} BTC rows / {stats.buys} buys from exported Polymarket history"
        if stats.found else "history CSV optional"
    )
    return (
        "### Interview demo: crypto trading operations control plane\n\n"
        "This project is a local paper-trading and monitoring system for BTC 5-minute prediction markets. "
        "It is intentionally scoped to demonstrate trading-system thinking rather than deploy real capital.\n\n"
        "- **Strategy loop:** discovers the current BTC 5m market, computes fair Up probability from spot move and short-horizon volatility, then paper-fades market overconfidence.\n"
        "- **Risk controls:** paper size `$1-$5` by confidence, one position per market window, time/target/stop/band-reentry exits, Stop force-closes paper exposure.\n"
        "- **Trade lifecycle:** every tick, simulated entry, simulated exit, reason, confidence, notional, and PnL is persisted in SQLite.\n"
        f"- **Monitoring:** current state `{paper.risk_state}`, open exposure `${paper.open_exposure_usd:.0f}`, closed trades `{paper.closed_positions}`, win rate `{win_rate}`, average PnL `{avg_pnl}`.\n"
        f"- **Reconciliation baseline:** {history}; used to explain why sizing defaults to the `$1-$5` band.\n"
        "- **Shift coverage fit:** dashboard is local, refreshes every 5 seconds on BTC, and exposes latest signal, feed source, activity feed, and paper positions for handover.\n\n"
        "The weather module is retained as a separate research example, but the BTC tab is the role-relevant demo."
    )


def build_ui() -> gr.Blocks:
    with gr.Blocks(title="Crypto Trading Ops Demo", theme=gr.themes.Soft(), css=CSS) as demo:
        gr.Markdown(
            "<div class='demo-hero'>"
            "<div class='big-title'>Crypto Trading Ops Demo</div>"
            "<div class='subtitle'>A local BTC 5-minute paper-trading control plane: live market discovery, signal generation, risk gates, SQLite ledger, reconciliation baseline, and shift-friendly monitoring. No real orders are placed.</div>"
            "<span class='pill'>BTC 5m paper trading</span>"
            "<span class='pill'>risk controls</span>"
            "<span class='pill'>trade lifecycle</span>"
            "<span class='pill'>ops monitoring</span>"
            "</div>"
        )

        with gr.Tabs():
            with gr.Tab("Interview Brief"):
                brief = gr.Markdown(value=_demo_brief_md())
                refresh_brief = gr.Button("Refresh brief", size="sm")
                refresh_brief.click(fn=_demo_brief_md, outputs=[brief])

            with gr.Tab("Home"):
                home = gr.HTML(value=_home_html())
                activity = gr.Markdown(value=_activity_markdown())
                timer = gr.Timer(30.0)
                timer.tick(fn=lambda: (_home_html(), _activity_markdown()), outputs=[home, activity])
                refresh_home = gr.Button("Refresh", size="sm")
                refresh_home.click(fn=lambda: (_home_html(), _activity_markdown()), outputs=[home, activity])

            with gr.Tab("Weather"):
                gr.Markdown(
                    "### Manual weather bet finder\n"
                    "Scan live Polymarket weather markets, review the model gap, then open Polymarket and place any trade manually."
                )
                with gr.Row():
                    scan_btn = gr.Button("Scan weather markets", variant="primary", scale=2)
                    min_gap = gr.Slider(0.02, 0.30, value=0.05, step=0.01, label="Minimum absolute gap")
                    max_events = gr.Slider(5, 120, value=30, step=5, label="Events to scan")
                scan_status = gr.Markdown("")
                recs_html = gr.HTML("")
                scan_btn.click(scan_weather_callback, inputs=[min_gap, max_events], outputs=[recs_html, scan_status])

                with gr.Accordion("Analyze a specific weather URL", open=False):
                    with gr.Row():
                        url_in = gr.Textbox(label="Polymarket URL", scale=3)
                        focus_in = gr.Textbox(label="Focus areas", scale=2)
                    analyze_btn = gr.Button("Analyze URL", variant="secondary")
                    analyze_status = gr.Markdown("")
                    analyze_summary = gr.HTML("")
                    analyze_report = gr.Markdown("")
                    history = gr.Markdown(value=_analysis_history_md())
                    analyze_btn.click(
                        analyze_weather_callback,
                        inputs=[url_in, focus_in],
                        outputs=[analyze_summary, analyze_report, analyze_status],
                    ).then(fn=_analysis_history_md, outputs=[history])

            with gr.Tab("Portfolio"):
                gr.Markdown("### Manual positions and weather recommendation tracking")
                refresh_portfolio = gr.Button("Refresh", size="sm")
                p_summary, p_html, r_summary, r_html = _portfolio_views()
                portfolio_summary = gr.Markdown(value=p_summary)
                portfolio_html = gr.HTML(value=p_html)
                gr.Markdown("### Weather recs vs actual")
                rec_summary = gr.Markdown(value=r_summary)
                rec_html = gr.HTML(value=r_html)
                refresh_portfolio.click(
                    fn=lambda: _portfolio_views(),
                    outputs=[portfolio_summary, portfolio_html, rec_summary, rec_html],
                )

            with gr.Tab("Performance"):
                gr.Markdown("### Weather model feedback")
                with gr.Row():
                    resolve_btn = gr.Button("Resolve settled recs", variant="primary")
                    refit_btn = gr.Button("Refit calibrator", variant="secondary")
                    refresh_perf = gr.Button("Refresh")
                perf_status = gr.Markdown("")
                perf_summary_init, perf_recent_init = _performance_views()
                perf_summary = gr.HTML(value=perf_summary_init)
                perf_recent = gr.HTML(value=perf_recent_init)
                resolve_btn.click(fn=lambda: _run(_do_resolve_recs()), outputs=[perf_status]).then(
                    fn=lambda: _performance_views(), outputs=[perf_summary, perf_recent]
                )
                refit_btn.click(fn=lambda: _run(_do_refit_calibrator()), outputs=[perf_status]).then(
                    fn=lambda: _performance_views(), outputs=[perf_summary, perf_recent]
                )
                refresh_perf.click(fn=lambda: _performance_views(), outputs=[perf_summary, perf_recent])

            with gr.Tab("BTC 5m"):
                gr.Markdown(
                    "### BTC 5-minute paper-trading control\n"
                    "Paper mode discovers the current `btc-updown-5m-*` market, reads live Up/Down prices, "
                    "uses a public BTC spot fallback while Chainlink Streams access is pending, and logs simulated trades."
                )
                btc_status_init, btc_paper_init, btc_history_init = _btc_views()
                btc_status = gr.Markdown(value=btc_status_init)
                btc_paper = gr.HTML(value=btc_paper_init)
                btc_history = gr.Markdown(value=btc_history_init)
                with gr.Row():
                    start_btn = gr.Button("Start BTC bot", variant="primary")
                    stop_btn = gr.Button("Stop BTC bot", variant="stop")
                    refresh_btc = gr.Button("Refresh")
                btc_timer = gr.Timer(5.0)
                start_btn.click(fn=_btc_start_views, outputs=[btc_status, btc_paper, btc_history])
                stop_btn.click(fn=_btc_stop_views, outputs=[btc_status, btc_paper, btc_history])
                refresh_btc.click(fn=_btc_views, outputs=[btc_status, btc_paper, btc_history])
                btc_timer.tick(fn=_btc_views, outputs=[btc_status, btc_paper, btc_history])

            with gr.Tab("Settings"):
                gr.Markdown(
                    "### Active scope\n"
                    "- Weather bets: dashboard analysis only; trades are placed manually.\n"
                    f"- BTC 5m: local automation approved; current configured mode is `{BTC_BOT_MODE}`.\n"
                    "- Local dashboard only; no password; bound to 127.0.0.1 by default.\n\n"
                    "### Preserved safety defaults\n"
                    f"- BTC paper sizing uses `$1-$5` by confidence. Future live default remains `${BTC_FIXED_TRADE_SIZE_USD:.2f}`.\n"
                    "- Private keys stay in local `.env` only and are never printed or displayed.\n"
                    "- Weather recommendations are informational, not auto-executed.\n"
                    "- Legacy modules are archived/inactive."
                )

    return demo


def launch() -> None:
    demo = build_ui()
    demo.queue()
    demo.launch(
        server_name=DASHBOARD_SERVER_NAME,
        server_port=DASHBOARD_SERVER_PORT,
        show_error=True,
    )
