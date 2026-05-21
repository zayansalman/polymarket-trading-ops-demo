# Operations Runbook

## Pre-Shift Checklist

- Confirm the dashboard is reachable at `http://127.0.0.1:7860`.
- Confirm **BTC 5m** shows `Mode: paper`.
- Confirm **Risk and operations state** is `OK` or understand why it is idle/stale.
- Confirm latest feed source is visible and explicitly marked as the paper fallback.
- Confirm open exposure is expected before pressing Start.

## Normal Monitoring Loop

- Watch **Latest signal** for side, confidence, notional, and skip/entry reason.
- Watch **Open exposure** and **Open paper positions**.
- Watch **Last tick** age; if stale, press Stop and investigate network/API health.
- Review recent positions for exit reasons: `TARGET`, `STOP`, `TIME`, `BAND_REENTRY`, `WINDOW_ROLL`, or `STOP_REQUEST`.

## Stop / Kill Switch

Press **Stop BTC bot** in the dashboard.

Expected behavior:

- Bot state changes to `stopped`.
- New simulated entries are disabled.
- Any open paper position is force-closed with exit reason `STOP_REQUEST`.
- Activity feed records the stop event.

## Reconciliation

Use the dashboard or CLI snapshot:

```bash
python tools/demo_snapshot.py
```

Useful SQLite checks:

```bash
sqlite3 data/polymarket_local.db "SELECT state, COUNT(*), SUM(notional_usd), SUM(realized_pnl_usd) FROM btc_paper_positions GROUP BY state;"
sqlite3 data/polymarket_local.db "SELECT opened_at, side, entry_price, exit_price, notional_usd, realized_pnl_usd, exit_reason FROM btc_paper_positions ORDER BY position_id DESC LIMIT 10;"
sqlite3 data/polymarket_local.db "SELECT created_at, window_slug, signal_side, confidence, notional_usd, reason FROM btc_paper_ticks ORDER BY tick_id DESC LIMIT 10;"
```

## Incident Notes

- If market discovery fails, verify Polymarket Gamma is serving the current `btc-updown-5m-*` slug.
- If BTC spot fails, verify Binance public API access from the local network.
- If DB writes fail, verify `DATA_DIR` and `DB_PATH` are writable.
- If paper PnL diverges from intuition, inspect recent ticks and exit reasons before changing thresholds.
