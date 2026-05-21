"""Entrypoint for the local BTC 5-minute paper trading dashboard."""
from __future__ import annotations

import asyncio

from config import DASHBOARD_SERVER_NAME, DASHBOARD_SERVER_PORT, DB_PATH
from dashboard import launch
from db import init_db, notify
from logging_setup import get_logger, setup_logging

log = get_logger("main")


async def startup_tasks() -> None:
    await init_db()
    await notify(
        "system_start",
        "BTC 5-minute paper trading dashboard started",
        {
            "db_path": str(DB_PATH),
            "server": f"{DASHBOARD_SERVER_NAME}:{DASHBOARD_SERVER_PORT}",
        },
    )


def main() -> None:
    setup_logging("INFO")
    log.info(
        "app.boot",
        db_path=str(DB_PATH),
        server_name=DASHBOARD_SERVER_NAME,
        server_port=DASHBOARD_SERVER_PORT,
    )
    asyncio.run(startup_tasks())
    launch()


if __name__ == "__main__":
    main()
