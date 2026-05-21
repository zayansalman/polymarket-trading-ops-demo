"""Entrypoint.

Startup sequence:
  1. Load env (via config.py import)
  2. Init structured logging
  3. Init SQLite schema
  4. Start APScheduler for weather recommendation resolution/calibration
  5. Launch local Gradio dashboard on :7860 without auth

The Gradio launch blocks. Background jobs run via APScheduler in the same process.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from config import (
    BTC_BOT_MODE,
    DASHBOARD_SERVER_NAME,
    DASHBOARD_SERVER_PORT,
    DB_PATH,
    DHAKA_TZ,
    HF_TOKEN,
    MY_POLYMARKET_PROXY_ADDRESS,
    POLYMARKET_PRIVATE_KEY,
)
from calibrator import refit_calibrator
from dashboard import launch
from db import init_db, notify
from logging_setup import get_logger, setup_logging
from model_eval import resolve_pending_recs
from polymarket_client import PolymarketClient

log = get_logger("main")


def _run_async_job(coro_fn, *args, **kwargs):
    """APScheduler-compatible wrapper to run an async function in its own loop."""
    def _runner():
        asyncio.run(coro_fn(*args, **kwargs))

    return _runner


async def _resolve_and_refit() -> None:
    """Daily feedback loop: resolve settled recs, then refit the calibrator.

    Never raises — logs and moves on so the scheduler stays healthy.
    """
    try:
        async with PolymarketClient() as c:
            counts = await resolve_pending_recs(c)
        log.info("resolve_and_refit.resolved", **counts)
    except Exception as e:  # noqa: BLE001
        log.warning("resolve_and_refit.resolve_failed", error=str(e))
    try:
        cal = await refit_calibrator()
        log.info(
            "resolve_and_refit.refit",
            samples=cal.total_samples,
            fit_note=cal.fit_note,
            brier_before=round(cal.brier_before, 4),
            brier_after=round(cal.brier_after, 4),
        )
    except Exception as e:  # noqa: BLE001
        log.warning("resolve_and_refit.refit_failed", error=str(e))


def validate_env() -> None:
    if not MY_POLYMARKET_PROXY_ADDRESS:
        log.warning("MY_POLYMARKET_PROXY_ADDRESS not set — Portfolio tab will be disabled.")
    if not HF_TOKEN:
        log.warning("HF_TOKEN not set — LLM-based weather analysis will fail.")
    if BTC_BOT_MODE == "live" and not POLYMARKET_PRIVATE_KEY:
        log.warning("BTC_BOT_MODE=live but POLYMARKET_PRIVATE_KEY is not set.")


async def startup_tasks() -> None:
    """Init DB + log start."""
    await init_db()
    await notify(
        "system_start",
        f"Dashboard started at {datetime.now(timezone.utc).isoformat(timespec='seconds')}",
        {"db_path": str(DB_PATH)},
    )


def start_scheduler() -> BackgroundScheduler:
    """Weather feedback loop scheduler."""
    scheduler = BackgroundScheduler(timezone=DHAKA_TZ)
    # Model-improvement loop: poll Gamma for settled recs, then refit calibrator.
    # Runs twice daily (8am + 8pm Dhaka) — weather markets settle ~midnight
    # local at the target city, so this catches both the overnight and
    # mid-day settlement waves. Hourly would be wasteful.
    scheduler.add_job(
        _run_async_job(_resolve_and_refit),
        CronTrigger(hour="8,20", minute=0, timezone=DHAKA_TZ),
        id="resolve_and_refit",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    scheduler.start()
    log.info("scheduler.started", jobs=[j.id for j in scheduler.get_jobs()])
    return scheduler


def main() -> None:
    setup_logging("INFO")
    validate_env()
    log.info("boot", db=str(DB_PATH))

    asyncio.run(startup_tasks())

    start_scheduler()
    log.info("ui.launch", server_name=DASHBOARD_SERVER_NAME, port=DASHBOARD_SERVER_PORT)
    launch()


if __name__ == "__main__":
    main()
