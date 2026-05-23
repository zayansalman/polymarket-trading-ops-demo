"""BTC 5-minute paper trader.

This module deliberately does not place live orders. It discovers the current
Polymarket BTC 5m market, computes a simple volatility-band fair probability
from BTC spot, and records simulated entries/exits in SQLite.
"""
from __future__ import annotations

import asyncio
import json
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx

from config import (
    BTC_CHAINLINK_STREAM_URL,
    BTC_MARKET_TIMEFRAME_MINUTES,
    BTC_PAPER_ENTRY_EDGE_MIN,
    BTC_PAPER_ENTRY_MIN_REMAINING_SECONDS,
    BTC_PAPER_MAX_TRADE_USD,
    BTC_PAPER_MIN_CONFIDENCE,
    BTC_PAPER_MIN_TRADE_USD,
    BTC_PAPER_STOP_RETURN,
    BTC_PAPER_TARGET_RETURN,
    BTC_PAPER_TICK_SECONDS,
    BTC_PAPER_TIME_EXIT_SECONDS,
    POLYMARKET_GAMMA_API,
)
from db import connect, notify, set_config
from logging_setup import get_logger
from btc_bot.strategy import (
    StrategyParams,
    fair_up_probability,
    sigma_per_second,
    signal_from_edge,
)

log = get_logger("btc_paper")

BINANCE_API = "https://api.binance.com"
FIVE_MINUTES = BTC_MARKET_TIMEFRAME_MINUTES * 60
STRATEGY_PARAMS = StrategyParams(
    min_trade_usd=BTC_PAPER_MIN_TRADE_USD,
    max_trade_usd=BTC_PAPER_MAX_TRADE_USD,
    entry_edge_min=BTC_PAPER_ENTRY_EDGE_MIN,
    min_confidence=BTC_PAPER_MIN_CONFIDENCE,
    entry_min_remaining_seconds=BTC_PAPER_ENTRY_MIN_REMAINING_SECONDS,
)


@dataclass
class PaperSnapshot:
    created_at: str
    window_slug: str
    market_question: str
    remaining_seconds: int
    spot_price: float
    reference_price: float
    sigma_per_second: float
    market_up_price: float
    market_down_price: float
    fair_up_prob: float
    edge: float
    signal_side: str | None
    confidence: float
    notional_usd: float
    reason: str
    feed_source: str


@dataclass
class PaperSummary:
    running_state: str
    open_positions: int
    closed_positions: int
    total_pnl_usd: float
    open_exposure_usd: float
    closed_notional_usd: float
    win_rate: float | None
    avg_pnl_usd: float | None
    avg_hold_seconds: float | None
    risk_state: str
    last_signal: str
    last_tick_at: str | None
    last_window_slug: str | None
    last_spot_price: float | None
    last_fair_up_prob: float | None
    last_up_price: float | None
    last_edge: float | None
    last_feed_source: str | None
    recent_positions: list[dict[str, Any]]


async def run_paper_loop(stop_event: threading.Event) -> None:
    """Run until Stop is pressed or the process exits."""
    await set_config("btc_bot.state", "running")
    await set_config("btc_bot.mode", "paper")
    await _set_detail("BTC paper loop running. No real orders will be placed.")
    await notify("btc_paper_started", "BTC paper bot started")
    log.info("paper_loop.started")

    try:
        while not stop_event.is_set():
            try:
                snapshot = await paper_tick_once()
                await _set_detail(_detail_from_snapshot(snapshot))
            except Exception as e:  # noqa: BLE001
                error = f"{type(e).__name__}: {e!s}"
                log.warning("paper_loop.tick_failed", error=error)
                await _set_detail(f"BTC paper loop tick failed: {error}")
            await _sleep_interruptible(stop_event, float(BTC_PAPER_TICK_SECONDS))
    finally:
        await set_config("btc_bot.state", "stopped")
        await _set_detail("BTC paper loop stopped. No new paper entries will be opened.")
        await notify("btc_paper_stopped", "BTC paper bot stopped")
        log.info("paper_loop.stopped")


