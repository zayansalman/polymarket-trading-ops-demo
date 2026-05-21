"""Log + query app-generated weather recommendations for performance tracking.

Every weather BET idea the dashboard surfaces flows through
`log_recommendation`. The Portfolio tab later joins these against the user's
real /positions payload to answer which manual ideas were taken or skipped.

Read-only against Polymarket — this module doesn't execute anything.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from db import connect
from logging_setup import get_logger

log = get_logger(__name__)


@dataclass
class Recommendation:
    """One row of `recommendations` table."""

    rec_id: int | None
    created_at: str
    source: str  # 'weather' | future paper/live sources
    market_slug: str | None
    event_slug: str | None
    market_question: str | None
    condition_id: str | None
    outcome: str  # 'Yes' | 'No'
    outcome_index: int
    rec_price: float
    edge_pp: float | None  # percentage-point edge at rec time
    confidence: str | None
    notes: str | None
    source_ref: str | None
    dedupe_key: str
    # Optional enrichment (populated after initial insert)
    synth_prob: float | None = None
    calibrated_prob: float | None = None
    location: str | None = None
    days_out: int | None = None
    resolved_at: str | None = None
    resolved_outcome_value: float | None = None
    hit: int | None = None
    realized_pnl_usd: float | None = None
    llm_verdict: str | None = None
    llm_reasoning: str | None = None


def _dedupe_key(source: str, market_slug: str, outcome: str, created_at: str) -> str:
    # One rec per (source, market, side, day). Re-scanning the same market on
    # the same day with the same call won't double-log.
    day = created_at[:10]
    return f"{source}|{market_slug or '_'}|{outcome}|{day}"


async def log_recommendation(
    *,
    source: str,
    outcome: str,
    outcome_index: int,
    rec_price: float,
    market_slug: str | None = None,
    event_slug: str | None = None,
    market_question: str | None = None,
    condition_id: str | None = None,
    edge_pp: float | None = None,
    confidence: str | None = None,
    notes: str | None = None,
    source_ref: str | None = None,
    synth_prob: float | None = None,
    calibrated_prob: float | None = None,
    location: str | None = None,
    days_out: int | None = None,
    llm_verdict: str | None = None,
    llm_reasoning: str | None = None,
) -> bool:
    """Insert one recommendation; returns True if inserted, False if deduped."""
    created_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    key = _dedupe_key(source, market_slug or "", outcome, created_at)
    async with connect() as db:
        cur = await db.execute(
            "INSERT OR IGNORE INTO recommendations("
            "created_at, source, market_slug, event_slug, market_question, "
            "condition_id, outcome, outcome_index, rec_price, synth_prob, "
            "calibrated_prob, edge_pp, confidence, notes, source_ref, "
            "dedupe_key, location, days_out, llm_verdict, llm_reasoning) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                created_at, source, market_slug, event_slug, market_question,
                condition_id, outcome, outcome_index, rec_price, synth_prob,
                calibrated_prob, edge_pp, confidence, notes, source_ref, key,
                location, days_out, llm_verdict, llm_reasoning,
            ),
        )
        await db.commit()
        inserted = (cur.rowcount or 0) > 0
    if inserted:
        log.info(
            "recommendation.logged",
            source=source,
            outcome=outcome,
            market=market_slug,
            price=rec_price,
            edge_pp=edge_pp,
            llm_verdict=llm_verdict,
        )
    return inserted


async def load_recent_recommendations(limit: int = 200) -> list[Recommendation]:
    async with connect() as db:
        async with db.execute(
            "SELECT * FROM recommendations ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ) as cur:
            rows = await cur.fetchall()
    return [
        Recommendation(
            rec_id=r["rec_id"],
            created_at=r["created_at"],
            source=r["source"],
            market_slug=r["market_slug"],
            event_slug=r["event_slug"],
            market_question=r["market_question"],
            condition_id=r["condition_id"],
            outcome=r["outcome"],
            outcome_index=r["outcome_index"],
            rec_price=r["rec_price"],
            edge_pp=r["edge_pp"],
            confidence=r["confidence"],
            notes=r["notes"],
            source_ref=r["source_ref"],
            dedupe_key=r["dedupe_key"],
        )
        for r in rows
    ]
