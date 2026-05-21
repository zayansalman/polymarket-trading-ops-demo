# Instructions for Codex

## Active Scope

This repository is a local-only Polymarket workspace with two tracks:

1. **Weather bets:** dashboard analysis only. The app scans/analyzes weather markets and logs recommendations. The user places weather trades manually on Polymarket.
2. **BTC 5-minute bot:** local automated trading for BTC 5-minute Polymarket Up/Down markets. The user controls it from the dashboard with Start and Stop.

Legacy copy-trader and older spec bundles live under `archive/`. Do not follow archived rules unless the user explicitly asks to restore them.

## Absolute Rules

- **Weather is manual only.** Never auto-execute weather bets.
- **BTC automation is allowed only for BTC 5-minute Up/Down markets.** No ETH/SOL, no 15-minute markets, no generic crypto markets until explicitly requested.
- **Local only.** Do not deploy a live executor remotely while a private key is configured.
- **Dedicated trading wallet only.** BTC automation must use a wallet funded only with the amount the user is willing to risk.
- **Private key may be read only from local `.env`.** Never print it, log it, commit it, echo it, expose it in the UI, or ask the user to paste it into chat.
- **BTC paper sizing is confidence-based $1-$5.** Live sizing stays fixed `$1` until the user explicitly approves live scaling.
- **Start means paper trade for now; Stop means stop.** Start may open/manage simulated BTC positions. Stop must prevent new simulated entries immediately. Live behavior is a later explicit build.
- **One BTC position per 5-minute market window.**
- **No stale-feed trading.** If Binance, Polymarket CLOB, or required market metadata is stale, halt entries.
- **No silent failures.** Any executor, feed, signing, or order failure must surface in dashboard state and structured logs.

If a requested change expands risk beyond these rules, stop and ask first.

## BTC 5m Execution Rules

- Entry and exit behavior should be explicit in code and dashboard logs.
- Default paper size is `$1-$5` by confidence. Default live size is `$1`.
- Track every signal, order, fill, exit, and error in SQLite.
- Persist state before changing in-memory state so crash recovery is possible.
- Treat order acknowledgement as separate from fill confirmation.
- A kill switch must always be available from the dashboard.
- Paper mode is the active build target. Live mode is permitted later only after paper validation.
- Chainlink Data Streams is the intended settlement-aware BTC reference. If real-time Chainlink access is unavailable, use a clearly-labelled public fallback feed for paper mode only.

## Weather Conventions

- Use Open-Meteo and Polymarket public APIs.
- Use `huggingface_hub.InferenceClient` / `AsyncInferenceClient` only for LLM calls.
- Weather recommendations are informational, not auto-executed.
- Log surfaced weather recommendations to `recommendations` for later performance tracking.
- Use fixed $1 hypothetical sizing for weather performance calculations.

## Code Conventions

- Python 3.11, async where I/O is involved.
- `httpx` async HTTP, `aiosqlite` DB, `gradio.Blocks()` dashboard.
- `structlog` JSON logs.
- No silent failures; surface real errors in the dashboard.
- Prefer small modules with clear boundaries.
- Keep archived code untouched unless explicitly asked.

## Running Locally

- Use `./.venv/bin/python main.py`.
- Dashboard binds to `127.0.0.1:7860` by default.
- Required for full weather analysis: `HF_TOKEN`.
- Required for portfolio tracking: `MY_POLYMARKET_PROXY_ADDRESS`.
- Required for future BTC live execution: local `.env` trading credentials, never shared in chat.
