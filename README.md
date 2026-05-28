# BTC 5m Binary Fair Value

A modular, testable trading system for **BTC 5-minute binary fair value** trading on Polymarket Up/Down markets. Built with clean architecture, exchange-agnostic connectors, deterministic replay backtesting, and explicit paper order lifecycle simulation.

---

## Quick Start

```bash
python3.11 -m venv .venv
./.venv/bin/pip install -e ".[test]"
cp .env.example .env
./.venv/bin/python -m uvicorn btc_5m_fv.ops.dashboard.app:app --reload --port 7860
```

Open the dashboard at `http://127.0.0.1:7860`.

## What It Does

1. Discovers the active BTC 5-minute Up/Down market on Polymarket
2. Computes a **fair probability** that BTC finishes above the reference price using a log-normal volatility model (Black-Scholes CDF)
3. Compares fair probability to market price to find **edge**
4. Paper-trades when edge, confidence, time-remaining, and risk filters all pass
5. Manages positions with dynamic exits (target, stop, time decay, band reentry)
6. Records every tick to SQLite for deterministic replay backtesting
7. Provides a real-time operator dashboard with health telemetry and incident tracking

No live orders are placed. No private key is required.

---

## Architecture

```
btc_5m_fv/
├── core/           # Domain types, abstract interfaces, exceptions
├── strategy/       # Fair value math, signal generation, confidence sizing
├── connectors/     # Polymarket, Binance, Chainlink (stub), registry
├── storage/        # Market data recorder, deterministic replay engine
├── backtest/       # Full-market harness + conditional backtest
├── execution/      # Paper order lifecycle, risk service
└── ops/            # Controller, telemetry, incidents, FastAPI dashboard
```

### Key Design Decisions

- **Async throughout** — all I/O (HTTP, SQLite) uses `asyncio` via `httpx` and `aiosqlite`
- **Interface-driven** — every component implements an ABC from `core.interfaces`
- **SQLite WAL mode** — concurrent reads during writes for dashboard queries
- **Deterministic replay** — market data recorder enables ground-truth backtesting
- **Explicit order states** — PENDING -> ACKNOWLEDGED -> FILLED (not instant fills)
- **Venue-independent risk** — risk checks know nothing about Polymarket

---

## Strategy

**BTC 5m Binary Fair Value** estimates the fair probability that BTC finishes Up or Down over a fixed 5-minute window, compares to the market-implied price, and enters when edge exceeds 4.5%.

### Core Math

```
z = log(spot / reference) / (sigma * sqrt(remaining_seconds))
fair_up_prob = 0.5 * (1 + erf(z / sqrt(2)))
edge = fair_up_prob - market_up_price
```

Where `sigma` is per-second volatility from the last 90 1-second Binance closes, floored at 2bps/s.

### Entry Filters (all must pass)

| Filter | Threshold | Purpose |
|--------|-----------|---------|
| Time remaining | > 60s | Avoid expiry uncertainty |
| Edge magnitude | > 4.5% | Filter noise |
| Confidence | > 50% | Minimum conviction |
| Price bounds | 0.05 - 0.95 | Avoid slippage at extremes |

### Sizing

Linear scale from $1 (50% confidence) to $5 (99% confidence) based on edge magnitude.

### Exits (priority order)

1. **WINDOW_ROLL** — new 5-min window started
2. **TIME** — < 45s remaining
3. **TARGET** — +10% PnL
4. **STOP** — -8% PnL
5. **BAND_REENTRY** — edge fell below half threshold

---

## Systems Scorecard

- **Scope:** BTC 5m Up/Down markets only
- **Operator control:** Start, Stop, Refresh, visible activity feed
- **Risk control:** 1 open position, $1-$5 sizing, late-window skip, target/stop/time exits, drawdown monitoring
- **Feed discipline:** Binance public fallback; Chainlink Data Streams intended as settlement reference
- **Auditability:** every tick, entry, exit, and dashboard event persisted to SQLite
- **Testability:** 321 tests, deterministic fixtures, network-free unit tests
- **Failure visibility:** feed health telemetry, incident states, operator runbooks

---

## Testing

```bash
# Full suite
pytest tests/ -v

# Unit only (network-free)
pytest tests/unit/ -v

# With coverage
pytest tests/ --cov=btc_5m_fv --cov-report=term-missing
```

**321 tests** covering all modules: core types, strategy math, connectors, storage, backtest, execution, risk, telemetry, incidents, dashboard.

---

## CLI Tools

```bash
# Dashboard snapshot
python -m btc_5m_fv.tools.snapshot

# Run backtest
python -m btc_5m_fv.tools.backtest

# Record market data
python -m btc_5m_fv.tools.record --duration 3600
```

---

## Configuration

Copy `.env.example` to `.env` and adjust:

```bash
DATA_DIR=./data
DB_PATH=./data/btc_5m_binary_fair_value.db
DASHBOARD_PORT=7860

BTC_BOT_MODE=paper
BTC_PAPER_MIN_TRADE_USD=1
BTC_PAPER_MAX_TRADE_USD=5
BTC_PAPER_TICK_SECONDS=5
BTC_PAPER_ENTRY_EDGE_MIN=0.045
BTC_PAPER_MIN_CONFIDENCE=0.50
BTC_PAPER_ENTRY_MIN_REMAINING_SECONDS=60
BTC_PAPER_TARGET_RETURN=0.10
BTC_PAPER_STOP_RETURN=-0.08
BTC_PAPER_TIME_EXIT_SECONDS=45
```

---

## Project Evolution

See `CHANGELOG.md` for the full rebuild history from v0.1 (monolithic demo) to v0.2 (modular system).

See `docs/ROADMAP.md` for future priorities.

---

## Safety Boundaries

- Paper mode only. `BTC_BOT_MODE` is restricted to `{"paper"}`.
- BTC 5m Up/Down markets only.
- One open paper position per window.
- Stop disables new entries immediately and force-closes open positions.
- Live trading, signing, and private-key handling are intentionally absent.
