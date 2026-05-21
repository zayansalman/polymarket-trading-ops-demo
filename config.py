"""Config — env vars, paths, constants. Load once, import everywhere."""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


def _env_float(key: str, default: float) -> float:
    raw = os.getenv(key)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except ValueError as e:
        raise RuntimeError(f"Invalid float env var {key}: {raw!r}") from e


def _env_choice(key: str, default: str, allowed: set[str]) -> str:
    raw = os.getenv(key, default).strip().lower()
    if raw not in allowed:
        choices = ", ".join(sorted(allowed))
        raise RuntimeError(f"Invalid {key}: {raw!r}. Expected one of: {choices}")
    return raw


# --- Secrets / local credentials ---
HF_TOKEN = os.getenv("HF_TOKEN", "")
MY_POLYMARKET_PROXY_ADDRESS = os.getenv("MY_POLYMARKET_PROXY_ADDRESS", "").lower()
POLYMARKET_PRIVATE_KEY = os.getenv("POLYMARKET_PRIVATE_KEY", "")

# --- Paths ---
# Local dev:      DATA_DIR=./data
# HF Space Pro:   DATA_DIR=/data  (persistent storage mount)
DATA_DIR = Path(os.getenv("DATA_DIR", "./data")).resolve()
DATA_DIR.mkdir(parents=True, exist_ok=True)

DB_PATH = Path(os.getenv("DB_PATH", str(DATA_DIR / "polymarket_local.db"))).resolve()
DB_PATH.parent.mkdir(parents=True, exist_ok=True)

CONFIG_DIR = DATA_DIR / "config"
CONFIG_DIR.mkdir(parents=True, exist_ok=True)

REPO_ROOT = Path(__file__).parent.resolve()

# --- Local dashboard ---
# No Gradio auth in local-only mode. Keep the unauthenticated server bound to
# localhost so it is not exposed on the LAN by accident.
DASHBOARD_SERVER_NAME = os.getenv("DASHBOARD_SERVER_NAME", "127.0.0.1")
DASHBOARD_SERVER_PORT = int(os.getenv("DASHBOARD_SERVER_PORT", "7860"))

# --- Polymarket API ---
POLYMARKET_DATA_API = "https://data-api.polymarket.com"
POLYMARKET_GAMMA_API = "https://gamma-api.polymarket.com"
POLYMARKET_CLOB_API = "https://clob.polymarket.com"
POLYMARKET_RATE_LIMIT_PER_MIN = 60
POLYMARKET_MAX_CONCURRENT = 10
POLYMARKET_TIMEOUT_SECONDS = 30.0

# --- LLM routing (used step 3+) ---
LLM_DEFAULT_MODEL = "deepseek-ai/DeepSeek-V3"
LLM_FALLBACK_MODEL = "Qwen/Qwen2.5-72B-Instruct"
LLM_PROVIDER_ROUTE = "auto"  # HF Inference Providers routes cheapest available

# --- Weather/manual trading defaults ---
FIXED_TRADE_SIZE_USD = 1.0
MAX_CONCURRENT_POSITIONS = 10
MIN_CAPITAL_USD = 5.0

# --- BTC 5-minute bot defaults ---
BTC_BOT_MODE = _env_choice("BTC_BOT_MODE", "paper", {"paper", "live"})
BTC_FIXED_TRADE_SIZE_USD = _env_float("BTC_FIXED_TRADE_SIZE_USD", 1.0)
BTC_PAPER_MIN_TRADE_USD = _env_float("BTC_PAPER_MIN_TRADE_USD", 1.0)
BTC_PAPER_MAX_TRADE_USD = _env_float("BTC_PAPER_MAX_TRADE_USD", 5.0)
BTC_PAPER_TICK_SECONDS = _env_float("BTC_PAPER_TICK_SECONDS", 5.0)
BTC_PAPER_ENTRY_EDGE_MIN = _env_float("BTC_PAPER_ENTRY_EDGE_MIN", 0.045)
BTC_PAPER_MIN_CONFIDENCE = _env_float("BTC_PAPER_MIN_CONFIDENCE", 0.62)
BTC_PAPER_TARGET_RETURN = _env_float("BTC_PAPER_TARGET_RETURN", 0.10)
BTC_PAPER_STOP_RETURN = _env_float("BTC_PAPER_STOP_RETURN", -0.08)
BTC_PAPER_TIME_EXIT_SECONDS = int(os.getenv("BTC_PAPER_TIME_EXIT_SECONDS", "45"))
BTC_HISTORY_CSV_PATH = Path(
    os.getenv("BTC_HISTORY_CSV_PATH", "~/Downloads/Polymarket-History-2026-04-30.csv")
).expanduser()
BTC_CHAINLINK_STREAM_URL = "https://data.chain.link/streams/btc-usd-cexprice-streams"
BTC_MARKET_TIMEFRAME_MINUTES = 5
BTC_MAX_POSITIONS_PER_WINDOW = 1
BTC_STALE_FEED_SECONDS = 10

# --- Timezone ---
DHAKA_TZ = "Asia/Dhaka"
