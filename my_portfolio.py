"""My Polymarket portfolio — honest view of user's actual positions.

Thin wrapper around `/positions` data-api. Polymarket returns everything we
need per-row (avgPrice, curPrice, cashPnl, realizedPnl, redeemable, endDate)
so no trade-replay is required for the dashboard view — we just normalize +
aggregate.

Also exposes `compare_recs_to_positions()` — joins weather recs against
the live /positions payload so the user can see:
  - which recs they acted on (TAKEN) vs. skipped (SKIPPED)
  - hypothetical mark-to-market PnL on SKIPPED recs using the latest price

Does NOT auto-execute anything.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from logging_setup import get_logger
from polymarket_client import PolymarketClient
from recommendations import Recommendation, load_recent_recommendations

log = get_logger(__name__)


@dataclass
class MyPosition:
    """One row out of /positions, normalized + with a status tag."""

    condition_id: str
    title: str
    slug: str
    event_slug: str
    outcome: str  # "Yes" | "No"
    outcome_index: int
    end_date: str | None  # "2026-04-21"

    size_shares: float  # current shares held
    avg_price: float  # cost basis, 0..1
    cur_price: float  # current market price for this outcome
    initial_value_usd: float  # shares * avg_price
    current_value_usd: float  # shares * cur_price
    unrealized_pnl_usd: float  # cashPnl — Polymarket's number
    unrealized_pnl_pct: float  # percentPnl / 100
    realized_pnl_usd: float  # from partial sells before now

    redeemable: bool  # market resolved, awaiting redemption
    status: str = "OPEN"  # OPEN | REDEEMABLE

    @property
    def total_pnl_usd(self) -> float:
        return self.unrealized_pnl_usd + self.realized_pnl_usd


@dataclass
class MyPortfolioSummary:
    positions: list[MyPosition] = field(default_factory=list)
    total_positions: int = 0
    redeemable_count: int = 0
    total_invested_usd: float = 0.0
    total_current_value_usd: float = 0.0
    total_unrealized_pnl_usd: float = 0.0
    total_realized_pnl_usd: float = 0.0

    @property
    def total_pnl_usd(self) -> float:
        return self.total_unrealized_pnl_usd + self.total_realized_pnl_usd

    @property
    def total_pnl_pct(self) -> float:
        if self.total_invested_usd <= 0:
            return 0.0
        return self.total_pnl_usd / self.total_invested_usd


def _parse_position_row(r: dict) -> MyPosition | None:
    cid = r.get("conditionId")
    if not cid:
        return None
    size = float(r.get("size") or 0)
    if size <= 0:
        # Fully closed — /positions shouldn't return these but guard anyway.
        return None
    redeemable = bool(r.get("redeemable"))
    return MyPosition(
        condition_id=cid,
        title=r.get("title") or "",
        slug=r.get("slug") or "",
        event_slug=r.get("eventSlug") or "",
        outcome=r.get("outcome") or "",
        outcome_index=int(r.get("outcomeIndex") or 0),
        end_date=r.get("endDate"),
        size_shares=size,
        avg_price=float(r.get("avgPrice") or 0),
        cur_price=float(r.get("curPrice") or 0),
        initial_value_usd=float(r.get("initialValue") or 0),
        current_value_usd=float(r.get("currentValue") or 0),
        unrealized_pnl_usd=float(r.get("cashPnl") or 0),
        unrealized_pnl_pct=float(r.get("percentPnl") or 0) / 100.0,
        realized_pnl_usd=float(r.get("realizedPnl") or 0),
        redeemable=redeemable,
        status="REDEEMABLE" if redeemable else "OPEN",
    )


async def fetch_my_portfolio(
    client: PolymarketClient, wallet_addr: str
) -> MyPortfolioSummary:
    """Pull /positions and roll up an honest portfolio summary."""
    rows = await client.get_positions(wallet_addr)
    positions: list[MyPosition] = []
    for r in rows:
        p = _parse_position_row(r)
        if p is not None:
            positions.append(p)

    # Sort: redeemable first (action needed), then by soonest end_date, then
    # by magnitude of unrealized PnL.
    def _sort_key(p: MyPosition) -> tuple[int, str, float]:
        return (
            0 if p.redeemable else 1,
            p.end_date or "9999-99-99",
            -abs(p.unrealized_pnl_usd),
        )

    positions.sort(key=_sort_key)

    summary = MyPortfolioSummary(
        positions=positions,
        total_positions=len(positions),
        redeemable_count=sum(1 for p in positions if p.redeemable),
        total_invested_usd=sum(p.initial_value_usd for p in positions),
        total_current_value_usd=sum(p.current_value_usd for p in positions),
        total_unrealized_pnl_usd=sum(p.unrealized_pnl_usd for p in positions),
        total_realized_pnl_usd=sum(p.realized_pnl_usd for p in positions),
    )
    log.info(
        "my_portfolio.fetched",
        wallet=wallet_addr,
        positions=summary.total_positions,
        invested=round(summary.total_invested_usd, 2),
        unrealized_pnl=round(summary.total_unrealized_pnl_usd, 2),
        realized_pnl=round(summary.total_realized_pnl_usd, 2),
    )
    return summary


@dataclass
class RecComparison:
    """One recommendation joined against the user's live portfolio."""

    rec: Recommendation
    status: str  # 'TAKEN' | 'SKIPPED'
    matched_position: MyPosition | None  # only set when status == 'TAKEN'
    current_price: float | None  # live price for the rec's outcome
    hypothetical_pnl_usd: float | None  # for SKIPPED, vs. a fixed $1 bet
    hypothetical_pnl_pct: float | None

    @property
    def outcome_label(self) -> str:
        return f"{self.rec.source.upper()} {self.rec.outcome.upper()}"


