"""Exchange and data connectors.

Public API
----------
* :class:`PolymarketConnector` — discovers BTC 5m binary markets on Polymarket
* :class:`BinanceConnector` — BTC spot price and recent close history
* :class:`ChainlinkConnectorStub` — placeholder for Data Streams integration (#9)
* :class:`ConnectorRegistry` — registration, lookup, and health aggregation
* Base ABCs: ``AbstractPriceConnector``, ``AbstractMarketConnector``
* Exceptions: ``FeedError``, ``MarketDiscoveryError``
"""

from __future__ import annotations

from .base import (
    AbstractMarketConnector,
    AbstractPriceConnector,
    FeedError,
    MarketDiscoveryError,
)
from .binance import BinanceConnector
from .chainlink import ChainlinkConnectorStub
from .polymarket import PolymarketConnector
from .registry import ConnectorRegistry

__all__ = [
    "AbstractMarketConnector",
    "AbstractPriceConnector",
    "BinanceConnector",
    "ChainlinkConnectorStub",
    "ConnectorRegistry",
    "FeedError",
    "MarketDiscoveryError",
    "PolymarketConnector",
]
