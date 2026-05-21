# Interview Demo

## 60-Second Pitch

This is a local BTC 5-minute Polymarket paper-trading ops demo. It is not a
black-box strategy claim; it is a compact demonstration of trading-system
discipline: clear market scope, explicit Start/Stop control, confidence-based
sizing, persistent logs, and simple risk limits.

## Suggested Walkthrough

1. Run `./.venv/bin/python main.py`.
2. Open `http://127.0.0.1:7860`.
3. Show the Overview tab: state, risk, exposure, paper PnL, and latest signal.
4. Press **Start BTC Paper Bot**.
5. Explain how the loop discovers BTC 5-minute markets and records ticks.
6. Open Activity to show structured operator events.
7. Press **Stop** and explain the kill switch.
8. Run `./.venv/bin/python tools/demo_snapshot.py` for a terminal summary.

## Talking Points

- The system is intentionally narrow: BTC 5-minute markets only.
- Paper sizing is $1-$5 by confidence because the historical BTC export shows
  that range is representative of the intended demo scale.
- The paper loop treats market discovery, signal, entry, exit, and operator
  events as auditable state.
- Live execution is not hidden in the code. That is a feature, not a gap, for
  an interview demo because it keeps the risk boundary honest.

## Future Work

- Chainlink Data Streams integration as the primary real-time reference.
- Proper CLOB quote freshness checks.
- Live order executor with dedicated wallet, acknowledgement/fill separation,
  reconciliation, and a reviewed rollout plan.
