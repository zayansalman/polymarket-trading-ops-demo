"""SQLite schema init + async connection helpers.

Active tables support weather recommendations, analysis history, portfolio
comparison, BTC control state, and config. Legacy tables that already exist in
the local SQLite file are left alone, but new installs only create the active
schema below.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator

import aiosqlite

from config import DB_PATH
from logging_setup import get_logger

log = get_logger("db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS analysis_requests (
  request_id INTEGER PRIMARY KEY AUTOINCREMENT,
  mode TEXT NOT NULL,
  requested_at TEXT NOT NULL,
  polymarket_url TEXT NOT NULL,
  market_id TEXT,
  market_question TEXT,
  market_price REAL,
  user_focus_areas TEXT,
  verdict TEXT,
  full_response_json TEXT,
  completed_at TEXT,
  duration_seconds INTEGER,
  llm_calls INTEGER
);

-- Every weather BET-YES/BET-NO the app surfaces is logged here,
-- so we can later compare surfaced ideas vs. the user's manual positions.
-- UNIQUE key dedupes same-source/market/side/day so repeat scans don't spam.
CREATE TABLE IF NOT EXISTS recommendations (
  rec_id INTEGER PRIMARY KEY AUTOINCREMENT,
  created_at TEXT NOT NULL,        -- ISO8601 UTC
  source TEXT NOT NULL,            -- 'weather' | future paper/live sources
  market_slug TEXT,                -- sub-market slug (weather bucket)
  event_slug TEXT,                 -- parent event slug for URL linking
  market_question TEXT,
  condition_id TEXT,               -- for position matching
  outcome TEXT NOT NULL,           -- 'Yes' | 'No'
  outcome_index INTEGER NOT NULL,  -- 0 | 1
  rec_price REAL NOT NULL,         -- market price at time of rec (0..1)
  synth_prob REAL,                 -- raw model probability (pre-calibration)
  calibrated_prob REAL,            -- post-calibrator probability
  edge_pp REAL,                    -- percentage-point edge (synth - market) * 100
  confidence TEXT,                 -- 'HIGH' | 'MEDIUM' | 'LOW'
  notes TEXT,                      -- free-text rationale
  source_ref TEXT,                 -- e.g. scan id
  dedupe_key TEXT NOT NULL UNIQUE, -- source|market_slug|outcome|YYYY-MM-DD
  -- Resolution tracking (set by model_eval.resolve_pending_recs)
  resolved_at TEXT,                -- when the market settled
  resolved_outcome_value REAL,     -- 0.0 or 1.0 for binary
  hit INTEGER,                     -- 1 if correct, 0 if wrong, NULL if unresolved
  realized_pnl_usd REAL,           -- hypothetical $1-bet PnL at settlement
  -- LLM sanity check (set by llm_sanity_check.sanity_check_rec)
  llm_verdict TEXT,                -- 'CONFIRM' | 'CAUTION' | 'REJECT' | NULL
  llm_reasoning TEXT,              -- one-liner rationale
  location TEXT,                   -- city, for per-location calibration
  days_out INTEGER                 -- forecast horizon at time of rec
);

CREATE INDEX IF NOT EXISTS idx_recommendations_created ON recommendations(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_recommendations_condition ON recommendations(condition_id, outcome_index);

CREATE TABLE IF NOT EXISTS notification_feed (
  event_id INTEGER PRIMARY KEY AUTOINCREMENT,
  created_at TEXT NOT NULL,
  event_type TEXT NOT NULL,
  headline TEXT NOT NULL,
  payload_json TEXT,
  seen INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS config (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_notification_feed_created ON notification_feed(created_at DESC);

CREATE TABLE IF NOT EXISTS btc_paper_ticks (
  tick_id INTEGER PRIMARY KEY AUTOINCREMENT,
  created_at TEXT NOT NULL,
  window_slug TEXT NOT NULL,
  market_question TEXT,
  remaining_seconds INTEGER NOT NULL,
  spot_price REAL NOT NULL,
  reference_price REAL NOT NULL,
  sigma_per_second REAL,
  market_up_price REAL NOT NULL,
  market_down_price REAL NOT NULL,
  fair_up_prob REAL NOT NULL,
  edge REAL NOT NULL,
  signal_side TEXT,
  confidence REAL,
  notional_usd REAL,
  feed_source TEXT NOT NULL,
  reason TEXT
);

CREATE INDEX IF NOT EXISTS idx_btc_paper_ticks_created ON btc_paper_ticks(created_at DESC);

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
  opened_spot REAL NOT NULL,
  closed_spot REAL,
  confidence REAL NOT NULL,
  entry_reason TEXT,
  exit_reason TEXT,
  realized_pnl_usd REAL
);

CREATE INDEX IF NOT EXISTS idx_btc_paper_positions_state ON btc_paper_positions(state, opened_at DESC);
CREATE INDEX IF NOT EXISTS idx_btc_paper_positions_window ON btc_paper_positions(window_slug);
"""


