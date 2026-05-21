# Interview Demo Brief

## Positioning

This repository is a compact crypto trading-operations demo. It does not try to be a production market maker. It shows the pieces a trading team cares about: signal generation, risk controls, lifecycle tracking, reconciliation, monitoring, and operator handoff.

## What To Show

1. Start `python main.py` and open `http://127.0.0.1:7860`.
2. Open **Interview Brief** and explain the role mapping.
3. Open **BTC 5m** and press **Start BTC bot**.
4. Watch latest signal, risk state, open exposure, feed source, and recent paper trades.
5. Press **Stop BTC bot** and point out that Stop disables new entries and force-closes any open paper position.
6. Run `python tools/demo_snapshot.py` to show the same state can be inspected outside the UI.

## Role Mapping

- **Capital efficiency:** confidence-based `$1-$5` paper sizing, open exposure metric, closed notional metric.
- **Algorithm/config optimization:** edge threshold, confidence threshold, target/stop/time exits, tick cadence, and sizing bands are configurable.
- **Trade lifecycle:** market discovery, tick logging, simulated entry, simulated exit, exit reason, PnL, and recent trade history are persisted in SQLite.
- **Execution/reconciliation:** paper fills are explicitly simulated; exported Polymarket history provides a baseline for manual trading behavior.
- **Risk/collateral:** one open position per market window, no real private key in paper mode, no live orders, stop/target/time exit rules, kill switch.
- **Monitoring/shift coverage:** dashboard surfaces live state, stale tick risk, feed source, latest signal, activity feed, and paper positions.

## Talking Points

- I scoped the demo down to the role-relevant path: BTC 5-minute crypto markets and operations monitoring.
- I separated paper execution from any live key path so the system is safe to demo publicly.
- The current signal is deliberately simple and inspectable: compare Polymarket Up price to a fair Up probability derived from BTC spot move and short-horizon volatility.
- The architecture is designed so a stronger model, CLOB websocket, Chainlink Data Streams, or a real execution adapter can be swapped in without changing the dashboard contract.
- I kept a research tab in the app to show broader analysis capability, but the BTC tab is the trading-systems demo.

## Current Limitations

- Uses Binance public spot as a paper-mode fallback while Chainlink Data Streams access is pending.
- Uses Gamma API polling rather than a low-latency CLOB websocket.
- Paper fills use displayed outcome prices, not full order book depth.
- Live execution is intentionally not wired in this demo.
