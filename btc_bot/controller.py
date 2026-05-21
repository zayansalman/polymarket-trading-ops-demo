"""Start/stop controller for the BTC 5-minute paper trader."""
from __future__ import annotations

import asyncio
import threading
from dataclasses import dataclass
from datetime import UTC, datetime

from btc_bot.paper import force_close_open_positions, run_paper_loop
from config import BTC_BOT_MODE, BTC_PAPER_MAX_TRADE_USD, BTC_PAPER_MIN_TRADE_USD
from db import get_config, set_config
from logging_setup import get_logger

log = get_logger("btc_controller")

PAPER_ONLY_DETAIL = (
    "BTC 5-minute paper mode is ready. No live orders are placed by this build."
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


def _default_detail() -> str:
    return (
        f"{PAPER_ONLY_DETAIL}\n\n"
        f"Paper sizing range: ${BTC_PAPER_MIN_TRADE_USD:.0f}-"
        f"${BTC_PAPER_MAX_TRADE_USD:.0f} by confidence."
    )


def _is_runner_alive() -> bool:
    return _runner_thread is not None and _runner_thread.is_alive()


async def get_status() -> BtcBotStatus:
    """Return current BTC controller status."""
    state = await get_config("btc_bot.state", "stopped")
    mode = await get_config("btc_bot.mode", BTC_BOT_MODE)
    updated_at = await get_config("btc_bot.updated_at")
    detail = await get_config("btc_bot.detail", _default_detail())

    if state == "running" and not _is_runner_alive():
        state = "stopped"
        detail = "BTC paper loop is not running in this process. Press Start to resume."
        await set_config("btc_bot.state", state)
        await set_config("btc_bot.detail", detail)

    return BtcBotStatus(
        state=state or "stopped",
        mode=mode or BTC_BOT_MODE,
        updated_at=updated_at,
        detail=detail or _default_detail(),
    )


async def request_start() -> BtcBotStatus:
    """Start the paper runner."""
    now = datetime.now(UTC).isoformat(timespec="seconds")
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
    now = datetime.now(UTC).isoformat(timespec="seconds")
    if _stop_event is not None:
        _stop_event.set()
    closed_count, close_error = await _safe_force_close()
    detail = (
        "BTC paper loop stop requested. New simulated entries are disabled. "
        f"Force-closed {closed_count} open paper position(s)."
    )
    if close_error:
        detail = f"{detail} Force-close check failed: {close_error}"
    await set_config("btc_bot.state", "stopped")
    await set_config("btc_bot.mode", "paper")
    await set_config("btc_bot.updated_at", now)
    await set_config("btc_bot.detail", detail)
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


async def _safe_force_close() -> tuple[int, str | None]:
    try:
        return await force_close_open_positions("STOP_REQUEST"), None
    except Exception as e:  # noqa: BLE001
        error = f"{type(e).__name__}: {e}"
        log.warning("btc.stop_force_close_failed", error=error)
        return 0, error
