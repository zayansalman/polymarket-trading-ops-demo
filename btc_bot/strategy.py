"""Shared BTC 5-minute binary strategy math."""
from __future__ import annotations

import math
import statistics
from dataclasses import dataclass


@dataclass(frozen=True)
class StrategyParams:
    min_trade_usd: float
    max_trade_usd: float
    entry_edge_min: float
    min_confidence: float
    entry_min_remaining_seconds: int = 90
    max_entry_price: float = 0.95
    min_entry_price: float = 0.05


def sigma_per_second(closes: list[float]) -> float:
    """Estimate one-second volatility from recent closes with a safety floor."""
    returns = [
        math.log(closes[i] / closes[i - 1])
        for i in range(1, len(closes))
        if closes[i] > 0 and closes[i - 1] > 0
    ]
    if len(returns) < 2:
        return 0.00002
    # Floor prevents a quiet sample from producing false certainty.
    return max(statistics.stdev(returns), 0.00002)


def fair_up_probability(
    spot: float,
    reference: float,
    sigma: float,
    remaining_seconds: int,
) -> float:
    """Volatility-band probability that BTC finishes above the reference price."""
    if spot <= 0 or reference <= 0:
        return 0.5
    denom = sigma * math.sqrt(max(remaining_seconds, 1))
    if denom <= 0:
        return 0.5
    z = math.log(spot / reference) / denom
    return min(0.995, max(0.005, 0.5 * (1 + math.erf(z / math.sqrt(2)))))


def confidence_from_edge(edge: float) -> float:
    return min(0.99, max(0.0, 0.50 + abs(edge) * 2.8))


def notional_from_confidence(confidence: float, params: StrategyParams) -> float:
    if confidence < params.min_confidence:
        return 0.0
    span = max(params.max_trade_usd - params.min_trade_usd, 0)
    scaled = (confidence - params.min_confidence) / max(0.99 - params.min_confidence, 0.01)
    raw = params.min_trade_usd + span * min(max(scaled, 0.0), 1.0)
    return float(round(min(max(raw, params.min_trade_usd), params.max_trade_usd)))


def signal_from_edge(
    edge: float,
    remaining_seconds: int,
    up_price: float,
    down_price: float,
    params: StrategyParams,
) -> tuple[str | None, float, float, str]:
    """Return side, confidence, paper notional, and reason for a current tick."""
    confidence = confidence_from_edge(edge)
    if remaining_seconds <= params.entry_min_remaining_seconds:
        return None, confidence, 0.0, "skip: too close to window end"
    if abs(edge) < params.entry_edge_min or confidence < params.min_confidence:
        return None, confidence, 0.0, "skip: edge/confidence below threshold"
    side = "Up" if edge > 0 else "Down"
    entry_price = up_price if side == "Up" else down_price
    if entry_price < params.min_entry_price or entry_price > params.max_entry_price:
        return None, confidence, 0.0, "skip: entry price too extreme for paper fill model"
    notional = notional_from_confidence(confidence, params)
    return side, confidence, notional, f"enter {side}: edge {edge:+.3f}"