async def paper_tick_once() -> PaperSnapshot:
    """One paper trading tick. Useful for tests and dashboard-driven smoke checks."""
    async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
        snapshot = await _build_snapshot(client)
    await _log_tick(snapshot)
    await _close_due_positions(snapshot)
    await _maybe_open_position(snapshot)
    return snapshot


async def force_close_open_positions(exit_reason: str = "STOP_REQUEST") -> int:
    """Close all open paper positions at the latest available paper price."""
    async with connect() as db:
        async with db.execute(
            "SELECT * FROM btc_paper_positions WHERE state = 'open' ORDER BY opened_at"
        ) as cur:
            positions = [dict(r) for r in await cur.fetchall()]
    if not positions:
        return 0
    async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
        snapshot = await _build_snapshot(client)
    for pos in positions:
        await _close_position(
            pos,
            snapshot,
            _current_price_for_side(snapshot, pos["side"]),
            exit_reason,
        )
    return len(positions)


async def load_paper_summary() -> PaperSummary:
    """Dashboard summary from the SQLite paper ledger."""
    async with connect() as db:
        async with db.execute(
            "SELECT * FROM btc_paper_ticks ORDER BY created_at DESC LIMIT 1"
        ) as cur:
            tick = await cur.fetchone()
        async with db.execute(
            "SELECT COUNT(*) AS n, COALESCE(SUM(notional_usd), 0) AS exposure "
            "FROM btc_paper_positions WHERE state = 'open'"
        ) as cur:
            open_row = await cur.fetchone()
        async with db.execute(
            "SELECT COUNT(*) AS n, COALESCE(SUM(realized_pnl_usd), 0) AS pnl, "
            "COALESCE(SUM(notional_usd), 0) AS notional, "
            "SUM(CASE WHEN realized_pnl_usd > 0 THEN 1 ELSE 0 END) AS wins, "
            "AVG(realized_pnl_usd) AS avg_pnl, "
            "AVG(strftime('%s', closed_at) - strftime('%s', opened_at)) AS avg_hold "
            "FROM btc_paper_positions WHERE state = 'closed'"
        ) as cur:
            closed = await cur.fetchone()
        async with db.execute(
            "SELECT * FROM btc_paper_positions ORDER BY opened_at DESC LIMIT 10"
        ) as cur:
            recent = [dict(r) for r in await cur.fetchall()]

    last_signal = "none"
    if tick is not None:
        side = tick["signal_side"] or "SKIP"
        conf = tick["confidence"] if tick["confidence"] is not None else 0.0
        notional = tick["notional_usd"] if tick["notional_usd"] is not None else 0.0
        last_signal = f"{side} conf {conf:.2f} ${notional:.0f}: {tick['reason']}"

    open_count = int(open_row["n"] if open_row else 0)
    closed_count = int(closed["n"] if closed else 0)
    wins = int(closed["wins"] or 0) if closed else 0
    win_rate = (wins / closed_count) if closed_count else None
    avg_pnl = float(closed["avg_pnl"]) if closed and closed["avg_pnl"] is not None else None
    avg_hold = float(closed["avg_hold"]) if closed and closed["avg_hold"] is not None else None
    risk_state = _risk_state(open_count, tick["created_at"] if tick else None)

    return PaperSummary(
        running_state="paper",
        open_positions=open_count,
        closed_positions=closed_count,
        total_pnl_usd=float(closed["pnl"] if closed else 0.0),
        open_exposure_usd=float(open_row["exposure"] if open_row else 0.0),
        closed_notional_usd=float(closed["notional"] if closed else 0.0),
        win_rate=win_rate,
        avg_pnl_usd=avg_pnl,
        avg_hold_seconds=avg_hold,
        risk_state=risk_state,
        last_signal=last_signal,
        last_tick_at=tick["created_at"] if tick else None,
        last_window_slug=tick["window_slug"] if tick else None,
        last_spot_price=float(tick["spot_price"]) if tick else None,
        last_fair_up_prob=float(tick["fair_up_prob"]) if tick else None,
        last_up_price=float(tick["market_up_price"]) if tick else None,
        last_edge=float(tick["edge"]) if tick else None,
        last_feed_source=tick["feed_source"] if tick else None,
        recent_positions=recent,
    )