# Columns added after v1 of `recommendations` ships — ALTER TABLE adds them
# to existing DBs. Safe to re-run; the duplicate-column error is caught.
_REC_MIGRATIONS: tuple[tuple[str, str], ...] = (
    ("synth_prob", "REAL"),
    ("calibrated_prob", "REAL"),
    ("resolved_at", "TEXT"),
    ("resolved_outcome_value", "REAL"),
    ("hit", "INTEGER"),
    ("realized_pnl_usd", "REAL"),
    ("llm_verdict", "TEXT"),
    ("llm_reasoning", "TEXT"),
    ("location", "TEXT"),
    ("days_out", "INTEGER"),
)


async def _migrate_recommendations(db: aiosqlite.Connection) -> None:
    """Idempotent ALTER TABLE ADD COLUMN for newer recommendation fields."""
    for col, typ in _REC_MIGRATIONS:
        try:
            await db.execute(f"ALTER TABLE recommendations ADD COLUMN {col} {typ}")
        except aiosqlite.OperationalError as e:
            if "duplicate column" in str(e).lower():
                continue
            raise


async def init_db() -> None:
    """Create all tables + indexes. Idempotent.

    Also flips the DB into WAL mode so dashboard callbacks, weather scans,
    and the scheduled resolve/refit loop can read/write without the
    "database is locked" wars that kill default journal-mode SQLite.
    """
    log.info("init_db.start", path=str(DB_PATH))
    async with aiosqlite.connect(DB_PATH) as db:
        # Persistent settings (only journal_mode persists across connections;
        # the others must be re-set per connection — see connect() below).
        await db.execute("PRAGMA journal_mode = WAL")
        await db.execute("PRAGMA synchronous = NORMAL")
        await db.executescript(SCHEMA)
        await _migrate_recommendations(db)
        await db.commit()
    log.info("init_db.done", path=str(DB_PATH))


@asynccontextmanager
async def connect() -> AsyncIterator[aiosqlite.Connection]:
    """Async context manager for a DB connection. Row factory = sqlite3.Row.

    `busy_timeout` makes writers wait up to 5s for the lock instead of
    raising OperationalError immediately when dashboard callbacks and
    background maintenance touch the DB at the same time.
    """
    async with aiosqlite.connect(DB_PATH, timeout=10.0) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("PRAGMA busy_timeout = 5000")
        await db.execute("PRAGMA synchronous = NORMAL")  # safe under WAL, ~2x faster
        yield db


async def get_config(key: str, default: str | None = None) -> str | None:
    async with connect() as db:
        async with db.execute("SELECT value FROM config WHERE key = ?", (key,)) as cur:
            row = await cur.fetchone()
    return row["value"] if row else default


async def set_config(key: str, value: str) -> None:
    async with connect() as db:
        await db.execute(
            "INSERT INTO config(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
        await db.commit()


async def notify(event_type: str, headline: str, payload: dict | None = None) -> None:
    """Push an event onto the notification feed."""
    import json
    from datetime import datetime, timezone

    async with connect() as db:
        await db.execute(
            "INSERT INTO notification_feed(created_at, event_type, headline, payload_json) "
            "VALUES(?, ?, ?, ?)",
            (
                datetime.now(timezone.utc).isoformat(),
                event_type,
                headline,
                json.dumps(payload) if payload else None,
            ),
        )
        await db.commit()
