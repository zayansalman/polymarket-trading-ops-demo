"""SQLite storage for the BTC 5-minute paper trading demo."""
from __future__ import annotations

import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any, AsyncIterator

import aiosqlite

from config import DB_PATH


SCHEMA = """
CREATE TABLE IF NOT EXISTS notification_feed (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  created_at TEXT NOT NULL,
  event_type TEXT NOT NULL,
  message TEXT NOT NULL,
  details_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_notification_feed_created
  ON notification_feed(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_notification_feed_event
  ON notification_feed(event_type);

CREATE TABLE IF NOT EXISTS config (
  key TEXT PRIMARY KEY,
  value TEXT,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS btc_paper_ticks (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  created_at TEXT NOT NULL,
  window_slug TEXT NOT NULL,
  market_question TEXT,
  remaining_seconds INTEGER,
  spot_price REAL,
  reference_price REAL,
  sigma_per_second REAL,
  market_up_price REAL,
  market_down_price REAL,
  fair_up_prob REAL,
  edge REAL,
  signal_side TEXT,
  confidence REAL,
  notional_usd REAL,
  reason TEXT,
  feed_source TEXT
);
CREATE INDEX IF NOT EXISTS idx_btc_paper_ticks_created
  ON btc_paper_ticks(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_btc_paper_ticks_window
  ON btc_paper_ticks(window_slug);

CREATE TABLE IF NOT EXISTS btc_paper_positions (
  position_id INTEGER PRIMARY KEY AUTOINCREMENT,
  opened_at TEXT NOT NULL,
  closed_at TEXT,
  window_slug TEXT NOT NULL,
  market_question TEXT,
  side TEXT NOT NULL,
  state TEXT NOT NULL,
  entry_price REAL NOT NULL,
  exit_price REAL,
  notional_usd REAL NOT NULL,
  shares REAL NOT NULL,
  opened_spot REAL,
  closed_spot REAL,
  confidence REAL,
  edge REAL,
  entry_reason TEXT,
  exit_reason TEXT,
  realized_pnl_usd REAL,
  feed_source TEXT
);
CREATE INDEX IF NOT EXISTS idx_btc_paper_positions_state
  ON btc_paper_positions(state);
CREATE INDEX IF NOT EXISTS idx_btc_paper_positions_opened
  ON btc_paper_positions(opened_at DESC);
"""

BTC_POSITION_COLUMN_MIGRATIONS = {
    "market_question": "TEXT",
    "exit_price": "REAL",
    "shares": "REAL",
    "opened_spot": "REAL",
    "closed_spot": "REAL",
    "confidence": "REAL",
    "edge": "REAL",
    "entry_reason": "TEXT",
    "exit_reason": "TEXT",
    "realized_pnl_usd": "REAL",
    "feed_source": "TEXT",
}


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


@asynccontextmanager
async def connect() -> AsyncIterator[aiosqlite.Connection]:
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    try:
        yield db
    finally:
        await db.close()


async def init_db() -> None:
    async with connect() as db:
        await db.execute("PRAGMA journal_mode=WAL")
        await db.executescript(SCHEMA)
        await _migrate_columns(db, "btc_paper_positions", BTC_POSITION_COLUMN_MIGRATIONS)
        await db.commit()


async def _migrate_columns(
    db: aiosqlite.Connection,
    table: str,
    columns: dict[str, str],
) -> None:
    async with db.execute(f"PRAGMA table_info({table})") as cur:
        existing = {row["name"] for row in await cur.fetchall()}
    for column, column_type in columns.items():
        if column not in existing:
            await db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {column_type}")


async def get_config(key: str, default: str | None = None) -> str | None:
    async with connect() as db:
        async with db.execute("SELECT value FROM config WHERE key = ?", (key,)) as cur:
            row = await cur.fetchone()
    return row["value"] if row else default


async def set_config(key: str, value: str | None) -> None:
    async with connect() as db:
        await db.execute(
            """
            INSERT INTO config(key, value, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
              value = excluded.value,
              updated_at = excluded.updated_at
            """,
            (key, value, utc_now_iso()),
        )
        await db.commit()


async def notify(
    event_type: str,
    message: str,
    details: dict[str, Any] | None = None,
) -> None:
    payload = json.dumps(details or {}, sort_keys=True, default=str)
    async with connect() as db:
        await db.execute(
            """
            INSERT INTO notification_feed(created_at, event_type, message, details_json)
            VALUES (?, ?, ?, ?)
            """,
            (utc_now_iso(), event_type, message, payload),
        )
        await db.commit()
