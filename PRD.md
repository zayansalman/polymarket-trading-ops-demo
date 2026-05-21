# PRD: BTC 5-Minute Paper Trading Demo

## Product Goal

Build a focused local dashboard that demonstrates BTC 5-minute Polymarket
trading operations without live execution risk.

The user should be able to press **Start** to run a paper BTC bot and press
**Stop** to halt it. The app should expose enough state to discuss strategy,
risk controls, operations, and future live-execution work in interviews.

## Scope

In scope:

- Discover current BTC 5-minute Up/Down Polymarket markets.
- Use public BTC spot data as a paper-mode feed.
- Show the intended Chainlink Data Streams reference in the dashboard.
- Compute a simple fair Up probability and edge versus market price.
- Size simulated trades between $1 and $5 by confidence.
- Persist every tick, simulated position, exit, and dashboard event in SQLite.
- Provide dashboard Start, Stop, Refresh, activity feed, and summary metrics.
- Summarize the optional exported BTC Polymarket history CSV.

Out of scope:

- Live order signing.
- Private key handling.
- Remote deployment.
- Any non-BTC market.
- Any timeframe other than 5-minute Up/Down.

## User Flow

1. User runs `./.venv/bin/python main.py`.
2. User opens `http://127.0.0.1:7860`.
3. User presses **Start BTC Paper Bot**.
4. Bot loops every configured tick interval.
5. Bot records market state, signal, and any simulated entries/exits.
6. User presses **Stop**.
7. Bot prevents new entries and closes any open simulated position.

## Risk Rules

- Paper mode is the only active mode.
- Trade size is $1-$5 by confidence.
- Only BTC 5-minute Up/Down markets are eligible.
- One open BTC paper position is allowed at a time.
- Late-window entries are skipped.
- Stale or failed feeds surface in logs/dashboard state.
- Stop acts as a kill switch.

## Success Criteria

- Dashboard has no unrelated tabs or modules.
- Start begins paper trading without requiring secrets.
- Stop halts new entries immediately.
- SQLite records ticks, positions, and activity.
- CLI snapshot prints current risk, PnL, and history baseline.
- Repo can be shown as a clean, job-relevant trading-ops demo.
