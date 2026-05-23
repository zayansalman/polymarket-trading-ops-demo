# Engineering Roadmap

This roadmap keeps the project useful as a local BTC paper-trading tool while
moving it toward the engineering shape expected of serious trading systems.

## Current Strengths

- Narrow market scope: BTC 5-minute Up/Down only.
- Explicit operator controls: Start, Stop, Refresh, activity feed.
- Local paper ledger: ticks, simulated positions, exits, config state, and
  notifications are persisted in SQLite.
- Basic risk rules: bounded $1-$5 paper sizing, one open position, late-window
  skip, target/stop/time exits.
- Failure visibility: loop/feed/market errors are surfaced in logs and
  dashboard state.

## Priority Buildout

1. **Market Data Recorder**

   Persist raw market snapshots, BTC reference prices, quote timestamps, and
   feed-source metadata. This makes signals reproducible instead of only
   explainable after the fact.

2. **Full-Market Replay And Backtest Harness**

   Add a deterministic replay mode that runs the signal engine over recorded
   market data. Include basic market-friction assumptions: stale quotes,
   bid/ask spread, late-window liquidity, and no-fill cases.

   Current status: the repo has a trade-history conditional backtest in
   `btc_bot/backtest.py`. It is useful for optimizing filters over historical
   user buys, but it is not yet a full-market replay.

3. **Order Lifecycle Simulator**

   Model paper orders as separate acknowledgement, fill, partial-fill, cancel,
   exit, and reconciliation events. This keeps the paper system structurally
   close to a future live executor without adding live risk.

4. **Risk And PnL Console**

   Add realized/unrealized PnL, exposure, inventory, drawdown, win/loss by
   market window, and stop-reason attribution. Keep risk metrics visible in
   both dashboard and CLI snapshots.

5. **Feed Health And Latency Telemetry**

   Track feed heartbeat age, HTTP latency, tick-processing time, p50/p95/p99
   loop duration, and stale-feed halt reasons. Store telemetry in SQLite and
   expose it in the activity feed.

6. **Research-To-Production Boundary**

   Separate signal research from execution state. A new signal should be
   testable in replay before it is allowed in the live paper loop.

7. **Operational Runbooks And Incidents**

   Add incident states for stale market metadata, exchange/API failure,
   unexpected open-position count, DB write failure, and stop/force-close
   failure. Each state should have an operator action in the runbook.

8. **CI And Deterministic Fixtures**

   Add fixtures for market discovery, quote parsing, signal thresholds, order
   lifecycle transitions, and DB migrations. Tests should run without network
   access.

## Later, Explicitly Reviewed

- CLOB quote integration with freshness checks.
- Chainlink Data Streams as primary reference input.
- Dedicated-wallet live executor.
- Order acknowledgement/fill reconciliation.
- Position and balance reconciliation against venue state.
- Remote monitoring only after private-key handling is isolated.
