"""Configuration for the local BTC 5-minute trading systems lab."""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


def _env_float(name: str, default: float) -> float:
    value = os.getenv(name)
    if value is None or value == "":
        return default
    try:
        return float(value)
    except ValueError:
        return default


def _env_choice(name: str, default: str, allowed: set[str]) -> str:
    value = os.getenv(name, default).strip().lower()
    return value if value in allowed else default


REPO_ROOT = Path(__file__).parent.resolve()

DATA_DIR = Path(os.getenv("DATA_DIR", "./data")).expanduser().resolve()
DATA_DIR.mkdir(parents=True, exist_ok=True)

DB_PATH = Path(
    os.getenv("DB_PATH", str(DATA_DIR / "btc_5m_lab.db"))
).expanduser().resolve()
DB_PATH.parent.mkdir(parents=True, exist_ok=True)

DASHBOARD_SERVER_NAME = os.getenv("DASHBOARD_SERVER_NAME", "127.0.0.1")
DASHBOARD_SERVER_PORT = int(os.getenv("DASHBOARD_SERVER_PORT", "7860"))

POLYMARKET_GAMMA_API = "https://gamma-api.polymarket.com"
BTC_CHAINLINK_STREAM_URL = "https://data.chain.link/streams/btc-usd-cexprice-streams"
BTC_MARKET_TIMEFRAME_MINUTES = 5

# Paper mode is the only active execution target. Live trading needs a later,
# explicit implementation and review before any order signing code is added.
BTC_BOT_MODE = _env_choice("BTC_BOT_MODE", "paper", {"paper"})
BTC_PAPER_MIN_TRADE_USD = _env_float("BTC_PAPER_MIN_TRADE_USD", 1.0)
BTC_PAPER_MAX_TRADE_USD = _env_float("BTC_PAPER_MAX_TRADE_USD", 5.0)
BTC_PAPER_TICK_SECONDS = _env_float("BTC_PAPER_TICK_SECONDS", 5.0)
BTC_PAPER_ENTRY_EDGE_MIN = _env_float("BTC_PAPER_ENTRY_EDGE_MIN", 0.045)
BTC_PAPER_MIN_CONFIDENCE = _env_float("BTC_PAPER_MIN_CONFIDENCE", 0.62)
BTC_PAPER_TARGET_RETURN = _env_float("BTC_PAPER_TARGET_RETURN", 0.10)
BTC_PAPER_STOP_RETURN = _env_float("BTC_PAPER_STOP_RETURN", -0.08)
BTC_PAPER_TIME_EXIT_SECONDS = int(os.getenv("BTC_PAPER_TIME_EXIT_SECONDS", "45"))

BTC_HISTORY_CSV_PATH = Path(
    os.getenv(
        "BTC_HISTORY_CSV_PATH",
        str(DATA_DIR / "polymarket_history.csv"),
    )
).expanduser()