@dataclass
class RecPerformance:
    comparisons: list[RecComparison] = field(default_factory=list)
    total_recs: int = 0
    taken_count: int = 0
    skipped_count: int = 0
    taken_pnl_usd: float = 0.0  # sum of unrealized+realized on matched positions
    skipped_pnl_hypothetical_usd: float = 0.0  # sum of $1-bet hypothetical
    # Per-source breakdown
    by_source: dict[str, dict[str, float]] = field(default_factory=dict)


def _match_key(condition_id: str | None, outcome_index: int) -> str | None:
    """Join key between Recommendation and MyPosition."""
    if not condition_id:
        return None
    return f"{condition_id}|{outcome_index}"


def _rec_current_price(
    rec: Recommendation,
    positions_by_key: dict[str, MyPosition],
    extra_prices: dict[str, list[float]] | None = None,
) -> float | None:
    """Live price for the recommended outcome.

    Free path: a matching position already carries `curPrice`.
    Fallback: `extra_prices[condition_id]` holds the outcomePrices list pulled
    from Gamma for skipped recs.
    """
    key = _match_key(rec.condition_id, rec.outcome_index)
    if key and key in positions_by_key:
        return positions_by_key[key].cur_price
    if extra_prices and rec.condition_id in extra_prices:
        prices = extra_prices[rec.condition_id]
        if rec.outcome_index < len(prices):
            return prices[rec.outcome_index]
    return None