def _risk_state(open_positions: int, last_tick_at: str | None) -> str:
    if open_positions > 1:
        return "BREACH: more than one open BTC paper position"
    if last_tick_at is None:
        return "IDLE: no ticks yet"
    try:
        ts = datetime.fromisoformat(last_tick_at.replace("Z", "+00:00"))
    except ValueError:
        return "UNKNOWN: bad tick timestamp"
    age = (datetime.now(UTC) - ts).total_seconds()
    if age > max(BTC_PAPER_TICK_SECONDS * 3, 20):
        return f"STALE: last tick {int(age)}s ago"
    return "OK"


async def _build_snapshot(client: httpx.AsyncClient) -> PaperSnapshot:
    now = int(time.time())
    market = await _fetch_current_market(client, now)
    start_ts = int(market["window_start_ts"])
    slug = str(market["slug"])
    question = str(market.get("question") or slug)
    remaining = max(0, start_ts + FIVE_MINUTES - now)

    spot, closes = await _fetch_spot_and_recent_closes(client)
    reference = await _fetch_reference_price(client, start_ts)
    sigma = sigma_per_second(closes)
    up_price, down_price = _outcome_prices(market)
    fair_up = fair_up_probability(spot, reference, sigma, remaining)
    edge = fair_up - up_price
    side, confidence, notional, reason = signal_from_edge(
        edge,
        remaining,
        up_price,
        down_price,
        STRATEGY_PARAMS,
    )

    return PaperSnapshot(
        created_at=datetime.now(UTC).isoformat(timespec="seconds"),
        window_slug=slug,
        market_question=question,
        remaining_seconds=remaining,
        spot_price=spot,
        reference_price=reference,
        sigma_per_second=sigma,
        market_up_price=up_price,
        market_down_price=down_price,
        fair_up_prob=fair_up,
        edge=edge,
        signal_side=side,
        confidence=confidence,
        notional_usd=notional,
        reason=reason,
        feed_source=f"binance_public_fallback; chainlink_target={BTC_CHAINLINK_STREAM_URL}",
    )


async def _fetch_current_market(client: httpx.AsyncClient, now: int) -> dict[str, Any]:
    current_start = now - (now % FIVE_MINUTES)
    # Try current first, then next and previous to handle boundary/API timing.
    for start_ts in (current_start, current_start + FIVE_MINUTES, current_start - FIVE_MINUTES):
        slug = f"btc-updown-5m-{start_ts}"
        data = await _gamma_get(client, "markets", {"slug": slug})
        market = _first(data)
        if market is None:
            event_data = await _gamma_get(client, "events", {"slug": slug})
            event = _first(event_data)
            markets = event.get("markets") if event else None
            market = markets[0] if isinstance(markets, list) and markets else None
        if market is None:
            continue
        market["window_start_ts"] = start_ts
        return market
    raise RuntimeError("Could not discover current BTC 5-minute Polymarket market.")


async def _gamma_get(
    client: httpx.AsyncClient, endpoint: str, params: dict[str, Any]
) -> Any:
    r = await client.get(f"{POLYMARKET_GAMMA_API}/{endpoint}", params=params)
    r.raise_for_status()
    return r.json()


async def _fetch_spot_and_recent_closes(client: httpx.AsyncClient) -> tuple[float, list[float]]:
    r = await client.get(
        f"{BINANCE_API}/api/v3/klines",
        params={"symbol": "BTCUSDT", "interval": "1s", "limit": 90},
    )
    r.raise_for_status()
    rows = r.json()
    closes = [float(row[4]) for row in rows if len(row) > 4]
    if not closes:
        raise RuntimeError("Binance returned no BTC closes.")
    return closes[-1], closes


