# File Map

## Active Root

- `main.py` - local app entrypoint.
- `dashboard.py` - focused Gradio dashboard.
- `config.py` - env vars and constants.
- `db.py` - SQLite helpers and recommendation schema.
- `polymarket_client.py` - public Polymarket API wrapper.
- `weather_scanner.py` - multi-market weather scanner.
- `analyzer_weather.py` - single-market weather analyzer.
- `recommendations.py` - recommendation logging/querying.
- `model_eval.py` - recommendation settlement and scoring.
- `calibrator.py` - weather probability calibrator.
- `my_portfolio.py` - read-only Polymarket portfolio view.
- `llm.py` and `llm_sanity_check.py` - HF-only LLM helpers.
- `btc_bot/` - BTC 5m package; paper loop, history stats, Start/Stop state.
- `tools/analyze_my_trades.py` - one-off wallet/weather history audit helper.
- `tools/demo_snapshot.py` - interview-friendly CLI snapshot of BTC paper state.
- `docs/INTERVIEW_DEMO.md` - demo narrative mapped to crypto trading roles.
- `docs/OPERATIONS_RUNBOOK.md` - monitoring and shift handoff checklist.
- `docs/ARCHITECTURE.md` - active BTC paper trading architecture.

## Archive

- `archive/legacy_hold_to_resolution/` - old copy-trader/watchlist implementation and PRD.
- `archive/polyvol_spec/` - older BTC/PolyVol specification bundle.
- `archive/data_exports/` - old Polymarket CSV exports.
- `archive/graphify_out_legacy/` - stale generated project graph from the old rule set.
