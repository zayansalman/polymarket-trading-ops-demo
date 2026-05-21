"""Closing the loop: resolve past recommendations, score the model, expose metrics.

After a market settles, Gamma flips `closed = True` and `outcomePrices` holds
the final values. This module polls Gamma for any unresolved recommendation
whose event date is in the past, then writes back:
  - resolved_at
  - resolved_outcome_value   (0.0 or 1.0 for binary)
  - hit                      (1 if the rec's side matched the settled outcome)
  - realized_pnl_usd         (hypothetical $1 bet PnL at settlement)

Once recs have hits, `compute_metrics` produces honest calibration + hit-rate
stats the dashboard can show, and `calibrator.py` can learn from.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone

from db import connect
from logging_setup import get_logger
from polymarket_client import PolymarketClient, resolution_outcome_value

log = get_logger(__name__)


# ----------------------------------------------------------------------
# Resolving settled recs
# ----------------------------------------------------------------------


async def _load_unresolved_condition_ids() -> list[str]:
    """Every weather condition_id where at least one rec is still unresolved."""
    async with connect() as db:
        async with db.execute(
            "SELECT DISTINCT condition_id FROM recommendations "
            "WHERE source = 'weather' "
            "AND resolved_at IS NULL AND condition_id IS NOT NULL "
            "AND condition_id != ''"
        ) as cur:
            rows = await cur.fetchall()
    return [r["condition_id"] for r in rows]


async def resolve_pending_recs(client: PolymarketClient) -> dict:
    """Poll Gamma for every unresolved rec's market; write back if settled.

    Returns a counters dict: {checked, newly_resolved, still_open}.
    """
    cids = await _load_unresolved_condition_ids()
    if not cids:
        return {"checked": 0, "newly_resolved": 0, "still_open": 0}

    # Batch-fetch Gamma market metadata (20 per chunk — same as pnl.py pattern)
    market_by_cid: dict[str, dict] = {}
    for i in range(0, len(cids), 20):
        chunk = cids[i : i + 20]
        try:
            markets = await client.get_markets_by_condition_ids(chunk)
        except Exception as e:  # noqa: BLE001 — resolve loop must keep going
            log.warning("model_eval.gamma_failed", error=str(e), chunk_size=len(chunk))
            continue
        for m in markets:
            cid = m.get("conditionId")
            if cid:
                market_by_cid[cid] = m

    newly_resolved = 0
    still_open = 0
    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")

    async with connect() as db:
        for cid in cids:
            market = market_by_cid.get(cid)
            if market is None or not market.get("closed"):
                still_open += 1
                continue
            # Load all unresolved rec rows for this market (could be many
            # outcome sides — same market_id appears on both YES + NO buckets).
            async with db.execute(
                "SELECT rec_id, outcome_index, rec_price FROM recommendations "
                "WHERE source = 'weather' AND condition_id = ? AND resolved_at IS NULL",
                (cid,),
            ) as cur:
                rec_rows = await cur.fetchall()
            for rr in rec_rows:
                outcome_val = resolution_outcome_value(market, rr["outcome_index"])
                if outcome_val is None:
                    still_open += 1
                    continue
                # Binary markets resolve to {0, 1}. Multi-outcome scalar markets
                # can settle mid-range; we treat "hit" as outcome_val > 0.5 for
                # the rec's side (edge case, rarely fires for binary-only ingest).
                hit = 1 if outcome_val >= 0.5 else 0
                # Hypothetical $1 at rec_price → shares = 1/rec_price
                # Payout at resolution = shares * outcome_val
                # PnL = payout − 1
                pnl = (1.0 / rr["rec_price"]) * outcome_val - 1.0 if rr["rec_price"] > 0 else 0.0
                await db.execute(
                    "UPDATE recommendations SET "
                    "resolved_at = ?, resolved_outcome_value = ?, hit = ?, realized_pnl_usd = ? "
                    "WHERE rec_id = ?",
                    (now_iso, outcome_val, hit, round(pnl, 4), rr["rec_id"]),
                )
                newly_resolved += 1
        await db.commit()

    log.info(
        "model_eval.resolved",
        checked=len(cids),
        newly_resolved=newly_resolved,
        still_open=still_open,
    )
    return {
        "checked": len(cids),
        "newly_resolved": newly_resolved,
        "still_open": still_open,
    }


# ----------------------------------------------------------------------
# Metrics — scoring rules + slice breakdowns
# ----------------------------------------------------------------------


@dataclass
class ModelMetrics:
    """Aggregate + slice stats over resolved recommendations."""

    total_resolved: int = 0
    total_unresolved: int = 0
    hit_rate: float = 0.0
    brier_score: float = 0.0  # mean squared error; lower is better, 0.25 = random
    log_loss: float = 0.0  # cross-entropy; lower is better
    realized_pnl_per_bet_usd: float = 0.0  # hypothetical $1 bet avg
    total_realized_pnl_usd: float = 0.0
    # Slices: each maps category -> {"n", "hit_rate", "brier", "pnl", "pnl_per"}
    by_confidence: dict[str, dict[str, float]] = field(default_factory=dict)
    by_source: dict[str, dict[str, float]] = field(default_factory=dict)
    by_location: dict[str, dict[str, float]] = field(default_factory=dict)
    by_horizon: dict[str, dict[str, float]] = field(default_factory=dict)
    # Calibration histogram: predicted prob bin -> (predicted mean, actual mean, n)
    calibration_buckets: list[dict[str, float]] = field(default_factory=list)


def _brier(prob_yes: float, outcome: float) -> float:
    return (prob_yes - outcome) ** 2


def _logloss(prob_yes: float, outcome: float) -> float:
    p = min(max(prob_yes, 1e-6), 1 - 1e-6)
    return -(outcome * math.log(p) + (1 - outcome) * math.log(1 - p))


def _slice_metrics(rows: list[dict]) -> dict[str, float]:
    """Hit rate, Brier, PnL for a group of resolved rec rows."""
    n = len(rows)
    if n == 0:
        return {"n": 0, "hit_rate": 0.0, "brier": 0.0, "pnl": 0.0, "pnl_per": 0.0}
    hits = sum(r["hit"] for r in rows)
    # Prob assigned to the rec's own side. For binary: if outcome_index==0 (YES),
    # prob_side = synth_prob; if index==1 (NO), prob_side = 1 - synth_prob.
    briers: list[float] = []
    for r in rows:
        # Use calibrated_prob if present, else raw synth_prob, else rec_price as fallback
        base = r["calibrated_prob"] or r["synth_prob"] or r["rec_price"] or 0.5
        if r["outcome_index"] == 0:
            prob_side = base
        else:
            prob_side = 1.0 - base
        # The "actual" outcome for the rec's side is r["hit"] (1 if correct, 0 if not)
        briers.append(_brier(prob_side, float(r["hit"])))
    pnl = sum(r["realized_pnl_usd"] or 0.0 for r in rows)
    return {
        "n": n,
        "hit_rate": hits / n,
        "brier": sum(briers) / n,
        "pnl": pnl,
        "pnl_per": pnl / n,
    }


def _horizon_bucket(days_out: int | None) -> str:
    if days_out is None:
        return "unknown"
    if days_out <= 0:
        return "0d (today)"
    if days_out <= 3:
        return "1-3d"
    if days_out <= 7:
        return "4-7d"
    if days_out <= 14:
        return "8-14d"
    return "15d+"


async def compute_metrics(limit: int = 2000) -> ModelMetrics:
    """Pull recent resolved recs + compute honest scoring + slices."""
    async with connect() as db:
        async with db.execute(
            "SELECT rec_id, source, confidence, location, days_out, "
            "rec_price, synth_prob, calibrated_prob, outcome_index, "
            "resolved_outcome_value, hit, realized_pnl_usd "
            "FROM recommendations "
            "WHERE source = 'weather' AND hit IS NOT NULL "
            "ORDER BY resolved_at DESC "
            "LIMIT ?",
            (limit,),
        ) as cur:
            rows = [dict(r) for r in await cur.fetchall()]
        async with db.execute(
            "SELECT COUNT(*) as n FROM recommendations "
            "WHERE source = 'weather' AND hit IS NULL"
        ) as cur:
            unresolved = (await cur.fetchone())["n"]

    m = ModelMetrics(total_unresolved=unresolved)
    m.total_resolved = len(rows)
    if not rows:
        return m

    # Aggregate
    hits = sum(r["hit"] for r in rows)
    m.hit_rate = hits / len(rows)
    total_pnl = 0.0
    briers: list[float] = []
    losses: list[float] = []
    for r in rows:
        base = r["calibrated_prob"] or r["synth_prob"] or r["rec_price"] or 0.5
        prob_side = base if r["outcome_index"] == 0 else 1.0 - base
        briers.append(_brier(prob_side, float(r["hit"])))
        losses.append(_logloss(prob_side, float(r["hit"])))
        total_pnl += r["realized_pnl_usd"] or 0.0
    m.brier_score = sum(briers) / len(briers)
    m.log_loss = sum(losses) / len(losses)
    m.total_realized_pnl_usd = total_pnl
    m.realized_pnl_per_bet_usd = total_pnl / len(rows)

    # Slices
    def _by(key_fn) -> dict[str, dict[str, float]]:
        buckets: dict[str, list[dict]] = {}
        for r in rows:
            buckets.setdefault(key_fn(r), []).append(r)
        return {k: _slice_metrics(v) for k, v in buckets.items()}

    m.by_confidence = _by(lambda r: r["confidence"] or "NONE")
    m.by_source = _by(lambda r: r["source"])
    m.by_location = _by(lambda r: r["location"] or "unknown")
    m.by_horizon = _by(lambda r: _horizon_bucket(r["days_out"]))

    # Calibration histogram — bucket by predicted prob_side in 0.1 bins
    buckets: dict[int, list[tuple[float, int]]] = {}
    for r in rows:
        base = r["calibrated_prob"] or r["synth_prob"] or r["rec_price"] or 0.5
        prob_side = base if r["outcome_index"] == 0 else 1.0 - base
        bucket = min(int(prob_side * 10), 9)
        buckets.setdefault(bucket, []).append((prob_side, r["hit"]))
    m.calibration_buckets = []
    for b in range(10):
        lst = buckets.get(b, [])
        if not lst:
            continue
        pred_mean = sum(p for p, _ in lst) / len(lst)
        actual_mean = sum(h for _, h in lst) / len(lst)
        m.calibration_buckets.append(
            {
                "bucket": b,
                "range": f"{b*10}-{(b+1)*10}%",
                "n": len(lst),
                "predicted": pred_mean,
                "actual": actual_mean,
                "gap": actual_mean - pred_mean,
            }
        )
    return m


async def load_resolved_recs(limit: int = 30) -> list[dict]:
    """Recent resolved recs for the WIN/LOSS feed on the Model Performance tab."""
    async with connect() as db:
        async with db.execute(
            "SELECT rec_id, created_at, resolved_at, source, confidence, location, "
            "market_question, market_slug, event_slug, outcome, outcome_index, "
            "rec_price, synth_prob, calibrated_prob, edge_pp, "
            "resolved_outcome_value, hit, realized_pnl_usd, llm_verdict, "
            "llm_reasoning, notes, days_out "
            "FROM recommendations "
            "WHERE source = 'weather' AND hit IS NOT NULL "
            "ORDER BY resolved_at DESC "
            "LIMIT ?",
            (limit,),
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]