async def _fetch_reference_price(client: httpx.AsyncClient, window_start_ts: int) -> float:
    r = await client.get(
        f"{BINANCE_API}/api/v3/klines",
        params={
            "symbol": "BTCUSDT",
            "interval": "1s",
            "startTime": window_start_ts * 1000,
            "limit": 1,
        },
    )
    r.raise_for_status()
    rows = r.json()
    if not rows:
        raise RuntimeError("Binance returned no BTC window reference candle.")
    return float(rows[0][4])


def _outcome_prices(market: dict[str, Any]) -> tuple[float, float]:
    prices = _json_list(market.get("outcomePrices"))
    outcomes = _json_list(market.get("outcomes"))
    if len(prices) != 2:
        raise RuntimeError("BTC market did not expose two outcome prices.")
    up_idx = 0
    if len(outcomes) == 2:
        labels = [str(x).lower() for x in outcomes]
        if "up" in labels:
            up_idx = labels.index("up")
    down_idx = 1 - up_idx
    return float(prices[up_idx]), float(prices[down_idx])


def _json_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return []
        return parsed if isinstance(parsed, list) else []
    return []


def _first(value: Any) -> dict[str, Any] | None:
    if isinstance(value, list) and value:
        first = value[0]
        return first if isinstance(first, dict) else None
    return None


async def _log_tick(snapshot: PaperSnapshot) -> None:
    async with connect() as db:
        await db.execute(
            """
            INSERT INTO btc_paper_ticks(
              created_at, window_slug, market_question, remaining_seconds,
              spot_price, reference_price, sigma_per_second, market_up_price,
              market_down_price, fair_up_prob, edge, signal_side, confidence,
              notional_usd, feed_source, reason
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                snapshot.created_at,
                snapshot.window_slug,
                snapshot.market_question,
                snapshot.remaining_seconds,
                snapshot.spot_price,
                snapshot.reference_price,
                snapshot.sigma_per_second,
                snapshot.market_up_price,
                snapshot.market_down_price,
                snapshot.fair_up_prob,
                snapshot.edge,
                snapshot.signal_side,
                snapshot.confidence,
                snapshot.notional_usd,
                snapshot.feed_source,
                snapshot.reason,
            ),
        )
        await db.commit()


async def _maybe_open_position(snapshot: PaperSnapshot) -> None:
    if not snapshot.signal_side or snapshot.notional_usd <= 0:
        return
    async with connect() as db:
        async with db.execute(
            "SELECT COUNT(*) AS n FROM btc_paper_positions WHERE state = 'open'"
        ) as cur:
            if (await cur.fetchone())["n"]:
                return
        entry_price = (
            snapshot.market_up_price
            if snapshot.signal_side == "Up"
            else snapshot.market_down_price
        )
        shares = snapshot.notional_usd / entry_price
        await db.execute(
            """
            INSERT INTO btc_paper_positions(
              opened_at, window_slug, market_question, side, state, entry_price,
              notional_usd, shares, opened_spot, confidence, edge, entry_reason,
              feed_source
            ) VALUES (?, ?, ?, ?, 'open', ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                snapshot.created_at,
                snapshot.window_slug,
                snapshot.market_question,
                snapshot.signal_side,
                entry_price,
                snapshot.notional_usd,
                shares,
                snapshot.spot_price,
                snapshot.confidence,
                snapshot.edge,
                snapshot.reason,
                snapshot.feed_source,
            ),
        )
        await db.commit()
    await notify(
        "btc_paper_entry",
        f"Paper BUY {snapshot.signal_side} ${snapshot.notional_usd:.0f} @ {entry_price:.3f}",
        {"window_slug": snapshot.window_slug, "confidence": snapshot.confidence},
    )
    log.info(
        "paper_position.opened",
        window_slug=snapshot.window_slug,
        side=snapshot.signal_side,
        notional=snapshot.notional_usd,
        entry_price=entry_price,
    )


