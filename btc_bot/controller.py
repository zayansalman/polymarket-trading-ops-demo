"""Start/stop surface for the BTC 5-minute bot.

BTC automation is allowed by the active repository rules, but the actual
market feed, signal engine, order executor, and recovery ledger still need to
be built. Until then, this controller records dashboard intent and reports
that execution is not ready instead of pretending a bot is running.
"""
from __future__ import annotations

import asyncio
import threading
from dataclasses import dataclass
from datetime import datetime, timezone

from config import BTC_BOT_MODE, BTC_PAPER_MAX_TRADE_USD, BTC_PAPER_MIN_TRADE_USD
from btc_bot.paper import force_close_open_positions, run_paper_loop
from db import get_config, set_config


LIVE_NOT_READY_REASON = (
    "Live BTC execution is not implemented here yet. Paper mode is available "
    "and does not place real orders."
)

_runner_thread: threading.Thread | None = None
_stop_event: threading.Event | None = None
_thread_lock = threading.Lock()


@dataclass
class BtcBotStatus:
    state: str
    mode: str
    updated_at: str | None
    detail: str


def _not_ready_detail() -> str:
    return (
        f"{LIVE_NOT_READY_REASON}\n\n"
        f"Configured mode: {BTC_BOT_MODE}. Paper sizing range: "
        f"${BTC_PAPER_MIN_TRADE_USD:.0f}-${BTC_PAPER_MAX_TRADE_USD:.0f} by confidence."
    )


def _is_runner_alive() -> bool:
    return _runner_thread is not None and _runner_thread.is_alive()


async def get_status() -> BtcBotStatus:
    """Return current BTC controller status, normalizing legacy DB state."""
    state = await get_config("btc_bot.state", "not_ready")
    mode = await get_config("btc_bot.mode", BTC_BOT_MODE)
    updated_at = await get_config("btc_bot.updated_at")
    detail = await get_config("btc_bot.detail", _not_ready_detail())

    stale_policy_state = (
        state == "blocked"
        or mode == "policy_gated"
        or (detail is not None and "AGENTS.md rules" in detail)
    )
    if stale_policy_state:
        state = "not_ready"
        mode = BTC_BOT_MODE
        detail = _not_ready_detail()
        await set_config("btc_bot.state", state)
        await set_config("btc_bot.mode", mode)
        await set_config("btc_bot.detail", detail)
    if state == "running" and not _is_runner_alive():
        state = "stopped"
        detail = "BTC paper loop is not running in this process. Press Start to resume paper trading."
        await set_config("btc_bot.state", state)
        await set_config("btc_bot.detail", detail)

    return BtcBotStatus(
        state=state or "not_ready",
        mode=mode or BTC_BOT_MODE,
        updated_at=updated_at,
        detail=detail or _not_ready_detail(),
    )


async def request_start() -> BtcBotStatus:
    """Start the paper runner. Never places live orders."""
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    if BTC_BOT_MODE != "paper":
        await set_config("btc_bot.state", "not_ready")
        await set_config("btc_bot.mode", BTC_BOT_MODE)
        await set_config("btc_bot.updated_at", now)
        await set_config("btc_bot.detail", _not_ready_detail())
        return await get_status()

    _ensure_runner_started()
    await set_config("btc_bot.state", "running")
    await set_config("btc_bot.mode", "paper")
    await set_config("btc_bot.updated_at", now)
    await set_config(
        "btc_bot.detail",
        "BTC paper loop starting. It will discover the current BTC 5m market and log simulated trades only.",
    )
    return await get_status()


async def request_stop() -> BtcBotStatus:
    """Stop the paper runner and disable new simulated entries."""
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    if _stop_event is not None:
        _stop_event.set()
    closed_count = await _safe_force_close()
    await set_config("btc_bot.state", "stopped")
    await set_config("btc_bot.mode", "paper")
    await set_config("btc_bot.updated_at", now)
    await set_config(
        "btc_bot.detail",
        f"BTC paper loop stop requested. New simulated entries are disabled. "
        f"Force-closed {closed_count} open paper position(s).",
    )
    return await get_status()


def _ensure_runner_started() -> None:
    global _runner_thread, _stop_event
    with _thread_lock:
        if _runner_thread is not None and _runner_thread.is_alive():
            return
        _stop_event = threading.Event()
        _runner_thread = threading.Thread(
            target=_run_loop_in_thread,
            args=(_stop_event,),
            name="btc-paper-runner",
            daemon=True,
        )
        _runner_thread.start()


def _run_loop_in_thread(stop_event: threading.Event) -> None:
    asyncio.run(run_paper_loop(stop_event))


async def _safe_force_close() -> int:
    try:
        return await force_close_open_positions("STOP_REQUEST")
    except Exception:  # noqa: BLE001
        return 0
