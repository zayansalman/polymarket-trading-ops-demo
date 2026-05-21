# PRD: Crypto Trading Ops Demo

## Goal

Build one local dashboard that can be shown in interviews as a compact crypto trading-operations system:

1. Run and monitor a **BTC 5-minute paper trader** with clear risk controls and ledgered lifecycle events.
2. Keep **weather bets** as a separate research-only module that shows analysis breadth but does not distract from the trading-ops demo.

## Interview Demo Requirements

- Show live crypto market discovery and signal generation.
- Show configurable parameters and risk gates.
- Show paper execution, reconciliation, and PnL in SQLite.
- Show monitoring surfaces useful for shift coverage.
- Be safe to share publicly: no `.env`, no private keys, no live order path in paper mode.

## Track 1: Weather Bets

Weather remains manual.

- Scan active Polymarket weather markets.
- Estimate bucket probabilities using Open-Meteo historical and forecast data.
- Surface gaps between market price and synthetic probability.
- Log recommendations for hit-rate, calibration, and hypothetical $1 PnL tracking.
- User places all weather trades manually in Polymarket.
- No weather auto-execution.

## Track 2: BTC 5-Minute Bot

BTC automation is now an approved repo direction.

The bot should:

- Trade only BTC 5-minute Polymarket Up/Down markets.
- Be controlled from the dashboard with Start and Stop.
- Use a dedicated local trading wallet.
- Keep private keys only in local `.env`.
- Paper trade with confidence-based `$1-$5` notional.
- Keep future live mode at fixed `$1` until explicitly changed.
- Enforce one position per market window.
- Refuse new entries when feeds are stale or risk gates fail.
- Persist signals, orders, fills, positions, exits, PnL, and errors to SQLite.
- Expose current state, feed health, latest signal, open position, and last errors in the dashboard.

## BTC Start/Stop Semantics

- **Start:** enable the BTC paper loop. It may open/manage simulated BTC positions only.
- **Stop:** immediately disable new simulated entries. Any open paper position is closed by the paper loop rules or window rollover.
- **Crash/restart:** recover paper state from SQLite before resuming. Live recovery is a later implementation.

## Risk Rules

- Local-only live execution.
- Dedicated wallet only.
- Paper sizing is `$1-$5` by confidence.
- Future live sizing remains `$1` by default.
- One position per BTC 5-minute market.
- No new entries on stale feeds.
- No secrets in logs, UI, git, or chat.
- No external notifications for now.

## Non-Goals

- No copy-wallet watchlist.
- No leaderboard scoring.
- No black swan scanner.
- No election, macro, quant, or generic deep research analyzers.
- No weather auto-execution.
- No non-BTC assets until BTC works.
- No public deployment of live executor.

## Active Dashboard Tabs

- **Home:** weather counts, BTC status, activity feed.
- **Weather:** market scanner and single-URL analysis.
- **Portfolio:** read-only Polymarket positions and weather rec-vs-actual tracking.
- **Performance:** resolve settled recommendations and refit weather calibrator.
- **BTC 5m:** Start/Stop, state, feed health, latest signal, position, errors.
- **Settings:** active scope and safety state.

## Acceptance Criteria

- `python main.py` launches locally at `127.0.0.1:7860`.
- No copy-wallet jobs run on startup.
- Weather scanner can run from the dashboard.
- Single weather URL analysis can run when `HF_TOKEN` is set.
- Portfolio tab works when `MY_POLYMARKET_PROXY_ADDRESS` is set.
- BTC tab exposes Start/Stop, current paper state, latest signal, open paper position, and recent paper trades.
- BTC paper loop can run without private keys or live order credentials.
- BTC live executor only starts when implementation, local credentials, and risk config are complete.
