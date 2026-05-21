# Instructions for Codex

## Active Scope

This repository is a local-only BTC 5-minute Polymarket paper-trading demo.

The only active product behavior is:

1. User opens the local dashboard.
2. User presses **Start BTC Paper Bot**.
3. The bot paper trades BTC 5-minute Up/Down markets.
4. User presses **Stop** to halt new paper entries and close open simulated
   positions.

## Absolute Rules

- BTC 5-minute Up/Down markets only.
- Paper mode only until the user explicitly requests and approves live trading.
- Do not add live signing or order-submission code without explicit approval.
- Do not read, print, log, commit, echo, or expose private keys.
- The dashboard must stay local by default at `127.0.0.1:7860`.
- Start means paper trade; Stop means stop.
- One open BTC paper position at a time.
- No silent failures. Feed, market, state, or execution-loop errors must appear
  in structured logs or dashboard state.
- Keep modules small and boundaries clear.

## Code Conventions

- Python 3.11.
- Async I/O with `httpx` and `aiosqlite`.
- `gradio.Blocks()` dashboard.
- `structlog` JSON logs.
- SQLite for local paper ledger and dashboard state.
- Prefer explicit, boring safety over cleverness.

## Running Locally

```bash
./.venv/bin/python main.py
```

Dashboard:

```text
http://127.0.0.1:7860
```

Optional snapshot:

```bash
./.venv/bin/python tools/demo_snapshot.py
```