def compare_recs_to_positions(
    recs: list[Recommendation],
    positions: list[MyPosition],
    extra_prices: dict[str, list[float]] | None = None,
) -> RecPerformance:
    """Join recs against positions. $1 notional per rec (playbook rule).

    `extra_prices` supplies curPrice for SKIPPED recs where no position exists.
    """
    positions_by_key: dict[str, MyPosition] = {}
    for p in positions:
        k = _match_key(p.condition_id, p.outcome_index)
        if k:
            positions_by_key[k] = p

    comparisons: list[RecComparison] = []
    taken_pnl = 0.0
    skipped_hypo_pnl = 0.0
    by_source: dict[str, dict[str, float]] = {}

    for r in recs:
        key = _match_key(r.condition_id, r.outcome_index)
        matched = positions_by_key.get(key) if key else None
        cur_price = _rec_current_price(r, positions_by_key, extra_prices)

        src = by_source.setdefault(
            r.source,
            {"total": 0, "taken": 0, "skipped": 0, "taken_pnl": 0.0, "skipped_hypo_pnl": 0.0},
        )
        src["total"] += 1

        if matched is not None:
            status = "TAKEN"
            src["taken"] += 1
            pnl = matched.unrealized_pnl_usd + matched.realized_pnl_usd
            taken_pnl += pnl
            src["taken_pnl"] += pnl
            hypo = None
            hypo_pct = None
        else:
            status = "SKIPPED"
            src["skipped"] += 1
            # Hypothetical: if user had placed $1 at rec_price and held to now,
            # position would be worth $1 * (cur_price / rec_price). PnL = that − $1.
            if cur_price is not None and r.rec_price > 0:
                shares = 1.0 / r.rec_price
                hypo = shares * cur_price - 1.0
                hypo_pct = hypo  # $1 base, so pct == dollar pnl
                skipped_hypo_pnl += hypo
                src["skipped_hypo_pnl"] += hypo
            else:
                hypo = None
                hypo_pct = None

        comparisons.append(
            RecComparison(
                rec=r,
                status=status,
                matched_position=matched,
                current_price=cur_price,
                hypothetical_pnl_usd=hypo,
                hypothetical_pnl_pct=hypo_pct,
            )
        )

    return RecPerformance(
        comparisons=comparisons,
        total_recs=len(recs),
        taken_count=sum(1 for c in comparisons if c.status == "TAKEN"),
        skipped_count=sum(1 for c in comparisons if c.status == "SKIPPED"),
        taken_pnl_usd=taken_pnl,
        skipped_pnl_hypothetical_usd=skipped_hypo_pnl,
        by_source=by_source,
    )


def _parse_outcome_prices(raw: object) -> list[float] | None:
    """Gamma returns outcomePrices as either a JSON string `'["0.52","0.48"]'`
    or a list. Normalize."""
    import json

    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception:  # noqa: BLE001
            return None
    if not isinstance(raw, list):
        return None
    try:
        return [float(x) for x in raw]
    except (TypeError, ValueError):
        return None


async def fetch_rec_performance(
    client: PolymarketClient, wallet_addr: str, rec_limit: int = 200
) -> tuple[MyPortfolioSummary, RecPerformance]:
    """One-shot: pull /positions + load recent recs + join them.

    For SKIPPED recs we batch-lookup current prices via Gamma so the
    hypothetical-PnL column isn't always $0.
    """
    summary = await fetch_my_portfolio(client, wallet_addr)
    recs = await load_recent_recommendations(limit=rec_limit)

    # Identify condition_ids that will be SKIPPED (i.e. not in positions) but
    # have a condition_id we can price. Batch-fetch from Gamma.
    position_cids = {p.condition_id for p in summary.positions}
    skipped_cids = sorted({
        r.condition_id for r in recs
        if r.condition_id and r.condition_id not in position_cids
    })
    extra_prices: dict[str, list[float]] = {}
    if skipped_cids:
        # Gamma accepts repeated ?condition_ids=... — chunk to keep URL sane
        for i in range(0, len(skipped_cids), 20):
            chunk = skipped_cids[i : i + 20]
            try:
                markets = await client.get_markets_by_condition_ids(chunk)
            except Exception as e:  # noqa: BLE001 — keep comparison alive
                log.warning("rec_performance.gamma_failed", error=str(e))
                continue
            for m in markets:
                cid = m.get("conditionId")
                if not cid:
                    continue
                prices = _parse_outcome_prices(m.get("outcomePrices"))
                if prices:
                    extra_prices[cid] = prices

    perf = compare_recs_to_positions(recs, summary.positions, extra_prices)
    log.info(
        "rec_performance.computed",
        recs=perf.total_recs,
        taken=perf.taken_count,
        skipped=perf.skipped_count,
        taken_pnl=round(perf.taken_pnl_usd, 2),
        skipped_hypo_pnl=round(perf.skipped_pnl_hypothetical_usd, 2),
        priced_skipped=len(extra_prices),
    )
    return summary, perf


def days_until_resolution(end_date: str | None) -> int | None:
    if not end_date:
        return None
    try:
        dt = datetime.fromisoformat(end_date).replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    delta = (dt - datetime.now(tz=timezone.utc)).total_seconds() / 86400.0
    return int(round(delta))
