# File Map

## Runtime

- `main.py` - app entrypoint.
- `dashboard.py` - BTC-only Gradio dashboard.
- `config.py` - local paths, dashboard bind, and BTC paper parameters.
- `db.py` - SQLite schema and helper functions.
- `logging_setup.py` - structured JSON logging.

## BTC Bot

- `btc_bot/controller.py` - Start/Stop controller and kill switch.
- `btc_bot/paper.py` - BTC 5-minute paper trading engine.
- `btc_bot/strategy.py` - shared probability, confidence, and sizing math.
- `btc_bot/backtest.py` - historical trade replay and parameter optimizer.
- `btc_bot/history.py` - optional exported BTC trade-history summary.
- `btc_bot/__init__.py` - package marker.

## Docs And Tools

- `README.md` - setup and lab overview.
- `PRD.md` - product scope and success criteria.
- `AGENTS.md` - repo-specific Codex operating rules.
- `docs/ARCHITECTURE.md` - module and data-flow overview.
- `docs/BACKTESTING.md` - local backtest methodology.
- `docs/ROADMAP.md` - trading-system engineering roadmap.
- `docs/OPERATIONS_RUNBOOK.md` - local runbook.
- `tools/demo_snapshot.py` - terminal summary of the current paper ledger.
- `tools/backtest_btc_strategy.py` - run backtest and optimization report.
