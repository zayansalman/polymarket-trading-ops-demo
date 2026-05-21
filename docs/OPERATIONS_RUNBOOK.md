# Operations Runbook

This runbook is for the local BTC 5-minute trading systems lab. The goal is to
make operation boring: visible state, bounded paper risk, and fast Stop behavior.

## Start Locally

```bash
./.venv/bin/python main.py
```

Open:

```text
http://127.0.0.1:7860
```

## Paper Trading

- Press **Start BTC Paper Bot** to begin the BTC 5-minute paper loop.
- Press **Stop** to halt new paper entries and force-close open simulated
  positions.
- Use **Refresh** if you want an immediate dashboard update between timer ticks.

## Health Checks

```bash
./.venv/bin/python tools/demo_snapshot.py
```

Expected:

- Risk state is `OK`, `IDLE`, or an explicit stale/feed state.
- Open positions are `0` or `1`.
- Activity feed contains BTC bot events.
- Start/Stop events are visible in structured logs and SQLite notifications.

## Common Issues

- If the dashboard port is busy, stop the old process or change
  `DASHBOARD_SERVER_PORT`.
- If no current BTC market is found, wait for the next 5-minute boundary and
  refresh.
- If public BTC spot data is unavailable, the paper loop surfaces the error in
  logs and dashboard detail instead of opening silent entries.

## Data

SQLite lives at `DB_PATH`, defaulting to `./data/btc_5m_lab.db`.

The `data/` directory is local and gitignored.
