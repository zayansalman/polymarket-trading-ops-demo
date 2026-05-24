"""Shared pytest fixtures for the btc_5m_fv test suite."""

from __future__ import annotations

import math
from datetime import datetime, timezone

import pytest

from btc_5m_fv.core.types import (
    BacktestParams,
    MarketWindow,
    OrderState,
    PaperOrder,
    PaperPosition,
    Side,
    Signal,
    SignalAction,
    StrategyParams,
)


# ---------------------------------------------------------------------------
# Strategy parameter fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def default_params() -> StrategyParams:
    """Default strategy parameters matching typical paper trading config."""
    return StrategyParams(
        min_trade_usd=1.0,
        max_trade_usd=5.0,
        entry_edge_min=0.05,
        min_confidence=0.60,
        entry_min_remaining_seconds=90,
        max_entry_price=0.95,
        min_entry_price=0.05,
    )


@pytest.fixture
def loose_params() -> StrategyParams:
    """Loose parameters that make it easy to generate entry signals."""
    return StrategyParams(
        min_trade_usd=1.0,
        max_trade_usd=5.0,
        entry_edge_min=0.01,
        min_confidence=0.50,
        entry_min_remaining_seconds=10,
        max_entry_price=0.99,
        min_entry_price=0.01,
    )


@pytest.fixture
def strict_params() -> StrategyParams:
    """Strict parameters that make it hard to generate entry signals."""
    return StrategyParams(
        min_trade_usd=5.0,
        max_trade_usd=10.0,
        entry_edge_min=0.20,
        min_confidence=0.85,
        entry_min_remaining_seconds=180,
        max_entry_price=0.70,
        min_entry_price=0.30,
    )


@pytest.fixture
def backtest_params() -> BacktestParams:
    """Typical backtest parameter set."""
    return BacktestParams(
        entry_edge_min=0.05,
        min_confidence=0.60,
        min_remaining_seconds=90,
        max_entry_price=0.95,
        min_trade_usd=1.0,
        max_trade_usd=5.0,
        min_entry_price=0.05,
    )


# ---------------------------------------------------------------------------
# Domain object fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def sample_window() -> MarketWindow:
    """A single BTC 5-minute market window."""
    return MarketWindow(
        slug="btc-updown-5m-1700000000",
        question="Bitcoin Up or Down - Dec 14, 2023?",
        start_ts=1700000000,
        end_ts=1700000300,
        up_price=0.52,
        down_price=0.48,
    )


@pytest.fixture
def sample_signal_enter_up() -> Signal:
    """A sample ENTER_UP signal."""
    return Signal(
        action=SignalAction.ENTER_UP,
        side=Side.UP,
        confidence=0.75,
        notional_usd=3.0,
        edge=0.08,
        fair_up_prob=0.60,
        reason="enter Up: edge +0.080",
    )


@pytest.fixture
def sample_signal_skip() -> Signal:
    """A sample SKIP signal."""
    return Signal(
        action=SignalAction.SKIP,
        side=None,
        confidence=0.55,
        notional_usd=0.0,
        edge=0.02,
        fair_up_prob=0.54,
        reason="skip: edge/confidence below threshold",
    )


@pytest.fixture
def sample_order() -> PaperOrder:
    """A filled paper order."""
    return PaperOrder(
        order_id=1,
        created_at=datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc),
        window_slug="btc-updown-5m-1700000000",
        side=Side.UP,
        state=OrderState.FILLED,
        requested_notional=3.0,
        filled_notional=3.0,
        entry_price=0.52,
        confidence=0.75,
        edge=0.08,
        feed_source="binance",
    )


@pytest.fixture
def sample_position(sample_order: PaperOrder) -> PaperPosition:
    """An open paper position wrapping *sample_order*."""
    return PaperPosition(
        position_id=1,
        order=sample_order,
        opened_at=datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc),
    )


@pytest.fixture
def closed_position(sample_order: PaperOrder) -> PaperPosition:
    """A closed paper position with realized PnL."""
    return PaperPosition(
        position_id=2,
        order=sample_order,
        opened_at=datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc),
        closed_at=datetime(2024, 1, 1, 12, 2, 30, tzinfo=timezone.utc),
        exit_price=0.95,
        exit_reason="TARGET",
        realized_pnl_usd=1.24,
    )


# ---------------------------------------------------------------------------
# Price series fixtures for fair-value tests
# ---------------------------------------------------------------------------


@pytest.fixture
def flat_prices() -> list[float]:
    """Constant prices — should yield sigma floor."""
    return [50000.0] * 20


@pytest.fixture
def volatile_prices() -> list[float]:
    """Prices with a clear upward drift and volatility."""
    base = 50000.0
    prices = [base]
    for i in range(1, 30):
        prices.append(base * (1 + 0.001 * i + 0.0005 * math.sin(i)))
    return prices


@pytest.fixture
def valid_closes() -> list[float]:
    """90 seconds of realistic 1-second BTC closes."""
    base = 50000.0
    closes: list[float] = [base]
    for i in range(1, 90):
        # ~0.02% per-step volatility => sigma_per_second ~0.0002
        closes.append(base * (1 + 0.0002 * (1 if i % 3 == 0 else -1)))
        base = closes[-1]
    return closes
