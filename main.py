"""Entrypoint for the BTC 5-minute paper trading system."""
from __future__ import annotations

import asyncio

from config import DASHBOARD_PORT, DB_PATH
from db import init_db, notify
from logging_setup import get_logger, setup_logging

# New architecture entrypoint (v0.2+)
try:
    from btc_5m_fv.ops.dashboard.app import app as dashboard_app
    HAS_NEW_DASHBOARD = True
except ImportError:
    HAS_NEW_DASHBOARD = False

# Legacy entrypoint (v0.1)
if not HAS_NEW_DASHBOARD:
    from config import DASHBOARD_SERVER_NAME, DASHBOARD_SERVER_PORT
    from dashboard import launch

log = get_logger("main")


async def startup_tasks() -> None:
    await init_db()
    await notify(
        "system_start",
        "BTC 5-minute paper trading system started",
        {
            "db_path": str(DB_PATH),
            "version": "0.2.0",
            "dashboard": "fastapi" if HAS_NEW_DASHBOARD else "gradio",
        },
    )


def main() -> None:
    setup_logging("INFO")
    log.info(
        "app.boot",
        db_path=str(DB_PATH),
        version="0.2.0",
        has_new_dashboard=HAS_NEW_DASHBOARD,
    )
    asyncio.run(startup_tasks())

    if HAS_NEW_DASHBOARD:
        import uvicorn
        log.info("dashboard.start_fastapi", port=DASHBOARD_PORT)
        uvicorn.run(
            "btc_5m_fv.ops.dashboard.app:app",
            host="127.0.0.1",
            port=DASHBOARD_PORT,
            log_level="info",
        )
    else:
        log.info("dashboard.start_gradio", server="127.0.0.1", port=DASHBOARD_SERVER_PORT)
        launch()


if __name__ == "__main__":
    main()
