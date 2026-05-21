# BTC 5-Minute Polymarket Paper Trading Demo

This repository is now a single-purpose, local-only demo for BTC 5-minute
Polymarket Up/Down paper trading.

The workflow is intentionally narrow:

1. Run the dashboard locally.
2. Press **Start BTC Paper Bot**.
3. The app discovers the current BTC 5-minute market, computes a simple fair
   Up probability from public BTC spot data, and records simulated entries and
   exits in SQLite.
4. Press **Stop** to prevent new simulated entries and force-close open paper
   positions.

No live orders are placed by this build. No private key is required.

## Why This Exists

The project is built as a small, observable trading-ops demo. It highlights
market discovery, signal logging, confidence-based sizing, risk controls,
persistence, and a dashboard kill switch without pretending to be a production
execution stack.

## Local Setup

```bash
python3.11 -m venv .venv
./.venv/bin/pip install -r requirements.txt
cp .env.example .env
./.venv/bin/python main.py
```

Open the dashboard at:

```text
http://127.0.0.1:7860
```

## Optional Environment

```bash
DATA_DIR=./data
DB_PATH=./data/btc_5m_demo.db
DASHBOARD_SERVER_NAME=127.0.0.1
DASHBOARD_SERVER_PORT=7860

BTC_BOT_MODE=paper
BTC_PAPER_MIN_TRADE_USD=1
BTC_PAPER_MAX_TRADE_USD=5
BTC_PAPER_TICK_SECONDS=5
BTC_PAPER_ENTRY_EDGE_MIN=0.045
BTC_PAPER_MIN_CONFIDENCE=0.62
BTC_PAPER_TARGET_RETURN=0.10
BTC_PAPER_STOP_RETURN=-0.08
BTC_PAPER_TIME_EXIT_SECONDS=45
BTC_HISTORY_CSV_PATH=./data/polymarket_history.csv
```

## Active Files

- `main.py` - initializes SQLite and launches the dashboard.
- `dashboard.py` - BTC-only Gradio control surface.
- `btc_bot/paper.py` - paper market discovery, signal, simulated entry/exit,
  and ledger logic.
- `btc_bot/controller.py` - Start/Stop control and kill-switch behavior.
- `btc_bot/history.py` - optional BTC history CSV summary for sizing context.
- `db.py` - SQLite schema for config, activity feed, ticks, and positions.
- `tools/demo_snapshot.py` - CLI snapshot for local operator review.

## Safety Boundaries

- Paper mode only.
- BTC 5-minute Up/Down markets only.
- One open BTC paper position per market window.
- Stop disables new entries immediately and force-closes open simulated
  positions.
- Live trading, signing, and private-key handling are intentionally absent.

## Snapshot CLI

```bash
./.venv/bin/python tools/demo_snapshot.py
```