async def _close_due_positions(snapshot: PaperSnapshot) -> None:
    async with connect() as db:
        async with db.execute(
            "SELECT * FROM btc_paper_positions WHERE state = 'open' ORDER BY opened_at"
        ) as cur:
            positions = [dict(r) for r in await cur.fetchall()]

    for pos in positions:
        exit_price = _current_price_for_side(snapshot, pos["side"])
        reason = _exit_reason(snapshot, pos, exit_price)
        if reason is None:
            continue
        await _close_position(pos, snapshot, exit_price, reason)


async def _close_position(
    pos: dict[str, Any], snapshot: PaperSnapshot, exit_price: float, reason: str
) -> None:
    pnl = float(pos["shares"]) * (exit_price - float(pos["entry_price"]))
    async with connect() as db:
        await db.execute(
            """
            UPDATE btc_paper_positions
            SET state = 'closed', closed_at = ?, exit_price = ?,
                closed_spot = ?, exit_reason = ?, realized_pnl_usd = ?
            WHERE position_id = ?
            """,
            (
                snapshot.created_at,
                exit_price,
                snapshot.spot_price,
                reason,
                pnl,
                pos["position_id"],
            ),
        )
        await db.commit()
    await notify(
        "btc_paper_exit",
        f"Paper EXIT {pos['side']} ${pnl:+.2f} ({reason})",
        {"window_slug": pos["window_slug"], "position_id": pos["position_id"]},
    )
    log.info(
        "paper_position.closed",
        position_id=pos["position_id"],
        window_slug=pos["window_slug"],
        side=pos["side"],
        pnl=round(pnl, 4),
        exit_reason=reason,
    )


def _current_price_for_side(snapshot: PaperSnapshot, side: str) -> float:
    return snapshot.market_up_price if side == "Up" else snapshot.market_down_price


def _exit_reason(snapshot: PaperSnapshot, pos: dict[str, Any], exit_price: float) -> str | None:
    if pos["window_slug"] != snapshot.window_slug:
        return "WINDOW_ROLL"
    entry_price = float(pos["entry_price"])
    notional = float(pos["notional_usd"])
    shares = float(pos["shares"])
    pnl = shares * (exit_price - entry_price)
    if snapshot.remaining_seconds <= BTC_PAPER_TIME_EXIT_SECONDS:
        return "TIME"
    if pnl >= notional * BTC_PAPER_TARGET_RETURN:
        return "TARGET"
    if pnl <= notional * BTC_PAPER_STOP_RETURN:
        return "STOP"
    if abs(snapshot.edge) < BTC_PAPER_ENTRY_EDGE_MIN / 2:
        return "BAND_REENTRY"
    return None


async def _set_detail(detail: str) -> None:
    now = datetime.now(UTC).isoformat(timespec="seconds")
    await set_config("btc_bot.updated_at", now)
    await set_config("btc_bot.detail", detail)


def _detail_from_snapshot(snapshot: PaperSnapshot) -> str:
    side = snapshot.signal_side or "SKIP"
    return (
        "BTC paper loop running. No real orders are placed.\n\n"
        f"Window: {snapshot.window_slug} ({snapshot.remaining_seconds}s left)\n"
        f"Spot: ${snapshot.spot_price:,.2f} vs ref ${snapshot.reference_price:,.2f}\n"
        f"Polymarket Up: {snapshot.market_up_price:.3f}; fair Up: {snapshot.fair_up_prob:.3f}; "
        f"edge: {snapshot.edge:+.3f}\n"
        f"Signal: {side}; confidence {snapshot.confidence:.2f}; notional ${snapshot.notional_usd:.0f}\n"
        f"Feed: Binance public fallback while Chainlink Streams access is pending."
    )


async def _sleep_interruptible(stop_event: threading.Event, seconds: float) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if stop_event.is_set():
            return
        await asyncio.sleep(min(0.25, deadline - time.monotonic()))
